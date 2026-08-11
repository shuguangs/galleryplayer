"""Main browser window: toolbar, folder tree, and the three media views."""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QDir,
    QModelIndex,
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFileSystemModel,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QSlider,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import dircache, fileops, icons, media, theme
from .albums import albums, orders
from .browser import DetailsView, MediaModel, TileView
from .config import flush, settings
from .i18n import t
from .thumbs import ThumbnailCache
from .welcome import WelcomePage, remember_recent

if TYPE_CHECKING:  # `viewer` pulls in libmpv; keep it off the startup path
    from .viewer import Viewer


def _icon_button(glyph: str, tip: str, width: int = 32, checkable: bool = False) -> QToolButton:
    b = QToolButton()
    b.setObjectName("IconBtn")
    b.setText(glyph)
    b.setToolTip(tip)
    b.setFixedWidth(width)
    b.setCheckable(checkable)
    return b


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setStyleSheet(f"color:{theme.BORDER};")
    f.setFixedWidth(1)
    return f


# A GUI thread busy laying out tiles cannot drain queued signals as fast as a warm
# cache produces them. Past this many unacknowledged batches the scan waits instead of
# queueing more, so a slow repaint can never turn into an unbounded backlog that the
# window then has to chew through long after the scan itself has finished.
MAX_BATCHES_IN_FLIGHT = 24


class _ScanSignals(QObject):
    # folder, items batch, token, phase ("cache" / "scan" / "done"), ScanStats
    batch = Signal(object, object, int, str, object)

    def __init__(self) -> None:
        super().__init__()
        # Back-pressure: a slot is taken before a batch is queued and handed back once
        # the GUI thread has dealt with it.
        self.inflight = threading.Semaphore(MAX_BATCHES_IN_FLIGHT)


class _ScanTask(QRunnable):
    """Folder scan on a pool thread.

    Everything about opening a folder happens here, including the zero-I/O pass that
    rebuilds the listing from cache. That pass touches no disk, but it still builds an
    item per file and a natural-sort key per item, which on a tree of tens of thousands
    of files is seconds of pure CPU — on the GUI thread that was the window going white
    and Windows offering to close it.

    The signal emitter is owned by the window, not by the task: QThreadPool deletes
    a QRunnable once run() returns, which would take a task-owned QObject with it
    and silently drop the queued signal before it reaches the GUI thread.
    """

    def __init__(
        self,
        folder: Path,
        recursive: bool,
        signals: _ScanSignals,
        token: int,
        use_cache: bool = True,
        stop_check=None,
    ) -> None:
        super().__init__()
        self.folder = folder
        self.recursive = recursive
        self.signals = signals
        self.token = token
        self.use_cache = use_cache
        self.stop_check = stop_check

    BATCH_ITEMS = 80          # flush at least this often ...
    BATCH_INTERVAL = 0.12     # ... and never sit on results longer than this

    def run(self) -> None:
        stats = media.ScanStats()
        pending: list = []
        last_flush = time.monotonic()

        def stopped() -> bool:
            return self.stop_check is not None and self.stop_check()

        def flush(phase: str = "scan") -> None:
            nonlocal pending, last_flush
            # Timed rather than blocking for ever: if the window went away before it
            # could acknowledge, the slot is never returned and this thread must not
            # be left parked on it.
            self.signals.inflight.acquire(timeout=5.0)
            self.signals.batch.emit(self.folder, pending, self.token, phase, stats)
            pending = []
            last_flush = time.monotonic()

        def emit(items: list) -> None:
            nonlocal pending
            if not items:
                return
            pending.extend(items)
            now = time.monotonic()
            if len(pending) >= self.BATCH_ITEMS or now - last_flush >= self.BATCH_INTERVAL:
                flush()

        def progress(_stats) -> None:
            # A level of folders that hold no media of their own produces no batches at
            # all, so without this the status line would sit still through the part of
            # the scan that takes longest on a deep tree.
            if time.monotonic() - last_flush >= self.BATCH_INTERVAL:
                flush()

        try:
            # Phase 1: everything the cache already knows, no filesystem access at all.
            # A folder opened before is on screen in full before the first stat() runs.
            if self.use_cache and not stopped():
                cached = media.scan_from_cache(self.folder, self.recursive)
                if cached and not stopped():
                    pending = cached
                    flush("cache")

            # Phase 2: the authoritative level-order pass.
            if not stopped():
                media.scan_folder_streaming(
                    self.folder,
                    self.recursive,
                    self.use_cache,
                    emit,
                    self.stop_check,
                    stats,
                    progress,
                )
                dircache.cache.save()
        except Exception:
            pass
        flush("done")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(t("main_window.title"))
        self.resize(1360, 850)
        self.setAcceptDrops(True)

        self.thumbs = ThumbnailCache(self)
        self.model = MediaModel(self)
        self.all_items: list[media.MediaItem] = []
        self.folder: Path | None = None
        self._nav_history: list[Path] = []   # 访问历史（浏览器式后退/前进）
        self._nav_index: int = -1
        self._nav_from_history: bool = False  # 标记当前导航来自历史（避免重复记录）
        self._scroll_positions: dict[str, int] = {}  # 文件夹路径 -> 滚动位置
        self._load_scroll_positions()
        self._scan_token = 0
        self._random_seed = random.randrange(1 << 30)
        self._pool = QThreadPool.globalInstance()
        self._scan_signals = _ScanSignals()
        self._scan_signals.batch.connect(self._on_scan_batch)
        self._pending_viewer_folder: Path | None = None

        # Progressive scan bookkeeping
        self._streaming = False
        self._stream_items: list[media.MediaItem] = []
        self._stream_timer = QTimer(self)
        self._stream_timer.setSingleShot(True)
        self._stream_timer.setInterval(self.STREAM_MIN_INTERVAL)
        self._stream_timer.timeout.connect(self._flush_stream)

        self._resort_timer = QTimer(self)
        self._resort_timer.setSingleShot(True)
        self._resort_timer.setInterval(700)
        self._resort_timer.timeout.connect(self._apply_view)
        self.thumbs.meta_ready.connect(self._on_meta_ready)

        self._build_ui()
        self._restore_state()
        self._install_shortcuts()

        # Preferences are saved on close, but a crash or a force-kill would otherwise
        # discard the session's volume / speed / folder. Cheap periodic insurance.
        self._autosave = QTimer(self)
        self._autosave.setInterval(20_000)
        self._autosave.timeout.connect(self._save_state)
        self._autosave.start()

        # Built on first use: constructing it spins up a libmpv instance, which is the
        # single largest chunk of startup time and is pure waste for a browse-only run.
        self.viewer: "Viewer | None" = None

        # No folder is opened on launch: the welcome page waits for the user. The
        # remembered folder is only used as the file dialog's starting directory.
        self._show_welcome()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_tree())

        self.stack = QStackedWidget()
        self.welcome = WelcomePage()
        self.welcome.open_requested.connect(self._choose_folder)
        self.welcome.folder_chosen.connect(self.set_folder)
        self.welcome.file_chosen.connect(self._open_recent_file)
        self.stack.addWidget(self.welcome)
        self.tiles = TileView(self.model, self.thumbs)
        self.stack.addWidget(self.tiles)
        # The first QTableView in a process costs ~2s of one-time Qt item-view
        # initialisation regardless of its model, so the details table is built only
        # if the user actually asks for list view.
        self.details: DetailsView | None = None
        self.splitter.addWidget(self.stack)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([250, 1100])
        root.addWidget(self.splitter, 1)

        root.addWidget(self._build_statusbar())
        self.setCentralWidget(central)

        self.tiles.activatedRow.connect(self._open_viewer)
        self.tiles.contextRow.connect(self._media_menu)
        self.model.sort_requested.connect(self._on_header_sort)

    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("Toolbar")
        bar.setFixedHeight(46)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(9, 6, 9, 6)
        lay.setSpacing(7)

        btn_open = _icon_button(icons.FOLDER_OPEN + "  " + t("main_window.open_folder"), t("main_window.choose_folder_tip"), 118)
        btn_open.clicked.connect(self._choose_folder)
        lay.addWidget(btn_open)

        btn_up = _icon_button(icons.LEVEL_UP, t("main_window.go_up_tip"))
        btn_up.clicked.connect(self._go_up)
        lay.addWidget(btn_up)

        self.btn_back = _icon_button(icons.CHEVRON_LEFT, t("main_window.back_tip"))
        self.btn_back.clicked.connect(self._nav_back)
        self.btn_back.setEnabled(False)
        lay.addWidget(self.btn_back)

        self.btn_forward = _icon_button(icons.CHEVRON_RIGHT, t("main_window.forward_tip"))
        self.btn_forward.clicked.connect(self._nav_forward)
        self.btn_forward.setEnabled(False)
        lay.addWidget(self.btn_forward)

        btn_refresh = _icon_button(icons.REFRESH, t("main_window.rescan_tip"))
        btn_refresh.clicked.connect(lambda: self.set_folder(self.folder, force=True))
        lay.addWidget(btn_refresh)

        lay.addWidget(_sep())

        self.view_buttons: dict[str, QToolButton] = {}
        for mode, glyph, tip in (
            ("grid", icons.VIEW_GRID, t("main_window.view_grid_tip")),
            ("waterfall", icons.VIEW_TILES, t("main_window.view_waterfall_tip")),
            ("list", icons.VIEW_LIST, t("main_window.view_list_tip")),
        ):
            b = _icon_button(glyph, tip, 34, checkable=True)
            b.clicked.connect(lambda _=False, m=mode: self.set_view_mode(m))
            lay.addWidget(b)
            self.view_buttons[mode] = b

        self.col_label = QLabel(t("main_window.columns"))
        self.col_label.setObjectName("Hint")
        lay.addWidget(self.col_label)
        self.col_slider = QSlider(Qt.Horizontal)
        self.col_slider.setRange(1, 12)
        self.col_slider.setFixedWidth(96)
        self.col_slider.setToolTip(t("main_window.columns_tip"))
        self.col_slider.valueChanged.connect(self._on_columns_changed)
        lay.addWidget(self.col_slider)

        lay.addWidget(_sep())

        lay.addWidget(QLabel(t("main_window.sort")))
        self.sort_combo = icons.ArrowComboBox()
        for key, label in media.SORT_LABELS.items():
            self.sort_combo.addItem(t(label), key)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        lay.addWidget(self.sort_combo)

        self.btn_desc = _icon_button(icons.SORT_ASC, t("main_window.sort_toggle_tip"), 30, checkable=True)
        self.btn_desc.clicked.connect(self._on_sort_changed)
        lay.addWidget(self.btn_desc)

        self.filter_combo = icons.ArrowComboBox()
        for key, label in media.FILTER_LABELS.items():
            self.filter_combo.addItem(t(label), key)
        # wrapped: the signal's own argument must not land in _apply_view(count_suffix)
        self.filter_combo.currentIndexChanged.connect(lambda _=0: self._apply_view())
        lay.addWidget(self.filter_combo)

        self.btn_recursive = QToolButton()
        self.btn_recursive.setText(t("main_window.recursive"))
        self.btn_recursive.setCheckable(True)
        self.btn_recursive.setToolTip(t("main_window.recursive_tip"))
        self.btn_recursive.clicked.connect(self._on_recursive_toggled)
        lay.addWidget(self.btn_recursive)

        lay.addStretch(1)

        self.search = QLineEdit()
        self.search.setPlaceholderText(t("main_window.search_placeholder"))
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(190)
        self.search.textChanged.connect(lambda _="": self._apply_view())
        lay.addWidget(self.search)

        self.btn_tree = _icon_button(icons.SIDEBAR, t("main_window.tree_toggle_tip"), 32, checkable=True)
        self.btn_tree.clicked.connect(self._toggle_tree)
        lay.addWidget(self.btn_tree)

        btn_settings = _icon_button(icons.SETTINGS, t("main_window.settings_tip"), 32)
        # The Fluent/MDL2 gear glyph (E713) is taller than the em box, so at the
        # toolbar's 14px icon size Qt clips its top and bottom to the text line box.
        # A slightly smaller size keeps the whole gear inside the line box.
        btn_settings.setStyleSheet(
            f'QToolButton#IconBtn {{ font-family:"{icons.FAMILY}"; font-size:11px; }}'
        )
        btn_settings.clicked.connect(self._show_settings)
        lay.addWidget(btn_settings)

        btn_help = _icon_button(icons.HELP, t("main_window.help_tip"), 32)
        btn_help.clicked.connect(self._show_help)
        lay.addWidget(btn_help)
        return bar

    def _build_tree(self) -> QWidget:
        """Return the (initially empty) side panel; the tree fills it in later.

        Realising the first QAbstractItemView under a large stylesheet costs a couple
        of seconds of one-off Qt style resolution. Deferring it until just after the
        first paint gets the window on screen quickly instead of showing nothing.
        """
        wrap = QWidget()
        self._tree_layout = QVBoxLayout(wrap)
        self._tree_layout.setContentsMargins(0, 0, 0, 0)
        self._tree_layout.setSpacing(0)

        # ---- sort bar for the folder tree itself (independent of the media sort)
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(6, 4, 6, 2)
        bl.setSpacing(4)
        self.tree_sort_combo = icons.ArrowComboBox()
        for key in ("name", "mtime", "size"):
            self.tree_sort_combo.addItem(t(media.SORT_LABELS[key]), key)
        idx = self.tree_sort_combo.findData(str(settings["tree_sort_key"]))
        self.tree_sort_combo.setCurrentIndex(max(0, idx))
        self.tree_sort_combo.currentIndexChanged.connect(self._apply_tree_sort)
        bl.addWidget(self.tree_sort_combo)
        self.tree_sort_desc = _icon_button(
            icons.SORT_ASC, t("main_window.sort_toggle_tip"), 24, checkable=True
        )
        self.tree_sort_desc.setChecked(bool(settings["tree_sort_desc"]))
        self.tree_sort_desc.clicked.connect(self._apply_tree_sort)
        bl.addWidget(self.tree_sort_desc)
        bl.addStretch(1)
        self._tree_layout.addWidget(bar)

        self.fs_model: QFileSystemModel | None = None
        self.tree: QTreeView | None = None
        return wrap

    def _apply_tree_sort(self) -> None:
        """Sort the folder tree by the selected column (hidden columns are fine)."""
        key = self.tree_sort_combo.currentData() or "name"
        desc = self.tree_sort_desc.isChecked()
        col = {"name": 0, "size": 1, "mtime": 3}.get(key, 0)
        if self.fs_model is not None:
            self.fs_model.sort(
                col, Qt.DescendingOrder if desc else Qt.AscendingOrder
            )
        settings["tree_sort_key"] = key
        settings["tree_sort_desc"] = desc

    def _materialize_tree(self) -> None:
        if self.tree is not None:
            return
        self.fs_model = QFileSystemModel()
        self.fs_model.setFilter(QDir.Dirs | QDir.Drives | QDir.NoDotAndDotDot)
        # No file watchers: the tree only lists directories, and watching a large
        # tree costs handles and startup time for nothing.
        self.fs_model.setOption(QFileSystemModel.DontWatchForChanges, True)
        self.fs_model.setRootPath("")

        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(False)
        self.tree.setIndentation(14)
        for c in range(1, 4):
            self.tree.hideColumn(c)
        self.tree.clicked.connect(self._on_tree_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self._tree_layout.addWidget(self.tree)
        self._apply_tree_sort()
        if self.folder:
            self._sync_tree(self.folder)

    def showEvent(self, e):
        super().showEvent(e)
        if self.tree is None and self.btn_tree.isChecked():
            # after this paint, not during it
            QTimer.singleShot(0, self._materialize_tree)

    def _build_statusbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(28)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 0, 10, 0)
        self.status_path = QLabel("")
        self.status_path.setObjectName("Hint")
        lay.addWidget(self.status_path, 1)
        self.status_count = QLabel("")
        self.status_count.setObjectName("Hint")
        lay.addWidget(self.status_count)
        return bar

    def _install_shortcuts(self) -> None:
        def sc(seq: str, fn):
            s = QShortcut(QKeySequence(seq), self)
            s.activated.connect(fn)
            return s

        sc("Ctrl+O", self._choose_folder)
        sc("F5", lambda: self.set_folder(self.folder, force=True))
        sc("Backspace", self._go_up)
        sc("Ctrl+B", lambda: self.btn_tree.click())
        sc("Ctrl+F", lambda: (self.search.setFocus(), self.search.selectAll()))
        sc("Ctrl+1", lambda: self.set_view_mode("grid"))
        sc("Ctrl+2", lambda: self.set_view_mode("waterfall"))
        sc("Ctrl+3", lambda: self.set_view_mode("list"))
        sc("Alt+Left", self._nav_back)
        sc("Alt+Right", self._nav_forward)
        sc("F1", self._show_help)
        sc("Ctrl+,", self._show_settings)

    def _show_settings(self) -> None:
        from .settings_dialog import SettingsDialog

        SettingsDialog.show_for(self)

    def _show_help(self) -> None:
        from .help_dialog import HelpDialog

        HelpDialog.show_for(self)

    # ------------------------------------------------------------- state

    def _restore_state(self) -> None:
        self.col_slider.setValue(int(settings["grid_columns"]))
        self.tiles.set_columns(int(settings["grid_columns"]))
        idx = self.sort_combo.findData(settings["sort_key"])
        self.sort_combo.setCurrentIndex(max(0, idx))
        self.btn_desc.setChecked(bool(settings["sort_desc"]))
        self._update_desc_icon()
        idx = self.filter_combo.findData(settings["filter_kind"])
        self.filter_combo.setCurrentIndex(max(0, idx))
        self.btn_recursive.setChecked(bool(settings["recursive"]))
        self.btn_tree.setChecked(bool(settings["tree_visible"]))
        self.splitter.widget(0).setVisible(bool(settings["tree_visible"]))
        # Only reflect the saved mode on the buttons here. Calling set_view_mode() would
        # build the details table at startup and undo its lazy construction; the real
        # switch happens once a folder is actually opened.
        saved_mode = str(settings["view_mode"])
        for mode, button in self.view_buttons.items():
            button.setChecked(mode == saved_mode)
        sizes = settings["splitter_sizes"]
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(s, int) for s in sizes):
            self.splitter.setSizes(sizes)

    def _save_state(self) -> None:
        settings["grid_columns"] = self.col_slider.value()
        settings["sort_key"] = self.sort_combo.currentData()
        settings["sort_desc"] = self.btn_desc.isChecked()
        settings["filter_kind"] = self.filter_combo.currentData()
        settings["recursive"] = self.btn_recursive.isChecked()
        settings["tree_visible"] = self.btn_tree.isChecked()
        settings["splitter_sizes"] = self.splitter.sizes()
        if self.folder:
            settings["last_folder"] = str(self.folder)
        flush()

    # ------------------------------------------------------------ actions

    def _choose_folder(self) -> None:
        start = str(self.folder) if self.folder else ""
        d = QFileDialog.getExistingDirectory(self, t("main_window.choose_folder_dialog"), start)
        if d:
            self.set_folder(Path(d))

    def _go_up(self) -> None:
        if self.folder and self.folder.parent != self.folder:
            self._save_scroll_pos()
            self._push_nav_history(self.folder)
            self.set_folder(self.folder.parent)

    def _toggle_tree(self) -> None:
        show = self.btn_tree.isChecked()
        if show:
            self._materialize_tree()
        self.splitter.widget(0).setVisible(show)

    def _show_welcome(self) -> None:
        self.welcome.refresh_recent()
        self.stack.setCurrentWidget(self.welcome)
        self.col_slider.setVisible(False)
        self.col_label.setVisible(False)
        self.status_path.setText(t("main_window.no_folder"))
        self.status_count.setText("")
        self.setWindowTitle(t("main_window.title"))

    def _show_browser(self) -> None:
        """Leave the welcome page for whichever view mode is selected."""
        if self.stack.currentWidget() is self.welcome:
            self.set_view_mode(str(settings["view_mode"]))

    def _remember_recent(self, folder: Path) -> None:
        remember_recent(folder)
        settings["last_folder"] = str(folder)

    def _ensure_details(self) -> DetailsView:
        if self.details is None:
            self.details = DetailsView(self.model)
            self.details.activatedRow.connect(self._open_viewer)
            self.details.contextRow.connect(self._media_menu)
            self.stack.addWidget(self.details)
        return self.details

    def set_view_mode(self, mode: str) -> None:
        for m, b in self.view_buttons.items():
            b.setChecked(m == mode)
        settings["view_mode"] = mode
        if self.folder is None:
            return  # stay on the welcome page until there is something to show
        is_tile = mode in ("grid", "waterfall")
        self.col_slider.setVisible(is_tile)
        self.col_label.setVisible(is_tile)
        if is_tile:
            self.tiles.set_mode(mode)
            self.stack.setCurrentWidget(self.tiles)
        else:
            self.stack.setCurrentWidget(self._ensure_details())

    def _on_columns_changed(self, n: int) -> None:
        self.tiles.set_columns(n)

    def _on_sort_changed(self) -> None:
        self._update_desc_icon()
        if self.sort_combo.currentData() == "random":
            self._random_seed = random.randrange(1 << 30)
        self._apply_view()

    def _update_desc_icon(self) -> None:
        self.btn_desc.setText(icons.SORT_DESC if self.btn_desc.isChecked() else icons.SORT_ASC)

    def _on_header_sort(self, key: str, desc: bool) -> None:
        i = self.sort_combo.findData(key)
        if i >= 0:
            self.sort_combo.blockSignals(True)
            self.sort_combo.setCurrentIndex(i)
            self.sort_combo.blockSignals(False)
        self.btn_desc.setChecked(desc)
        self._update_desc_icon()
        self._apply_view()

    def _on_recursive_toggled(self) -> None:
        self.set_folder(self.folder, force=True)

    def _sync_tree(self, folder: Path) -> None:
        """Reveal `folder` in the directory tree without re-triggering a scan.

        Deferred by one turn of the event loop. Resolving a path in QFileSystemModel
        walks it component by component, and on a network share or a spun-down USB disk
        each of those is a blocking call; doing it inline would hold up opening the
        folder, which is the part the user is waiting on.
        """
        if self.tree is None or self.fs_model is None:
            return
        QTimer.singleShot(0, lambda: self._sync_tree_now(folder))

    def _sync_tree_now(self, folder: Path) -> None:
        if self.tree is None or self.fs_model is None or folder != self.folder:
            return
        idx = self.fs_model.index(str(folder))
        if not idx.isValid():
            return
        self.tree.blockSignals(True)
        self.tree.setCurrentIndex(idx)
        self.tree.expand(idx)
        self.tree.scrollTo(idx, QTreeView.PositionAtCenter)
        self.tree.blockSignals(False)

    def _on_tree_clicked(self, index: QModelIndex) -> None:
        if self.fs_model is None:
            return
        # No is_dir() check: the model is filtered to directories and drives already,
        # and asking the file system would put a blocking stat on a network path back
        # on the GUI thread.
        self.set_folder(Path(self.fs_model.filePath(index)))

    def _on_meta_ready(self, *_args) -> None:
        # duration/aspect just changed; re-sort or re-flow shortly
        if self.sort_combo.currentData() == "duration":
            self._resort_timer.start()

    # ------------------------------------------------------- context menus

    def _media_menu(self, row: int, global_pos) -> None:
        menu = self.build_media_menu(row)
        if menu.actions():
            menu.exec(global_pos)

    def build_media_menu(self, row: int) -> QMenu:
        """Right-click menu for the grid / waterfall / details views."""
        menu = QMenu(self)
        item = self.model.item(row)

        if item is not None:
            act = menu.addAction(t("main_window.open"))
            act.triggered.connect(lambda _=False, r=row: self._open_viewer(r))
            menu.addAction(t("main_window.open_default")).triggered.connect(
                lambda _=False, p=item.path: fileops.open_default(p)
            )
            menu.addSeparator()
            native = menu.addAction(t("main_window.open_native"))
            native.setCheckable(True)
            native.setChecked(bool(settings["open_native_size"]))
            native.setToolTip(t("main_window.open_native_tip"))
            native.triggered.connect(
                lambda checked: settings.__setitem__("open_native_size", checked)
            )
            menu.addSeparator()
            menu.addAction(t("main_window.reveal_in_explorer")).triggered.connect(
                lambda _=False, p=item.path: fileops.reveal(p)
            )
            menu.addAction(t("main_window.copy_path")).triggered.connect(
                lambda _=False, p=item.path: fileops.copy_to_clipboard(str(p))
            )
            menu.addAction(t("main_window.copy_name")).triggered.connect(
                lambda _=False, p=item.path: fileops.copy_to_clipboard(p.name)
            )
            menu.addSeparator()
            if not item.is_video:
                menu.addAction(t("menu.copy_image")).triggered.connect(
                    lambda _=False, p=item.path: fileops.copy_image_to_clipboard(p)
                )
            menu.addAction(t("menu.copy_file")).triggered.connect(
                lambda _=False, p=item.path: fileops.copy_files_to_clipboard([p])
            )
            menu.addSeparator()
            menu.addAction(t("main_window.rename")).triggered.connect(
                lambda _=False, p=item.path: self._rename_media(p)
            )
            menu.addAction(t("main_window.recycle")).triggered.connect(
                lambda _=False, p=item.path: self._recycle_media([p])
            )
            menu.addSeparator()

        if self.folder is not None:
            menu.addAction(t("main_window.open_containing_folder")).triggered.connect(
                lambda _=False, f=self.folder: fileops.open_folder(f)
            )
            menu.addAction(t("main_window.rescan")).triggered.connect(
                lambda: self.set_folder(self.folder, force=True)
            )
        return menu

    def _rename_media(self, path: Path) -> None:
        if fileops.rename(self, path) is None:
            return
        # The name is part of the cached directory record, so the listing has to be
        # rebuilt from disk rather than from what the cache still believes.
        dircache.cache.forget(path.parent)
        self.set_folder(self.folder, force=True)

    def _recycle_media(self, paths: list[Path]) -> None:
        if not fileops.confirm_recycle(self, paths):
            return
        done, err = fileops.recycle(paths)
        if err and not done:
            self.status_count.setText(err)
            return
        for p in {p.parent for p in paths}:
            dircache.cache.forget(p)
        self.set_folder(self.folder, force=True)

    def _tree_menu(self, pos) -> None:
        if self.tree is None:
            return
        menu = self.build_tree_menu(self.tree.indexAt(pos))
        if menu is not None and menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def build_tree_menu(self, idx) -> QMenu | None:
        """Right-click menu for the folder tree."""
        if self.tree is None or self.fs_model is None:
            return None
        menu = QMenu(self)
        if idx.isValid():
            folder = Path(self.fs_model.filePath(idx))
            menu.addAction(t("main_window.browse_here")).triggered.connect(
                lambda _=False, f=folder: self.set_folder(f)
            )
            menu.addAction(t("main_window.open_in_explorer")).triggered.connect(
                lambda _=False, f=folder: fileops.open_folder(f)
            )
            menu.addSeparator()
            menu.addAction(t("main_window.copy_folder_path")).triggered.connect(
                lambda _=False, f=folder: fileops.copy_to_clipboard(str(f))
            )
            # A drive root has no name to change, and renaming one is not a thing.
            if folder.parent != folder:
                menu.addAction(t("main_window.rename")).triggered.connect(
                    lambda _=False, f=folder: self._rename_folder(f)
                )
            menu.addSeparator()
            menu.addAction(t("main_window.rescan_folder")).triggered.connect(
                lambda _=False, f=folder: self._rescan_folder(f)
            )
        menu.addAction(t("main_window.collapse_all")).triggered.connect(self.tree.collapseAll)
        return menu

    def _rename_folder(self, folder: Path) -> None:
        target = fileops.rename(self, folder, is_dir=True)
        if target is None:
            return
        dircache.cache.forget(folder)
        dircache.cache.forget(folder.parent)
        if self.folder is not None and (self.folder == folder or folder in self.folder.parents):
            self.set_folder(target, force=True)
        else:
            self._sync_tree(target)

    def _rescan_folder(self, folder: Path) -> None:
        dircache.cache.forget(folder)
        self.set_folder(folder, force=True)

    # ------------------------------------------------------------ scanning

    def set_folder(self, folder: Path | None, force: bool = False) -> None:
        """Open `folder`. Returns immediately; everything slow happens on a pool thread.

        Note what is deliberately *not* done here: no `is_dir()`, no cache walk, no
        listing built. A single `stat()` against a sleeping USB disk or a network share
        that has to be re-dialled costs seconds, and on the GUI thread those seconds are
        a frozen window. A folder that turns out not to be readable simply comes back
        empty, which the status line says.
        """
        if folder is None:
            return
        folder = Path(folder)
        if folder == self.folder and not force:
            return
        # Record navigation history (skip if navigating from history or refreshing)
        if not force and not self._nav_from_history and self.folder is not None:
            self._save_scroll_pos()
            self._push_nav_history(self.folder)
        self.folder = folder
        self.thumbs.invalidate_queue()
        self.thumbs.trim_memory(600)
        self.status_path.setText(str(folder))
        self.setWindowTitle(t("main_window.title_with_folder").format(folder=folder.name or str(folder)))
        self._show_browser()
        self._remember_recent(folder)
        self._sync_tree(folder)

        recursive = self.btn_recursive.isChecked()
        self._scan_token += 1
        token = self._scan_token

        self._streaming = True
        self._stream_items = []
        self._stream_timer.setInterval(self.STREAM_MIN_INTERVAL)
        self.all_items = []
        self.model.set_items([])
        self.status_count.setText(t("main_window.scanning"))

        # One task, two phases: rebuild from cache with zero filesystem I/O so a folder
        # opened before is on screen at once, then the authoritative level-order pass in
        # which only directories that actually changed get read.
        self._pool.start(
            _ScanTask(
                folder,
                recursive,
                self._scan_signals,
                token,
                use_cache=not force,
                stop_check=lambda t=token: t != self._scan_token,
            )
        )

    def _scan_progress_text(self, stats) -> str:
        """Status line for a scan in flight.

        The scan runs level by level, so how many folders are already *known* runs ahead
        of how many have been opened — showing both is what makes the progress read as
        progress rather than as a number climbing for no visible reason.
        """
        n = len(self._stream_items)
        if stats is None:
            return t("main_window.scanning_now")
        text = t("main_window.scan_progress").format(
            n=n, dirs_total=stats.dirs_total, dirs_found=stats.dirs_found
        )
        if stats.levels > 1:
            text += t("main_window.scan_level").format(levels=stats.levels)
        return text

    def _on_scan_batch(
        self, folder: Path, items: list, token: int, phase: str, stats
    ) -> None:
        try:
            self._handle_scan_batch(folder, items, token, phase, stats)
        finally:
            # Always, even for a batch belonging to an abandoned scan: the slot is what
            # the scanning thread is waiting on, and never giving it back would stall
            # that thread until its timeout.
            self._scan_signals.inflight.release()

    def _handle_scan_batch(
        self, folder: Path, items: list, token: int, phase: str, stats
    ) -> None:
        if token != self._scan_token:
            return

        if phase == "cache":
            # A complete listing rebuilt from cache. Show it in full, then let the
            # authoritative pass verify it quietly underneath.
            self._stream_timer.stop()
            self._streaming = False
            # Left empty on purpose: it accumulates the *authoritative* pass, which is
            # about to re-report every one of these files. Seeding it here would show
            # each of them twice when that pass lands.
            self._stream_items = []
            self.all_items = items
            self._random_seed = random.randrange(1 << 30)
            self._apply_view(count_suffix=t("main_window.verifying_suffix"))
            return

        self._stream_items.extend(items)

        if phase != "done":
            if not self._streaming:
                return  # a cached list is already on screen; verify quietly
            # Repaint at a human pace rather than once per directory: re-sorting and
            # resetting the model costs far more than accumulating the batch.
            self.status_count.setText(self._scan_progress_text(stats))
            if not self._stream_timer.isActive():
                self._stream_timer.start()
            return

        self._stream_timer.stop()
        self.all_items = self._stream_items
        self._stream_items = []
        self._streaming = False
        self._random_seed = random.randrange(1 << 30)
        suffix = ""
        if stats is not None and stats.dirs_reused:
            suffix = t("main_window.cache_hit_suffix").format(
                reused=stats.dirs_reused, total=stats.dirs_total
            )
        self._apply_view(count_suffix=suffix)

        # A folder picked from the panel's browser tab keeps playback going: hand the
        # freshly scanned list straight to the open viewer.
        pending = self._pending_viewer_folder
        self._pending_viewer_folder = None
        if (
            pending is not None
            and pending == folder
            and self.viewer is not None
            and self.viewer.isVisible()
            and self.model.items
        ):
            self.viewer.open_playlist(self.model.items, 0)

    STREAM_MIN_INTERVAL = 220
    STREAM_MAX_INTERVAL = 1500

    def _flush_stream(self) -> None:
        """Show what the breadth-first scan has found so far.

        Re-sorting and resetting the model costs roughly 200 ms once a list reaches a
        few thousand entries, so the repaint interval is tuned from how long the last
        repaint actually took. Otherwise a long scan would leave the UI thread with no
        headroom to stay responsive.
        """
        if not self._streaming:
            return
        started = time.perf_counter()
        self.all_items = list(self._stream_items)
        self._apply_view(count_suffix=t("main_window.scanning_suffix"))
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._stream_timer.setInterval(
            int(min(self.STREAM_MAX_INTERVAL, max(self.STREAM_MIN_INTERVAL, elapsed_ms * 3)))
        )

    def _apply_view(self, count_suffix: str = "") -> None:
        kind = self.filter_combo.currentData() or "all"
        items = media.apply_filter(self.all_items, kind, self.search.text().strip())
        sort_key = self.sort_combo.currentData() or "name"
        items = media.sort_items(
            items,
            sort_key,
            self.btn_desc.isChecked(),
            self._random_seed,
            orders.get(self.folder) if (sort_key == "custom" and self.folder) else None,
        )
        keep_row = self.tiles.current_row()
        keep_path = None
        if 0 <= keep_row < len(self.model.items):
            keep_path = self.model.items[keep_row].path
        self.model.set_items(items)
        self._restore_scroll_pos()
        n_img = sum(1 for i in items if not i.is_video)
        self.status_count.setText(
            t("main_window.item_count").format(
                count=len(items), images=n_img, videos=len(items) - n_img, suffix=count_suffix
            )
        )
        if keep_path is not None:
            for i, it in enumerate(items):
                if it.path == keep_path:
                    self.tiles.set_current_row(i)
                    break

    # -------------------------------------------------------------- viewer

    def _fs_model_for_panel(self):
        """Share the browser's directory model with the panel's browser tab."""
        self._materialize_tree()
        return self.fs_model

    def ensure_viewer(self) -> "Viewer":
        if self.viewer is None:
            from .viewer import Viewer  # imports mpv; deliberately deferred

            self.viewer = Viewer(self.thumbs, self._fs_model_for_panel)
            self.viewer.index_changed.connect(self._on_viewer_index)
            self.viewer.folder_requested.connect(self._on_panel_folder_requested)
            self.viewer.playlist_changed.connect(self._on_playlist_changed)
            self.viewer.sort_requested.connect(self._on_panel_sort_requested)
        return self.viewer

    def _on_panel_sort_requested(self, key: str, desc: bool) -> None:
        """The panel's sort combo changed; mirror it into the toolbar and re-sort."""
        idx = self.sort_combo.findData(key)
        if idx >= 0:
            self.sort_combo.blockSignals(True)
            self.sort_combo.setCurrentIndex(idx)
            self.sort_combo.blockSignals(False)
        self.btn_desc.blockSignals(True)
        self.btn_desc.setChecked(desc)
        self.btn_desc.blockSignals(False)
        self._update_desc_icon()
        self._apply_view()

    def _on_panel_folder_requested(self, folder: Path) -> None:
        """Browser tab picked a folder: rescan and hand the new list to the viewer."""
        self._pending_viewer_folder = Path(folder)
        self.set_folder(Path(folder))

    def _on_playlist_changed(self, items: list) -> None:
        """The panel reordered or trimmed the list; mirror it in the browser."""
        self.all_items = list(items)
        if self.folder is not None:
            orders.set(self.folder, [i.name for i in items])
            orders.save()
        self.model.set_items(list(items))
        self.tiles.relayout()

    def _open_viewer(self, row: int) -> None:
        if not self.model.items:
            return
        self.tiles.set_current_row(row, scroll=False)
        self.ensure_viewer().open_playlist(self.model.items, row)

    def _on_viewer_index(self, row: int) -> None:
        self.tiles.set_current_row(row)
        if self.details is not None and self.stack.currentWidget() is self.details:
            self.details.selectRow(row)

    # ------------------------------------------------------- events / DnD

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                self.set_folder(p)
                return
            if p.is_file() and media.classify(p) is not None:
                self.set_folder(p.parent)
                QTimer.singleShot(400, lambda target=p: self._open_path(target))
                return

    def _open_path(self, path: Path) -> None:
        for i, it in enumerate(self.model.items):
            if it.path == path:
                self._open_viewer(i)
                return

    def _open_recent_file(self, path) -> None:
        p = Path(path)
        if not p.is_file():
            return
        self.set_folder(p.parent)
        QTimer.singleShot(400, lambda target=p: self._open_path(target))

    # ------------------------------------------------------- navigation history

    def _push_nav_history(self, folder: Path) -> None:
        """Record a folder visit before navigating away."""
        # Trim any forward history beyond the current index
        if self._nav_index < len(self._nav_history) - 1:
            self._nav_history = self._nav_history[:self._nav_index + 1]
        # Avoid consecutive duplicates
        if not self._nav_history or self._nav_history[-1] != folder:
            self._nav_history.append(folder)
            self._nav_index = len(self._nav_history) - 1
            # Cap history to prevent memory bloat
            if len(self._nav_history) > 80:
                self._nav_history = self._nav_history[-60:]
                self._nav_index = len(self._nav_history) - 1
        self._update_nav_buttons()

    def _nav_back(self) -> None:
        """Return to the previously visited folder."""
        if self._nav_index <= 0:
            return
        self._save_scroll_pos()
        self._nav_index -= 1
        target = self._nav_history[self._nav_index]
        self._nav_from_history = True
        try:
            self.set_folder(target)
        finally:
            self._nav_from_history = False
        self._update_nav_buttons()

    def _nav_forward(self) -> None:
        """Go forward to the next folder in history."""
        if self._nav_index >= len(self._nav_history) - 1:
            return
        self._save_scroll_pos()
        self._nav_index += 1
        target = self._nav_history[self._nav_index]
        self._nav_from_history = True
        try:
            self.set_folder(target)
        finally:
            self._nav_from_history = False
        self._update_nav_buttons()

    def _update_nav_buttons(self) -> None:
        self.btn_back.setEnabled(self._nav_index > 0)
        self.btn_forward.setEnabled(self._nav_index < len(self._nav_history) - 1)

    # ------------------------------------------------------- scroll position

    def _scroll_pos_path(self):
        """Path to the persisted scroll-position file."""
        from pathlib import Path
        from .runtime import USERDATA_DIR
        return USERDATA_DIR / "scroll_pos.json"

    def _load_scroll_positions(self) -> None:
        """Restore scroll positions saved from a previous session."""
        import json
        p = self._scroll_pos_path()
        try:
            if p.exists():
                self._scroll_positions = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            self._scroll_positions = {}

    def _save_scroll_positions(self) -> None:
        """Persist scroll positions so they survive restarts."""
        import json
        if not self._scroll_positions:
            return
        p = self._scroll_pos_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._scroll_positions, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _save_scroll_pos(self) -> None:
        """Remember the current scroll position before leaving a folder."""
        if not self.folder or not settings["remember_scroll"]:
            return
        try:
            if self.details is not None and self.stack.currentWidget() is self.details:
                bar = self.details.verticalScrollBar()
                if bar:
                    self._scroll_positions[str(self.folder)] = bar.value()
            else:
                bar = self.tiles.verticalScrollBar()
                self._scroll_positions[str(self.folder)] = bar.value()
        except Exception:
            pass

    def _restore_scroll_pos(self) -> None:
        """Restore the scroll position for the current folder, if remembered."""
        if not self.folder or not settings["remember_scroll"]:
            return
        key = str(self.folder)
        pos = self._scroll_positions.get(key)
        if pos is None:
            return
        from PySide6.QtCore import QTimer
        # Defer: the view needs one paint cycle to lay out before scroll works
        QTimer.singleShot(30, lambda: self._restore_scroll_now(pos))

    def _restore_scroll_now(self, pos: int) -> None:
        try:
            if self.details is not None and self.stack.currentWidget() is self.details:
                bar = self.details.verticalScrollBar()
            else:
                bar = self.tiles.verticalScrollBar()
            if bar:
                bar.setValue(min(pos, bar.maximum()))
        except Exception:
            pass

    def closeEvent(self, e):
        self._save_state()
        self._save_scroll_positions()
        if self.viewer is not None:
            self.viewer.shutdown()
        self.thumbs.shutdown()
        media.metadata.save()
        dircache.cache.save()
        albums.save()
        orders.save()
        super().closeEvent(e)
