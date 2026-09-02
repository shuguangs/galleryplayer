"""Main browser window: toolbar, folder tree, and the three media views."""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QDir,
    QEvent,
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
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFileSystemModel,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSlider,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from . import dircache, fileops, icons, media, theme
from .albums import albums, orders
from .browser import DetailsView, MediaModel, TileView
from .config import flush, settings
from .i18n import t
from .thumbs import MAX_QUEUED_JOBS, ThumbnailCache, WARMUP_PRIO
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
        except Exception as exc:  # noqa: BLE001
            # 扫描线程意外失败也要让用户看到，而不是得到一个"空文件夹"
            stats.errors += 1
            stats.last_error = f"{self.folder}: {exc}"
        flush("done")


class MainWindow(QMainWindow):
    _drop_open = Signal(object)    # 拖放解析结论 (kind, Path)，后台线程 → UI 线程
    _ext_resolved = Signal(object)  # 第二实例外部路径解析结论 (kind, Path)
    _playlist_restored = Signal(list, int)  # 崩溃恢复：后台线程收集好的 (items, index)

    def __init__(self, startup_file: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(t("main_window.title"))
        self.resize(1360, 850)
        self._drop_open.connect(self._on_drop_resolved)
        self._ext_resolved.connect(self._on_external_resolved)
        self._playlist_restored.connect(self._on_playlist_restored)
        QTimer.singleShot(600, self._maybe_restore_playlist)
        self.setAcceptDrops(True)
        self._startup_file: Path | None = startup_file

        from . import startup_log as _slog

        _slog.stage("init", "MainWindow.__init__ 开始")
        self.thumbs = ThumbnailCache(self)
        _slog.stage("thumbs", "缩略图缓存构建完成")
        self.model = MediaModel(self)
        self.all_items: list[media.MediaItem] = []
        self.folder: Path | None = None
        self._nav_history: list[Path] = []   # 访问历史（浏览器式后退/前进）
        self._nav_index: int = -1
        self._nav_from_history: bool = False  # 标记当前导航来自历史（避免重复记录）
        self._scroll_positions: dict[str, int] = {}  # 文件夹路径 -> 滚动位置
        self._load_scroll_positions()
        self._scan_token = 0
        self._random_seed = secrets.randbits(30)  # 扫描去重种子（非安全用途）
        self._pool = QThreadPool.globalInstance()
        self._scan_signals = _ScanSignals()
        self._scan_signals.batch.connect(self._on_scan_batch)
        self._pending_viewer_folder: Path | None = None
        self._quiet_scan = False  # startup playback: browser model updates are skipped
        self._external_open_session = False  # 本次启动来自外部打开（跳过恢复弹窗）
        self._srt_timer = QTimer(self)  # monitors the resident engine's SRT job
        self._srt_timer.setInterval(500)
        self._srt_timer.timeout.connect(self._poll_srt_job)
        self._srt_active = False
        self._batch_srt_timer = QTimer(self)
        self._batch_srt_timer.setInterval(1000)
        self._batch_srt_timer.timeout.connect(self._poll_batch_srt)
        self._batch_srt_jobs: list[dict] = []
        self._archive_mode = False
        self._archive_back: Path | None = None
        self._archive_archive: Path | None = None
        self._archive_entries: list = []
        self._archive_password: str | None = None
        self._archive_tree: QTreeWidget | None = None

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
        # meta 回填引发的自动重排：锚定项保持可见（keep="visible"，最小
        # 滚动）。对齐视口顶（anchor）会每批跳一次；像素偏移（offset）在
        # 时长排序下会让视口内容大换血（新获时长的项搬家）。
        self._resort_timer.timeout.connect(
            lambda: self._apply_view(keep="visible"))
        self.thumbs.meta_ready.connect(self._on_meta_ready)

        # 定时归位：元数据排序（时长/大小/时间）下，新探到数据的项
        # 累积在错误位置（如时长 None 区）——每 RESORT_INTERVAL 秒做一次
        # keep="anchor" 的完整重排，让它们回到应在的排序位置。锚定项=
        # 视口首项（绝不用选中项：选中一个项后继续下滑浏览是常态，
        # 锚选中项会把视口弹回它的位置）。
        self._resort_interval_timer = QTimer(self)
        self._resort_interval_timer.setInterval(5000)
        self._resort_interval_timer.timeout.connect(self._periodic_resort)
        self._meta_dirty = False  # 自上次归位后是否有新 meta 到达

        # 后台预热：扫描 done 后把未缓存的项以低优先级逐批排队解码——
        # 大文件夹放着不动也能全量加载缩略图/时长（纯按需加载曾让
        # 48k 文件夹一小时只探到滚过的 5%）。视口请求（priority=行号，
        # 小值）天然插队在前台，预热不抢用户正在看的。
        self._warmup_timer = QTimer(self)
        self._warmup_timer.setInterval(500)
        self._warmup_timer.timeout.connect(self._warmup_tick)
        self._warmup_cursor = 0

        self._build_ui()
        from . import startup_log as _slog

        _slog.stage("build-ui", "工具栏/目录树/平铺视图构建完成")
        self._restore_state()
        _slog.stage("restore-state", "窗口状态恢复完成")
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
        # A media file passed on the command line ("open with") skips the browser
        # and goes straight to the player window. Deferred until the window is
        # actually shown: password / error dialogs need a visible parent.
        if self._startup_file is not None:
            QTimer.singleShot(0, self._startup_play)
        else:
            self._show_welcome()
        if bool(settings["live_model_preload"]):
            # Keep model loading off the UI thread and after first paint.
            QTimer.singleShot(1200, self._preload_live_model)

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
        self.splitter.widget(0).setMinimumWidth(300)

        self.stack = QStackedWidget()
        self.welcome = WelcomePage()
        self.welcome.open_requested.connect(self._choose_folder)
        self.welcome.folder_chosen.connect(self.set_folder)
        self.welcome.file_chosen.connect(self._open_recent_file)
        self.stack.addWidget(self.welcome)
        # The tile view is a QTableView subclass; the first one in a process costs
        # ~2s of one-time Qt item-view initialisation. It is built lazily so that
        # opening a video from the command line shows the player first, not a
        # frozen browser. Same reason the details table is deferred.
        self.tiles: TileView | None = None
        # The first QTableView in a process costs ~2s of one-time Qt item-view
        # initialisation regardless of its model, so the details table is built only
        # if the user actually asks for list view.
        self.details: DetailsView | None = None
        self.splitter.addWidget(self.stack)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([340, 1100])
        root.addWidget(self.splitter, 1)

        root.addWidget(self._build_statusbar())
        self.setCentralWidget(central)

        self.model.sort_requested.connect(self._on_header_sort)

    def _ensure_tiles(self) -> TileView:
        """Lazily build the tile view (its first construction costs ~2s)."""
        if self.tiles is None:
            self.tiles = TileView(self.model, self.thumbs)
            self.tiles.set_columns(int(settings["grid_columns"]))
            self.stack.addWidget(self.tiles)
            self.tiles.activatedRow.connect(self._open_viewer)
            self.tiles.contextRow.connect(self._media_menu)
        return self.tiles

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

        btn_archive = _icon_button(chr(0xE7B8) + "  " + t("main_window.open_archive"), t("main_window.open_archive_tip"), 112)
        btn_archive.clicked.connect(self._open_archive)
        lay.addWidget(btn_archive)

        self.btn_archive_back = _icon_button(icons.CHEVRON_LEFT, t("main_window.archive_back_tip"), 32)
        self.btn_archive_back.setVisible(False)
        self.btn_archive_back.clicked.connect(self._exit_archive)
        lay.addWidget(self.btn_archive_back)

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

        self.chk_image = QCheckBox(t("media.filter_image"))
        self.chk_video = QCheckBox(t("media.filter_video"))
        self.chk_archive = QCheckBox(t("media.filter_archive"))
        for chk in (self.chk_image, self.chk_video, self.chk_archive):
            chk.setChecked(True)
            chk.setToolTip(t("main_window.filter_check_tip"))
            # wrapped: the signal's own argument must not land in _apply_view()
            chk.toggled.connect(lambda _=False: self._apply_view())
            lay.addWidget(chk)

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
        # 防抖：每击键全量 filter+sort+模型重置（2 万条约 200ms），停顿后再应用
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_view)
        self.search.textChanged.connect(lambda *_: self._search_timer.start())
        lay.addWidget(self.search)

        self.btn_tree = _icon_button(icons.SIDEBAR, t("main_window.tree_toggle_tip"), 32, checkable=True)
        self.btn_tree.clicked.connect(self._toggle_tree)
        lay.addWidget(self.btn_tree)

        btn_tools = _icon_button(icons.TOOLS, t("main_window.tools_tip"), 32)
        btn_tools.clicked.connect(self._show_tools)
        lay.addWidget(btn_tools)

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

        # ---- quick access combo: system locations + drives, top of the tree
        from PySide6.QtCore import QStandardPaths

        loc_box = QWidget()
        ll = QVBoxLayout(loc_box)
        ll.setContentsMargins(6, 4, 6, 2)
        ll.setSpacing(4)
        self.loc_combo = icons.ArrowComboBox()
        self.loc_combo.setToolTip(t("main_window.loc_tip"))
        for kind, key in (
            (QStandardPaths.DesktopLocation, "main_window.loc_desktop"),
            (QStandardPaths.PicturesLocation, "main_window.loc_pictures"),
            (QStandardPaths.MoviesLocation, "main_window.loc_videos"),
            (QStandardPaths.MusicLocation, "main_window.loc_music"),
            (QStandardPaths.DocumentsLocation, "main_window.loc_documents"),
            (QStandardPaths.DownloadLocation, "main_window.loc_downloads"),
        ):
            p = QStandardPaths.writableLocation(kind)
            if p and Path(p).is_dir():
                self.loc_combo.addItem(t(key), str(Path(p)))
        if self.loc_combo.count():
            self.loc_combo.insertSeparator(self.loc_combo.count())
        import string

        for letter in string.ascii_uppercase:
            d = Path(f"{letter}:/")
            if d.exists():
                self.loc_combo.addItem(f"{letter}:", str(d))
        self.loc_combo.setCurrentIndex(-1)
        self.loc_combo.activated.connect(self._on_loc_chosen)
        ll.addWidget(self.loc_combo)
        self._tree_layout.addWidget(loc_box)

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

    def _show_tools(self) -> None:
        """批量工具面板：跨文件夹聚合视频，批量 SRT / 缩略图网格。"""
        from .tools_dialog import ToolsDialog

        ToolsDialog.show_for(self)

    def _show_settings(self) -> None:
        from .settings_dialog import SettingsDialog

        SettingsDialog.show_for(self)

    def _show_help(self) -> None:
        from .help_dialog import HelpDialog

        HelpDialog.show_for(self)

    # ------------------------------------------------------------- state

    def _restore_state(self) -> None:
        self.col_slider.setValue(int(settings["grid_columns"]))
        if self.tiles is not None:
            self.tiles.set_columns(int(settings["grid_columns"]))
        idx = self.sort_combo.findData(settings["sort_key"])
        self.sort_combo.setCurrentIndex(max(0, idx))
        self.btn_desc.setChecked(bool(settings["sort_desc"]))
        self._update_desc_icon()
        self.chk_image.setChecked(bool(settings["filter_show_image"]))
        self.chk_video.setChecked(bool(settings["filter_show_video"]))
        self.chk_archive.setChecked(bool(settings["filter_show_archive"]))
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
            # Old builds saved a narrow left pane (250px) that hid folder names.
            sizes = [max(300, sizes[0]), sizes[1]]
            self.splitter.setSizes(sizes)

    def _save_state(self) -> None:
        settings["grid_columns"] = self.col_slider.value()
        settings["sort_key"] = self.sort_combo.currentData()
        settings["sort_desc"] = self.btn_desc.isChecked()
        settings["filter_show_image"] = self.chk_image.isChecked()
        settings["filter_show_video"] = self.chk_video.isChecked()
        settings["filter_show_archive"] = self.chk_archive.isChecked()
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

    def _on_loc_chosen(self, index: int) -> None:
        d = self.loc_combo.itemData(index)
        if d:
            self.set_folder(Path(d))

    def _open_archive(self, path: Path | None = None) -> None:
        """Browse a compressed archive in the main view (like a zip program)."""
        if path is None:
            start = str(self.folder) if self.folder else ""
            picked, _ = QFileDialog.getOpenFileName(
                self,
                t("main_window.open_archive"),
                start,
                t("main_window.archive_filter"),
            )
            if not picked:
                return
            path = Path(picked)
        self._enter_archive(path)

    # --------------------------------------------------------- archive mode

    def _enter_archive(self, path: Path) -> None:
        """Open an archive the way a zip program does: show its folder tree in the
        side panel, list the media of the selected folder, and extract members on
        demand when a folder is browsed or a file is played."""
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        from .archive import cache_dir, list_archive

        password: str | None = None
        entries, err = list_archive(path, None)
        while err == "password":
            pwd, ok = QInputDialog.getText(
                self, t("archive.password_title"), t("archive.password_prompt"),
                QLineEdit.Password,
            )
            if not ok:
                return
            password = pwd
            entries, err = list_archive(path, password)
        if err == "no7z":
            QMessageBox.warning(self, t("archive.title_suffix"), t("archive.no7z"))
            return
        if err:
            QMessageBox.warning(
                self, t("archive.title_suffix"),
                t("archive.error").format(error=err),
            )
            return
        if not any(e.is_media for e in entries):
            QMessageBox.information(self, t("archive.title_suffix"), t("archive.empty"))
            return

        # 已在浏览另一个压缩包：换成新包（原实现直接 return，包中包/拖入第二个
        # 压缩包时静默无反应），但"返回"仍回到最初进包前的那个文件夹
        back = self._archive_back if self._archive_mode else self.folder
        if self._archive_mode:
            self._leave_archive_state()
        # 作废进行中的目录扫描：否则后台 done 批次落地时会用文件夹内容覆盖压缩
        # 包视图（token 匹配 + 无 stop_check 双重失效）。必须放在上面所有校验
        # 之后——密码取消/损坏包/包内无媒体时提前 return，扫描已被作废却没人
        # 恢复，文件列表会永久停在半截结果
        self._scan_token += 1
        self._streaming = False
        self._stream_items = []
        self._stream_timer.stop()
        self._archive_back = back
        self._archive_archive = path
        self._archive_password = password
        self._archive_entries = entries
        self._archive_mode = True
        self.btn_archive_back.setVisible(True)
        self._materialize_archive_tree(entries)
        self._show_archive_dir("")
        self.setWindowTitle(t("main_window.archive_title").format(name=path.stem))
        self.status_path.setText(str(path))

    def _materialize_archive_tree(self, entries: list) -> None:
        """Build (or rebuild) the archive's folder tree in the side panel."""
        from PySide6.QtWidgets import QTreeWidgetItem

        if self._archive_tree is None:
            self._archive_tree = QTreeWidget()
            self._archive_tree.setHeaderHidden(True)
            self._archive_tree.setIndentation(14)
            self._archive_tree.itemClicked.connect(self._on_archive_item)
            self._tree_layout.addWidget(self._archive_tree)
        self._archive_tree.clear()
        # all parent folders of every member (media or not)
        dirs: set[str] = set()
        for e in entries:
            parts = [p for p in e.name.replace("\\", "/").split("/") if p]
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
        nodes: dict[str, object] = {"": self._archive_tree.invisibleRootItem()}
        for d in sorted(dirs):
            cur = ""
            parent = ""
            for i, part in enumerate(d.split("/")):
                parent = cur
                cur = "/".join(d.split("/")[: i + 1])
                if cur not in nodes:
                    item = QTreeWidgetItem(nodes[parent])
                    item.setText(0, part)
                    item.setData(0, Qt.UserRole, cur)
                    nodes[cur] = item
        if self.tree is not None:
            self.tree.setVisible(False)
        self._archive_tree.setVisible(True)

    def _on_archive_item(self, item, _col: int = 0) -> None:
        rel = item.data(0, Qt.UserRole)
        if rel is not None:
            self._show_archive_dir(str(rel))

    def _show_archive_dir(self, rel_dir: str) -> None:
        """List the media directly inside `rel_dir` of the open archive,
        extracting just those files to the cache."""
        from PySide6.QtWidgets import QInputDialog

        from .archive import cache_dir, extract_member

        if not self._archive_mode or self._archive_archive is None:
            return
        dest = cache_dir() / self._archive_archive.stem
        prefix = rel_dir + "/" if rel_dir else ""
        items: list[media.MediaItem] = []
        self.status_count.setText(t("archive.extracting"))
        prompted = False
        for e in self._archive_entries:
            if not e.is_media:
                continue
            name = e.name.replace("\\", "/")
            if prefix and not name.startswith(prefix):
                continue
            rest = name[len(prefix):] if prefix else name
            if "/" in rest:
                continue  # belongs to a subfolder that has its own tree node
            try:
                f = extract_member(
                    self._archive_archive, name, dest, self._archive_password
                )
            except RuntimeError as exc:
                if str(exc) == "password":
                    if prompted:
                        continue  # 只问一次：取消/输错后不再逐个成员弹密码框
                    prompted = True
                    pwd, ok = QInputDialog.getText(
                        self, t("archive.password_title"), t("archive.password_prompt"),
                        QLineEdit.Password,
                    )
                    if not ok:
                        continue
                    self._archive_password = pwd
                    try:
                        f = extract_member(self._archive_archive, name, dest, pwd)
                    except Exception:  # noqa: BLE001
                        continue
                else:
                    continue
            except Exception:  # noqa: BLE001
                continue
            item = media.item_for_path(f)
            if item is not None:
                items.append(item)
        self.all_items = items
        self.model.set_items(items)
        if self.tiles is not None:
            self.tiles.set_current_row(0, scroll=False)
        self._show_browser()
        if self.tiles is not None:
            self.tiles.thumbs_suspended = bool(settings["archive_no_thumbs"])
        self.status_count.setText(
            t("main_window.item_count").format(
                count=len(items),
                images=sum(1 for i in items if not i.is_video),
                videos=sum(1 for i in items if i.is_video),
                suffix="",
            )
        )

    def _leave_archive_state(self) -> None:
        """Swap the archive tree back to the real folder tree (no navigation)."""
        self._archive_mode = False
        if self.tiles is not None:
            self.tiles.thumbs_suspended = False
        self._archive_archive = None
        self._archive_back = None
        self._archive_entries = []
        self._archive_password = None
        if self._archive_tree is not None:
            self._archive_tree.setVisible(False)
            self._archive_tree.clear()
        if self.tree is not None:
            self.tree.setVisible(True)
        self.btn_archive_back.setVisible(False)

    def _exit_archive(self) -> None:
        """Leave archive browsing back to the folder we came from."""
        if not self._archive_mode:
            return
        back = self._archive_back
        self._leave_archive_state()
        if back is None:
            # 没有"来处"（启动后直接打开压缩包）：不能只切树，网格里还留着包内
            # 解压出来的文件、标题也还是压缩包名
            self.model.set_items([])
            self.all_items = []
            self._show_welcome()
            return
        # force：back 常常就等于 self.folder（进包时 folder 没变），不强制会被
        # set_folder 的"同一目录直接 return"挡掉，网格仍显示包内文件
        self.set_folder(back, force=True)

    def _preload_live_model(self) -> None:
        """Silently warm the subtitle engine without blocking the browser UI."""
        from . import live_engine
        from . import startup_log

        startup_log.stage("preload", "模型预载开始")
        live_engine.start_preload()
        startup_log.stage("preload", "模型预载提交完成（引擎在后台加载）")

    def _gen_srt_for(self, path: Path) -> None:
        """后台生成 SRT 字幕：复用常驻模型，不打开播放器。

        弹出进度窗口实时显示识别/翻译日志；完成后系统通知 + 可一键打开文件夹。
        """
        from PySide6.QtWidgets import (
            QDialog,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QVBoxLayout,
        )

        from . import live_engine
        from .config import settings
        from .runtime import APP_DIR

        if self._srt_active or getattr(self, "_batch_srt_jobs", None):
            # 引擎的 control 槽只保留最新任务：单文件与批量混发会互相覆盖
            QMessageBox.information(self, t("main_window.gen_srt"), t("main_window.gen_srt_busy"))
            return

        if live_engine.paths() is None:
            QMessageBox.warning(
                self, t("main_window.gen_srt"), t("main_window.gen_srt_no_pipeline")
            )
            return

        # 保存位置：视频所在文件夹 / 播放器所在文件夹
        fmt = str(settings["srt_export_format"] or "srt")
        if settings["subtitle_save_dir"] == "player":
            srt = APP_DIR / f"{path.stem}.zh.{fmt}"
        else:
            srt = path.with_suffix(f".zh.{fmt}")
        job_log = live_engine.paths()[0].parent / "srt-generation.log"
        job_log.unlink(missing_ok=True)

        # ---- 进度窗口 ----
        dlg = QDialog(self)
        dlg.setWindowTitle(t("main_window.gen_srt"))
        dlg.resize(560, 360)
        lay = QVBoxLayout(dlg)
        self._srt_log = QPlainTextEdit(dlg)
        self._srt_log.setReadOnly(True)
        from . import theme

        self._srt_log.setStyleSheet(
            f"QPlainTextEdit {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:4px;"
            f" font-family:Consolas; font-size:11px; }}"
        )
        lay.addWidget(self._srt_log)
        self._srt_status = QLabel(t("main_window.gen_srt_running"), dlg)
        lay.addWidget(self._srt_status)
        self._srt_cancel_btn = QPushButton(t("main_window.gen_srt_cancel"), dlg)
        self._srt_cancel_btn.clicked.connect(self._cancel_srt_job)
        lay.addWidget(self._srt_cancel_btn)
        dlg.setModal(False)
        dlg.rejected.connect(self._srt_dialog_closed)
        dlg.show()
        self._srt_dialog = dlg

        if not live_engine.start_preload():
            self._finish_srt_job(srt, t("main_window.gen_srt_no_pipeline"))
            return
        generation = live_engine.submit({
            "mode": "srt",
            "media": str(path),
            "output": str(srt),
            "log": str(job_log),
            "seek": 0.0,
            "translate_model": str(settings["srt_translate_model"] or "live"),
            "format": fmt,
            # 人声降噪随任务下发（设置可关；模型缺失引擎侧自动下载）
            "denoise": "on" if bool(settings["srt_denoise"]) else "off",
            # 场景随任务下发（与 translate_model 同等）：引擎不必为改场景重建，
            # 也不会在 start_preload 的 30s 冷却窗口里悄悄用上一次的场景
            "scenario": str(settings["translate_scenario"]),
        })
        if not generation:
            self._finish_srt_job(srt, t("main_window.gen_srt_no_pipeline"))
            return

        self._srt_active = True
        live_engine.srt_busy = True  # 实时字幕暂停提交（互斥）
        self._srt_generation = generation
        self._srt_output = srt
        self._srt_job_log = job_log
        self._srt_job_log_pos = 0
        self._srt_job_log_tail = ""  # 上一个任务的尾部残留会造成误判完成
        self._srt_timer.start()

    def _cancel_srt_job(self) -> None:
        """取消进行中的 SRT 任务（引擎在检查点退出，写 SRT_CANCELLED）。"""
        from . import live_engine

        if getattr(self, "_srt_active", False):
            live_engine.submit({"mode": "cancel", "media": "", "output": "",
                                "log": "", "seek": 0.0})

    def _srt_dialog_closed(self) -> None:
        """关闭窗口即取消后台任务（否则没有轮询者收尾 srt_busy 互斥）。"""
        self._srt_timer.stop()
        if getattr(self, "_srt_active", False):
            from . import live_engine

            live_engine.submit({"mode": "cancel", "media": "", "output": "",
                                "log": "", "seek": 0.0})
            # 轮询者已停，引擎之后写出的 CANCELLED 无人读取——必须在这里
            # 解除互斥，否则实时字幕被 srt_busy 永久卡住
            live_engine.srt_busy = False
        self._srt_active = False

    def _poll_srt_job(self) -> None:
        if not self._srt_active or not hasattr(self, "_srt_log"):
            return
        log_path = getattr(self, "_srt_job_log", None)
        if log_path is None or not log_path.is_file():
            return
        try:
            with open(log_path, "r", encoding="utf-8") as fp:
                fp.seek(getattr(self, "_srt_job_log_pos", 0))
                new = fp.read()
                self._srt_job_log_pos = fp.tell()
        except Exception:
            return
        if not new:
            return
        self._srt_log.appendPlainText(new.strip())
        # 拼上次尾部：状态行可能恰好被读到一半（跨两次轮询），截断后匹配不上
        buf = getattr(self, "_srt_job_log_tail", "") + new
        self._srt_job_log_tail = buf[-64:]
        for line in buf.splitlines():
            line = line.strip()
            if line.startswith("# SRT_READY "):
                self._finish_srt_job(getattr(self, "_srt_output", Path()))
                return
            if line == "# SRT_CANCELLED":
                self._finish_srt_job(getattr(self, "_srt_output", Path()),
                                     error=t("main_window.gen_srt_cancelled"),
                                     cancelled=True)
                return
            if line.startswith("# SRT_ERROR "):
                error = line.removeprefix("# SRT_ERROR ").strip() or t("main_window.gen_srt_fail").format(error="")
                self._finish_srt_job(getattr(self, "_srt_output", Path()), error)
                return

    def _finish_srt_job(self, srt: Path, error: str = "",
                        cancelled: bool = False) -> None:
        from PySide6.QtWidgets import QDialogButtonBox

        from . import live_engine
        from .fileops import reveal

        self._srt_timer.stop()
        self._srt_active = False
        live_engine.srt_busy = False  # 解除互斥，实时字幕恢复提交
        dlg = getattr(self, "_srt_dialog", None)
        if dlg is None:
            return

        btn = dlg.findChild(QPushButton)
        if btn is not None:
            btn.setEnabled(False)  # 取消按钮失效（任务已结束）
        bb = QDialogButtonBox(dlg)
        if cancelled:
            self._srt_status.setText(t("main_window.gen_srt_cancelled"))
            self._srt_status.setStyleSheet(f"color:{theme.TEXT_DIM if False else '#5dc0f0'};")
            self._notify(t("main_window.gen_srt"), t("main_window.gen_srt_cancelled"))
        elif not error and srt.is_file():
            self._srt_status.setText(t("main_window.gen_srt_done").format(path=srt.name))
            self._srt_status.setStyleSheet("color:#5dc0f0;")
            open_btn = bb.addButton(t("main_window.gen_srt_open_folder"), QDialogButtonBox.ActionRole)
            open_btn.clicked.connect(lambda: reveal(srt))
            self._notify(t("main_window.gen_srt"),
                         t("main_window.gen_srt_notify_done").format(path=srt.name))
        else:
            self._srt_status.setText(t("main_window.gen_srt_fail").format(error=error[:300]))
            self._srt_status.setStyleSheet("color:#e0653f;")
            self._notify(t("main_window.gen_srt"),
                         t("main_window.gen_srt_notify_fail").format(error=error[:120]))
        bb.addButton(QDialogButtonBox.Close).clicked.connect(dlg.accept)
        dlg.layout().addWidget(bb)
        dlg.adjustSize()

    def _notify(self, title: str, text: str) -> None:
        """Windows 系统通知（隐藏托盘图标承载；失败静默）。"""
        try:
            from PySide6.QtWidgets import QSystemTrayIcon

            if not hasattr(self, "_tray"):
                self._tray = QSystemTrayIcon(self)  # 无专属图标资源，用系统默认
                self._tray.show()
            self._tray.showMessage(title, text, QSystemTrayIcon.Information, 6000)
        except Exception:
            pass

    def _pick_videos_for_srt(self) -> None:
        """勾选多个视频 → 走批量生成队列。"""
        from PySide6.QtWidgets import (
            QDialog,
            QListWidget,
            QListWidgetItem,
            QPushButton,
            QVBoxLayout,
        )

        videos = [item for item in self.all_items if item.is_video]
        if not videos:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(t("main_window.gen_srt_pick"))
        dlg.resize(460, 480)
        lay = QVBoxLayout(dlg)
        lst = QListWidget(dlg)
        for item in videos:
            wi = QListWidgetItem(item.name)
            wi.setCheckState(Qt.Unchecked)
            wi.setData(Qt.UserRole, item)
            lst.addItem(wi)
        lay.addWidget(lst)

        btns = QHBoxLayout()
        all_btn = QPushButton(t("main_window.gen_srt_pick_all"), dlg)
        all_btn.clicked.connect(lambda: [lst.item(i).setCheckState(Qt.Checked)
                                         for i in range(lst.count())])
        none_btn = QPushButton(t("main_window.gen_srt_pick_none"), dlg)
        none_btn.clicked.connect(lambda: [lst.item(i).setCheckState(Qt.Unchecked)
                                          for i in range(lst.count())])
        btns.addWidget(all_btn)
        btns.addWidget(none_btn)
        btns.addStretch(1)
        ok_btn = QPushButton(t("settings.done"), dlg)
        btns.addWidget(ok_btn)
        lay.addLayout(btns)

        def _confirm() -> None:
            picked = [lst.item(i).data(Qt.UserRole)
                      for i in range(lst.count())
                      if lst.item(i).checkState() == Qt.Checked]
            dlg.accept()
            if picked:
                self._batch_generate_srt(picked)

        ok_btn.clicked.connect(_confirm)
        dlg.exec()

    def _batch_generate_srt(self, videos: list | None = None) -> None:
        """Queue videos through the resident engine（默认当前文件夹全部）。

        引擎的 control 槽只保留最新 generation——逐个懒提交（完成一个再提交
        下一个），否则前面的任务会被覆盖，实际只转写最后一个视频。
        """
        from PySide6.QtWidgets import QDialog, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout

        from . import live_engine
        from .config import settings
        from .runtime import APP_DIR

        if self._batch_srt_jobs or getattr(self, "_srt_active", False):
            # 同上：批量与单文件互斥，避免 control 槽互相覆盖导致先者永久挂起
            QMessageBox.information(self, t("main_window.gen_srt"), t("main_window.gen_srt_busy"))
            return
        if videos is None:
            videos = [item for item in self.all_items if item.is_video]
        if not videos:
            return
        if live_engine.paths() is None or not live_engine.start_preload():
            QMessageBox.warning(self, t("main_window.gen_srt"), t("main_window.gen_srt_no_pipeline"))
            return

        engine_dir = live_engine.paths()[0].parent
        fmt = str(settings["srt_export_format"] or "srt")
        jobs: list[dict] = []
        for index, item in enumerate(videos):
            output = (APP_DIR if settings["subtitle_save_dir"] == "player"
                      else item.path.parent) / f"{item.path.stem}.zh.{fmt}"
            job_log = engine_dir / f"srt-batch-{index}.log"
            job_log.unlink(missing_ok=True)
            jobs.append({
                "name": item.name,
                "path": item.path,
                "output": output,
                "log": job_log,
                "pos": 0,
                "tail": "",
                "done": False,
                "generation": 0,  # 懒提交：轮到它时才 submit
                "format": fmt,
            })

        dlg = QDialog(self)
        dlg.setWindowTitle(t("main_window.gen_srt"))
        dlg.resize(620, 380)
        lay = QVBoxLayout(dlg)
        self._batch_srt_status = QLabel(
            t("main_window.gen_srt_batch_running").format(done=0, total=len(jobs)), dlg
        )
        lay.addWidget(self._batch_srt_status)
        self._batch_srt_log = QPlainTextEdit(dlg)
        self._batch_srt_log.setReadOnly(True)
        from . import theme

        self._batch_srt_log.setStyleSheet(
            f"QPlainTextEdit {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:4px;"
            f" font-family:Consolas; font-size:11px; }}"
        )
        lay.addWidget(self._batch_srt_log)
        batch_cancel = QPushButton(t("main_window.gen_srt_cancel"), dlg)
        batch_cancel.clicked.connect(self._batch_cancel_srt)
        lay.addWidget(batch_cancel)
        self._batch_cancel_btn = batch_cancel
        dlg.setModal(False)
        dlg.rejected.connect(self._batch_cancel_srt)  # 关窗=取消（保留已完成的）
        dlg.show()
        self._batch_srt_dialog = dlg
        self._batch_srt_jobs = jobs
        live_engine.srt_busy = True
        self._batch_srt_timer.start()

    def _batch_cancel_srt(self) -> None:
        """取消批量：终止当前任务并丢弃队列剩余。"""
        from . import live_engine

        for job in getattr(self, "_batch_srt_jobs", []):
            if not job["done"]:
                job["done"] = True  # 队列剩余不再提交
        if getattr(self, "_batch_active_gen", 0):
            live_engine.submit({"mode": "cancel", "media": "", "output": "",
                                "log": "", "seek": 0.0})
        # 代次也必须归零：懒提交的第一步是 `if not active_gen`，留着上一批的
        # 陈旧代次会让下一次批量永远停在 0/N（一个任务都不提交），而
        # srt_busy 已被置 True，实时字幕跟着被永久互斥
        self._batch_active_gen = 0
        self._batch_srt_timer.stop()
        live_engine.srt_busy = False
        self._batch_srt_jobs = []  # 清队列，否则下次批量永远被"已有任务"守卫拒绝
        self._batch_srt_status.setText(t("main_window.gen_srt_cancelled"))
        btn = getattr(self, "_batch_cancel_btn", None)
        if btn is not None:
            btn.setEnabled(False)

    def _poll_batch_srt(self) -> None:
        jobs = getattr(self, "_batch_srt_jobs", [])
        if not jobs:
            self._batch_srt_timer.stop()
            return
        from . import live_engine

        # 懒提交：无进行中任务且队列还有 → 提交下一个
        active_gen = getattr(self, "_batch_active_gen", 0)
        if not active_gen:
            nxt = next((j for j in jobs if not j["done"] and not j["generation"]), None)
            if nxt is not None:
                generation = live_engine.submit({
                    "mode": "srt",
                    "media": str(nxt["path"]),
                    "output": str(nxt["output"]),
                    "log": str(nxt["log"]),
                    "seek": 0.0,
                    "translate_model": str(settings["srt_translate_model"] or "live"),
                    "format": str(nxt.get("format", "srt")),
                    "denoise": "on" if bool(settings["srt_denoise"]) else "off",
                    "scenario": str(settings["translate_scenario"]),
                })
                if generation:
                    nxt["generation"] = generation
                    self._batch_active_gen = generation
                    nxt["pos"] = 0
                    nxt["tail"] = ""

        for job in jobs:
            if not job["generation"] or job["done"] or not job["log"].is_file():
                continue
            try:
                with open(job["log"], "r", encoding="utf-8") as fp:
                    fp.seek(job["pos"])
                    new = fp.read()
                    job["pos"] = fp.tell()
            except Exception:
                continue
            if new:
                self._batch_srt_log.appendPlainText(f"{job['name']}\n{new.strip()}")
            # 结束检测只扫新增文本（拼上次尾部防止状态行被读到一半截断）。
            # 旧实现每秒全量重读整份日志，长任务数十 MB 会持续拖慢 UI
            text = job.get("tail", "") + new
            job["tail"] = text[-64:]
            finished = ("# SRT_READY " in text or "# SRT_ERROR " in text
                        or "# SRT_CANCELLED" in text)
            if finished:
                job["done"] = True
                if self._batch_active_gen == job["generation"]:
                    self._batch_active_gen = 0
                if "# SRT_READY " in text:
                    self._notify(t("main_window.gen_srt"),
                                 t("main_window.gen_srt_notify_done").format(path=job["output"].name))
                elif "# SRT_ERROR " in text:
                    self._notify(t("main_window.gen_srt"),
                                 t("main_window.gen_srt_notify_fail").format(error=job["name"]))

        done = sum(1 for job in jobs if job["done"])
        total = len(jobs)
        self._batch_srt_status.setText(
            t("main_window.gen_srt_batch_running").format(done=done, total=total)
        )
        if done == total:
            self._batch_srt_timer.stop()
            live_engine.srt_busy = False
            self._batch_active_gen = 0
            self._batch_srt_jobs = []  # 同上：清队列允许下一次批量
            btn = getattr(self, "_batch_cancel_btn", None)
            if btn is not None:
                btn.setEnabled(False)
            self._notify(t("main_window.gen_srt"),
                         t("main_window.gen_srt_batch_done").format(done=done, total=total))

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
        from . import startup_log as _slog

        _slog.stage("welcome", "欢迎页刷新开始")
        self.welcome.refresh_recent()
        self.stack.setCurrentWidget(self.welcome)
        self.col_slider.setVisible(False)
        self.col_label.setVisible(False)
        self.status_path.setText(t("main_window.no_folder"))
        self.status_count.setText("")
        self.setWindowTitle(t("main_window.title"))
        _slog.stage("welcome", "欢迎页就绪")

    def _startup_play(self) -> None:
        """A file was passed on the command line ("open with").

        Media files: show the player window immediately with a single-item
        playlist instead of building the whole browser first (the first tile
        view costs ~2s); the folder is scanned in the background and the full
        list is handed to the player when the scan reports done.
        Archives: open the archive browser so the user can pick what to play.
        """
        target = self._startup_file
        if target is None or not target.is_file():
            self._show_welcome()
            return
        # 本次启动是外部打开：600ms 后的崩溃恢复弹窗跳过（用户意图明确，
        # 模态框此刻弹出只会挡住刚打开的播放器）。独立于 _startup_file——
        # 缓存命中时扫描会在 600ms 内 done 并把它消费成 None。
        self._external_open_session = True
        from .archive import is_archive

        if is_archive(target):
            self._enter_archive(target)
            return
        item = media.item_for_path(target)
        if item is None:
            # 「打开方式」可以指到任意文件（txt/mp3 等非图片非视频）：不校验会在
            # open_playlist 里 AttributeError，主窗口停在空白页且毫无提示
            self._show_welcome()
            self.status_path.setText(
                t("main_window.unsupported_file").format(name=target.name))
            return
        self.ensure_viewer().open_playlist([item], 0)
        # quiet: the player window is already up; scanning the folder in the
        # background must not switch the UI to (and build) the browser.
        self.set_folder(target.parent, quiet=True)

    def handle_external_paths(self, paths) -> None:
        """Files/folders forwarded by a second launch (single-instance mode).

        Play the media file right away in this window and scan its folder in
        the background, mirroring the command-line startup path; raise the
        window so the user sees the result.
        """
        self.show()
        self.raise_()
        self.activateWindow()
        print(f"[single-instance] external paths: {list(paths)}")

        def _work() -> None:
            # 所有 stat 都在 worker 线程：网络路径上一个 stat 可能耗时数秒
            for raw in paths:
                p = Path(raw)
                try:
                    if p.is_dir():
                        self._ext_resolved.emit(("folder", p))
                        return
                    if not p.is_file():
                        continue
                except OSError:
                    continue
                from .archive import is_archive

                if is_archive(p):
                    self._ext_resolved.emit(("archive", p))
                    return
                if media.item_for_path(p) is not None:
                    self._ext_resolved.emit(("media", p))
                    return
            self._ext_resolved.emit((None, None))

        import threading

        threading.Thread(target=_work, daemon=True).start()

    def _on_external_resolved(self, decision) -> None:
        kind, p = decision
        if kind == "folder":
            self.set_folder(p)
        elif kind == "archive":
            self._open_archive(p)
        elif kind == "media":
            item = media.item_for_path(p)
            if item is None:
                return
            self.ensure_viewer().open_playlist([item], 0)
            # 与命令行首启路径（_startup_play）同构：记录外部打开的文件，
            # 文件夹扫描 done 后 _handle_scan_batch 凭它把完整列表交给
            # viewer（extend_playlist，不打断播放）。漏了这一步，播放列表
            # 会永远停在单文件——quiet 扫描的结果没人接收。
            self._startup_file = p
            # force：多半是同文件夹的另一个文件（self.folder 已是它），
            # set_folder 对同文件夹会短路返回——不强制重扫就没有 done，
            # _startup_file 永远无人消费，列表停在单文件。authoritative
            # 扫描层有 dircache 复用，重扫代价很小。
            self.set_folder(p.parent, quiet=True, force=True)

    def _maybe_restore_playlist(self) -> None:
        """上次会话未正常退出 → 询问是否恢复播放列表（路径收集在后台线程）。"""
        from .runtime import USERDATA_DIR, automation_mode
        from . import startup_log

        startup_log.stage("restore-prompt", "开始检查 last_playlist")
        # 外部打开（命令行/双击/转发）时用户意图明确：直接播给定的文件。
        # 恢复弹窗是模态的，此刻弹出来只会挡住刚打开的播放器。
        if self._startup_file is not None or self._external_open_session:
            startup_log.stage("restore-prompt", "跳过（外部打开会话）")
            return

        try:
            data = json.loads(
                (USERDATA_DIR / "last_playlist.json").read_text(encoding="utf-8"))
            paths = [p for p in (data.get("paths") or []) if p]
            index = int(data.get("index", 0))
        except Exception:
            return
        if not isinstance(data, dict) or data.get("clean", True):
            return
        if not paths:
            return
        if automation_mode():
            # 自动化模式：不弹模态框（会把无人值守的脚本卡死），按"不恢复"
            # 处理并清掉脏标记，等价于用户点了"否"。
            self._clear_restore_flag()
            return
        ret = QMessageBox.question(
            self, t("main_window.title"),
            t("main_window.restore_playlist").format(n=len(paths)),
            QMessageBox.Yes | QMessageBox.No,
        )
        startup_log.stage("restore-prompt",
                          f"用户选择: {'恢复' if ret == QMessageBox.Yes else '不恢复'}")
        if ret != QMessageBox.Yes:
            # 拒绝恢复也要清掉脏标记，否则之后每次启动都会再弹
            self._clear_restore_flag()
            return
        # 选“是”同样消费掉脏标记：恢复是一次性动作。之后再次异常退出时，
        # open_playlist 落盘的 clean=False 会重新计——但只要本次恢复过，
        # 就不会出现“反复弹同一个恢复框”的观感（断点位置另有 resume.json）。
        try:
            data["clean"] = True
            (USERDATA_DIR / "last_playlist.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        import threading

        def _collect() -> None:
            items = [it for it in
                     (media.item_for_path(Path(p)) for p in paths)
                     if it is not None]
            self._playlist_restored.emit(items, index)

        threading.Thread(target=_collect, daemon=True).start()

    @staticmethod
    def _clear_restore_flag() -> None:
        """消费掉"未正常退出"脏标记，避免下次启动继续弹恢复框。"""
        from .runtime import USERDATA_DIR

        try:
            (USERDATA_DIR / "last_playlist.json").write_text(
                json.dumps({"clean": True, "index": 0, "paths": []}),
                encoding="utf-8")
        except Exception:
            pass

    def _on_playlist_restored(self, items, index: int) -> None:
        if not items:
            return
        cur = min(max(0, index), len(items) - 1)
        self.ensure_viewer().open_playlist(items, cur)
        # 恢复的列表多半是上次会话的片段（强杀时可能只剩 1 条）：借 startup
        # handoff 让扫描 done 后 extend_playlist 把同文件夹完整列表交给
        # viewer（正在播的就是 _startup_file → 原地扩展，不打断播放）。
        # 浏览器同样需要扫描填充（否则主页面网格是空的，要点别的文件夹
        # 再点回来才恢复）。quiet：不把播放器界面切走。
        startup = items[cur].path
        folder = startup.parent
        if folder != self.folder:
            self._startup_file = startup
            self.set_folder(folder, quiet=True, force=True)

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
            self._ensure_tiles().set_mode(mode)
            self.stack.setCurrentWidget(self.tiles)
        else:
            self.stack.setCurrentWidget(self._ensure_details())

    def _on_columns_changed(self, n: int) -> None:
        if self.tiles is not None:
            self.tiles.set_columns(n)

    def _on_sort_changed(self) -> None:
        self._update_desc_icon()
        if self.sort_combo.currentData() == "random":
            self._random_seed = secrets.randbits(30)
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

    def _sync_tree_now(self, folder: Path, retries: int = 6) -> None:
        if self.tree is None or self.fs_model is None or folder != self.folder:
            return
        # Expand every ancestor from the drive down to the folder: setCurrentIndex
        # alone leaves the chain collapsed, and a selection on an invisible row
        # reads as "no highlight" even though the model has it selected.
        parts = list(folder.parts)
        if len(parts) < 2:
            return
        cur = Path(folder.anchor)
        idx = None
        for comp in parts[1:]:
            cur = cur / comp
            idx = self.fs_model.index(str(cur))
            if not idx.isValid():
                # A network share or sleeping disk may still be resolving its
                # listing; try again shortly instead of giving up silently.
                if retries > 0:
                    QTimer.singleShot(150, lambda: self._sync_tree_now(folder, retries - 1))
                return
            self.tree.expand(idx)
        if idx is None:
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
        self._meta_dirty = True
        if self.sort_combo.currentData() == "duration":
            self._resort_timer.start()

    def _periodic_resort(self) -> None:
        """元数据排序下的定时归位：新探到时长/大小的项回到排序位置。

        只锚定视口首项（用户看着的内容）——绝不用"选中项"做锚：正常
        使用中选中一个项继续下滑浏览，归位若锚定选中项会把视口弹回
        它的位置（实测体验差）。选中态本身按路径保留，不受重排影响。
        """
        if not self._meta_dirty:
            return
        sort_key = self.sort_combo.currentData() or "name"
        if sort_key not in ("duration", "mtime", "size"):
            self._meta_dirty = False
            return
        if self.tiles is None or not self.model.items:
            return
        self._meta_dirty = False
        # keep="anchor" 的锚取视口首项（set_items_keep_scroll 语义）
        self._apply_view(keep="anchor")

    # ------------------------------------------------------- context menus

    def _media_menu(self, row: int, global_pos) -> None:
        # 右键落在选中项上 → 多选菜单；否则先选中该行（桌面语义）
        tiles = getattr(self, "tiles", None)
        if tiles is not None and hasattr(tiles, "selected_rows"):
            sel = tiles.selected_rows()
            if sel and row in sel and len(sel) > 1:
                menu = self._build_multi_menu(sel)
                if menu.actions():
                    menu.exec(global_pos)
                return
            if row >= 0 and row not in sel:
                tiles.clear_selection()
        menu = self.build_media_menu(row)
        if menu.actions():
            menu.exec(global_pos)

    def _open_thumbgrid_dialog(self, videos: list) -> None:
        """打开缩略图网格参数配置（多选视频批量生成）。"""
        from .thumb_grid_dialog import ThumbGridDialog
        from .thumb_grid_progress import ThumbGridProgressDialog

        videos = [Path(v) for v in videos if Path(v).is_file()]
        if not videos:
            return
        dlg = ThumbGridDialog(videos, self)
        dlg.startRequested.connect(
            lambda vs, out_dir, width, fmt, quality, opts:
                ThumbGridProgressDialog(
                    vs, out_dir, width, fmt, quality, options=opts,
                    on_exists=dlg.cb_exists.currentData(), parent=self).show()
        )
        dlg.exec()

    def _open_selection_playlist(self, rows: tuple) -> None:
        """多选打开：把选中项作为独立播放列表在播放器中打开。

        单选时退化为普通打开（沿用浏览器全列表+定位，保持断点续播等
        语义）；多选时列表=仅这 N 项（用户明确圈定的播放范围），从
        第一个开始播。
        """
        items = [self.model.item(r) for r in rows]
        items = [i for i in items if i is not None and not i.is_archive]
        if not items:
            return
        if len(items) == 1:
            # 单选：普通打开（全文件夹列表）
            for i, it in enumerate(self.model.items):
                if it is items[0]:
                    self._open_viewer(i)
                    return
            return
        self.ensure_viewer().open_playlist(items, 0)

    def _build_multi_menu(self, rows: list[int]) -> QMenu:
        """多选右键菜单：批量操作作用于全部选中项。"""
        menu = QMenu(self)
        items = [self.model.item(r) for r in rows]
        items = [i for i in items if i is not None]
        if not items:
            return menu
        paths = [i.path for i in items]
        n = len(paths)

        menu.addAction(
            t("main_window.multi_open").format(n=n)).triggered.connect(
            lambda _=False, r=tuple(rows): self._open_selection_playlist(r))
        menu.addSeparator()
        menu.addAction(
            t("main_window.multi_copy_path").format(n=n)).triggered.connect(
            lambda _=False, ps=tuple(paths): fileops.copy_to_clipboard(
                "\n".join(str(p) for p in ps)))
        menu.addAction(
            t("main_window.multi_copy_name").format(n=n)).triggered.connect(
            lambda _=False, ps=tuple(paths): fileops.copy_to_clipboard(
                "\n".join(p.name for p in ps)))
        menu.addAction(
            t("main_window.multi_copy_file").format(n=n)).triggered.connect(
            lambda _=False, ps=tuple(paths): fileops.copy_files_to_clipboard(list(ps)))
        menu.addSeparator()
        video_paths = [p for p in paths if p.suffix.lower() in media.VIDEO_EXTS]
        if video_paths:
            menu.addAction(
                t("main_window.multi_gen_srt").format(n=len(video_paths))
            ).triggered.connect(
                lambda _=False, r=tuple(rows):
                    self._batch_generate_srt([self.model.item(x) for x in r
                                              if self.model.item(x) is not None]))
            menu.addAction(
                t("main_window.multi_thumbgrid").format(n=len(video_paths))
            ).triggered.connect(
                lambda _=False, ps=tuple(video_paths):
                    self._open_thumbgrid_dialog([Path(p) for p in ps]))
        menu.addSeparator()
        menu.addAction(
            t("main_window.multi_recycle").format(n=n)).triggered.connect(
            lambda _=False, ps=tuple(paths): self._recycle_media(list(ps)))
        return menu

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
            if item.is_video:
                menu.addAction(t("main_window.gen_srt")).triggered.connect(
                    lambda _=False, p=item.path: self._gen_srt_for(p)
                )
                menu.addAction(t("main_window.gen_srt_batch")).triggered.connect(
                    lambda _=False: self._batch_generate_srt()
                )
                menu.addAction(t("main_window.gen_srt_pick")).triggered.connect(
                    lambda _=False: self._pick_videos_for_srt()
                )
                menu.addAction(t("main_window.thumbgrid")).triggered.connect(
                    lambda _=False, p=item.path: self._open_thumbgrid_dialog([p])
                )
                menu.addSeparator()
            if not item.is_video and not item.is_archive:
                menu.addAction(t("menu.copy_image")).triggered.connect(
                    lambda _=False, p=item.path: fileops.copy_image_to_clipboard(p)
                )
            menu.addAction(t("menu.copy_file")).triggered.connect(
                lambda _=False, p=item.path: fileops.copy_files_to_clipboard([p])
            )
            if item.is_archive:
                menu.addAction(t("main_window.open_archive_entry")).triggered.connect(
                    lambda _=False, p=item.path: self._open_archive(p)
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
        new = fileops.rename(self, path)
        if new is None:
            return
        # The name is part of the cached directory record, so the listing has to be
        # rebuilt from disk rather than from what the cache still believes.
        dircache.cache.forget(path.parent)
        # 就地改名 + keep="anchor" 重排：原来 set_folder(force) 全量重进，
        # 模型先清空再铺回——视口回顶。retarget 顺带清掉旧排序键/缓存键
        # （缩略图键含路径，改名后本来就要重探）。
        for it in self.all_items:
            if it.path == path:
                it.retarget(new)
                break
        self._apply_view()

    def _recycle_media(self, paths: list[Path]) -> None:
        if not fileops.confirm_recycle(self, paths):
            return
        done, err = fileops.recycle(paths)
        if err and not done:
            self.status_count.setText(err)
            return
        for p in {p.parent for p in paths}:
            dircache.cache.forget(p)
        # 就地剔除 + keep="anchor" 重排（与五秒归位同一语义：锚定视口首
        # 项，原地更新，不回顶）。原来 set_folder(force) 全量重进：模型
        # 先清空、缓存相位走"全新内容"分支，观感是"删一个跳回顶部"。
        # dircache 已 forget，下次进目录/F5 的全量扫描照常校正。
        gone = {p for p in paths if not p.exists()}
        if gone:
            self.all_items = [it for it in self.all_items if it.path not in gone]
            self._apply_view()

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

    def set_folder(self, folder: Path | None, force: bool = False, quiet: bool = False) -> None:
        """Open `folder`. Returns immediately; everything slow happens on a pool thread.

        `quiet` skips switching the UI to the browser (used when a file was opened
        from the command line: the player window is already showing).

        Navigating anywhere leaves archive browsing automatically: the archive
        tree is swapped back for the real folder tree.

        Note what is deliberately *not* done here: no `is_dir()`, no cache walk, no
        listing built. A single `stat()` against a sleeping USB disk or a network share
        that has to be re-dialled costs seconds, and on the GUI thread those seconds are
        a frozen window. A folder that turns out not to be readable simply comes back
        empty, which the status line says.
        """
        if self._archive_mode:
            self._leave_archive_state()
        if folder is None:
            return
        folder = Path(folder)
        if folder == self.folder and not force:
            return
        from . import startup_log

        startup_log.stage("set-folder", f"打开文件夹 {folder}")
        # Record navigation history (skip if navigating from history or refreshing)
        if not force and not self._nav_from_history and self.folder is not None:
            self._save_scroll_pos()
            self._push_nav_history(self.folder)
        self.folder = folder
        self._quiet_scan = quiet
        self.thumbs.invalidate_queue()
        self.thumbs.trim_memory(600)
        # 换文件夹：预热/归位定时器停（旧文件夹的未完成项已被 invalidate
        # 清出队列；新文件夹扫描 done 后 _warmup_start 再启动）
        self._warmup_timer.stop()
        self._resort_interval_timer.stop()
        self._warmup_cursor = 0
        self._meta_dirty = False
        self.status_path.setText(str(folder))
        self.setWindowTitle(t("main_window.title_with_folder").format(folder=folder.name or str(folder)))
        if not quiet:
            self._show_browser()
        self._remember_recent(folder)
        self._sync_tree(folder)

        recursive = self.btn_recursive.isChecked()
        self._scan_token += 1
        token = self._scan_token
        # 种子每个文件夹只掷一次：cache/流式/done 各阶段共用，随机排序
        # 在扫描各阶段间保持稳定——中途重掷会让列表整体洗牌，既闪一下
        # 又留下按旧行号排队的过时缩略图请求
        self._random_seed = secrets.randbits(30)

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
            if not self._quiet_scan:
                self._apply_view(count_suffix=t("main_window.verifying_suffix"))
            return

        self._stream_items.extend(items)

        if phase != "done":
            if not self._streaming:
                return  # a cached list is already on screen; verify quietly
            if self._quiet_scan:
                return  # browser is not visible; keep the GUI thread free for playback
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
        suffix = ""
        if stats is not None and stats.dirs_reused:
            suffix = t("main_window.cache_hit_suffix").format(
                reused=stats.dirs_reused, total=stats.dirs_total
            )
        if stats is not None and stats.errors:
            # 权限/离线盘等导致的读取失败：明确浮出，避免看起来像"空文件夹"
            err_text = t("main_window.scan_errors").format(n=stats.errors)
            suffix = f"{suffix}{err_text}" if suffix else err_text
            print(f"[scan] {stats.errors} 个目录读取失败，最后: {stats.last_error}")
        if not self._quiet_scan:
            self._apply_view(count_suffix=suffix)
        else:
            # quiet 扫描（外部打开文件/恢复播放列表）：流式刷新仍跳过，但模型
            # 必须在 done 时回填——set_folder 进来时已把它清空，而 _flush_stream/
            # cache 阶段在 quiet 下全不执行，漏了这里用户回到浏览器就是空网格，
            # 要切到别的文件夹再切回来才恢复。代价与 startup handoff 的 sort 同
            # 量级，只在 done 一次性发生。
            self._apply_view()
            # stack 还停在欢迎页的话一并切到浏览器视图：播放器是独立窗口，
            # 这里切换不会打断它；否则用户关掉播放器看到的是欢迎页，观感
            # 仍是"没加载文件夹内容"。
            if self.stack.currentWidget() is self.welcome:
                self._show_browser()

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

        # 扫描 done：启动后台预热（低优先级消化全部未缓存项的缩略图/
        # 时长——大文件夹放着不管也会全量加载；视口请求优先插队）
        self._warmup_start()

        # A file opened from outside (command line on first launch, or forwarded
        # by a second instance to the running one): the player opened instantly
        # with a single-item list; now the folder scan is done, hand it the full
        # listing and keep the current position.
        startup = self._startup_file
        self._startup_file = None
        if (
            startup is not None
            and self.viewer is not None
            and self.viewer.isVisible()
        ):
            items = self.all_items if self._quiet_scan else self.model.items
            if self._quiet_scan:
                # 安静扫描（命令行/双击打开）时浏览器的 model 还没建，all_items
                # 是扫描顺序：按当前排序规则排一次，播放器与面板的顺序才与
                # 浏览器一致（面板不再自己重排，否则行号与 viewer.items 错位）
                items = media.sort_items(
                    items, self.sort_combo.currentData() or "name",
                    self.btn_desc.isChecked(), self._random_seed,
                )
            row = next(
                (i for i, it in enumerate(items) if it.path == startup),
                -1,
            )
            if row >= 0:
                # If the file that launched us is still what's playing, just swap in
                # the fuller listing without restarting playback.
                playing_now = (
                    0 <= self.viewer.index < len(self.viewer.items)
                    and self.viewer.items[self.viewer.index].path == startup
                )
                if playing_now:
                    self.viewer.extend_playlist(items, row)
                else:
                    self.viewer.open_playlist(items, row)

    STREAM_MIN_INTERVAL = 220
    STREAM_MAX_INTERVAL = 1500

    def _flush_stream(self) -> None:
        """Show what the breadth-first scan has found so far.

        Re-sorting and resetting the model costs roughly 200 ms once a list reaches a
        few thousand entries, so the repaint interval is tuned from how long the last
        repaint actually took. Otherwise a long scan would leave the UI thread with no
        headroom to stay responsive.
        """
        if not self._streaming or self._quiet_scan:
            return
        started = time.perf_counter()
        self.all_items = list(self._stream_items)
        # 流式增量的滚动保持：名称/随机等稳定排序下新项只追加，保持像素
        # 偏移即可；时长/mtime/大小等"元数据排序"下新项会插到中间（搬家），
        # 像素偏移=视口内容大换血——改用锚定项保持可见。
        meta_sort = (self.sort_combo.currentData() or "name") in ("duration", "mtime", "size")
        self._apply_view(count_suffix=t("main_window.scanning_suffix"),
                         keep="visible" if meta_sort else "offset")
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._stream_timer.setInterval(
            int(min(self.STREAM_MAX_INTERVAL, max(self.STREAM_MIN_INTERVAL, elapsed_ms * 3)))
        )

    def _apply_view(self, count_suffix: str = "", keep: str = "anchor") -> None:
        flags: set[str] = set()
        if self.chk_image.isChecked():
            flags.add("image")
        if self.chk_video.isChecked():
            flags.add("video")
        if self.chk_archive.isChecked():
            flags.add("archive")
        items = media.apply_filter(self.all_items, flags, self.search.text().strip())
        sort_key = self.sort_combo.currentData() or "name"
        items = media.sort_items(
            items,
            sort_key,
            self.btn_desc.isChecked(),
            self._random_seed,
            orders.get(self.folder) if (sort_key == "custom" and self.folder) else None,
        )
        # meta 回填引发的自动重排（keep=visible）加"结果未变"守卫：预热期
        # 间持续 meta_ready → 每 700ms 一次全量 set_items+relayout（48k 项
        # ~100ms/次）让界面持续卡顿（观感"未响应"）。排序键没变时（名称
        # 排序下 meta 回填不改变顺序）直接跳过——只有时长/大小等元数据
        # 排序才真正需要重排。
        if keep == "visible":
            new_fp = [it.cache_key for it in items]
            old_fp = [it.cache_key for it in self.model.items]
            if new_fp == old_fp:
                return
        keep_row = self.tiles.current_row() if self.tiles is not None else -1
        keep_path = None
        if 0 <= keep_row < len(self.model.items):
            keep_path = self.model.items[keep_row].path
        # keep="anchor"（默认，用户主动改排序/筛选/搜索）：同集合重排，锚定
        # 视口首项并滚到它的新位置——正在看的缩略图不会被"刷走"，也不再
        # 滚回顶部。keep="visible"（meta 回填/时长排序的自动重排）：锚定项
        # 保持可见即可（EnsureVisible 最小滚动）——对齐视口顶会每批跳一次，
        # 像素偏移会内容大换血。keep="offset"（流式增量）：内容增长，保持
        # 像素偏移。换文件夹时 model 已被 set_folder 清空 → 走 else
        # （回顶部+记忆位置）。
        if keep == "anchor" and self.tiles is not None \
                and self.model.items and items:
            self.tiles.set_items_keep_scroll(items)
        elif keep == "visible" and self.tiles is not None \
                and self.model.items and items:
            self.tiles.set_items_keep_anchor_visible(items)
        elif keep == "offset" and self.tiles is not None and items:
            self.tiles.set_items_keep_offset(items)
        else:
            self.model.set_items(items)
            self._restore_scroll_pos()
        n_img = sum(1 for i in items if not i.is_video)
        self.status_count.setText(
            t("main_window.item_count").format(
                count=len(items), images=n_img, videos=len(items) - n_img, suffix=count_suffix
            )
        )
        if keep_path is not None and self.tiles is not None:
            for i, it in enumerate(items):
                if it.path == keep_path:
                    # 只恢复选中态，不滚动：选中项常已滚出视口（选中后
                    # 继续下滑是常态），scroll_to 会把视口拽回它——定时
                    # 归位/防抖重排/改排序全都会"跳回选中的视频"
                    self.tiles.set_current_row(i, scroll=False)
                    break

    # ------------------------------------------------------------ 后台预热
    # 预热水位：每轮 tick 把待解码队列补到这个数为止（队列上限的一半，
    # 留一半给视口请求）。吞吐自动等于 worker 实际消化速度——磁盘命中/
    # 图片便宜就补得快，视频贵就自动放慢；固定"每轮 8 个"会让 worker
    # 吃完就空转大半秒，2 万项的文件夹要预热 40 多分钟。
    WARMUP_QUEUE_TARGET = MAX_QUEUED_JOBS // 2
    WARMUP_FAST_MS = 1000      # 首轮消化（游标未走完）；水位 + 1s 间隔的
                               # 吞吐上限 128 项/s，远超 worker 速度且不压
                               # GUI（meta 风暴由 _resort_timer 700ms 防抖兜住）
    WARMUP_POLL_MS = 15000     # 轮询模式（首轮走完，等失败重试/新增）
    WARMUP_DELAY_MS = 2000     # done 后延迟启动：避开 done 首绘 +
                               # 首批缩略图 ready 的重绘高峰（冷缓存实测
                               # 该窗口单次卡顿可达 1.3s）

    def _warmup_start(self) -> None:
        """扫描 done（或列表变化）后启动/重置后台预热。

        延迟 WARMUP_DELAY_MS：done 的首绘 + 首批缩略图 ready 是重绘
        高峰（冷缓存实测单次卡顿 1.3s），预热避开这 2 秒窗口再进场。
        归位定时器（元数据排序下的定时刷新）一并启动。
        """
        self._warmup_cursor = 0
        self._warmup_timer.setInterval(self.WARMUP_FAST_MS)
        self._warmup_timer.stop()
        if not self._resort_interval_timer.isActive():
            self._resort_interval_timer.start()
        QTimer.singleShot(self.WARMUP_DELAY_MS, self._warmup_timer.start)

    def _warmup_tick(self) -> None:
        """低优先级逐批请求未缓存项的缩略图/时长。

        priority 用 WARMUP_PRIO：视口请求（priority=行号，小值）在调度
        堆里恒排前面——用户滚动时可见项优先，预热只消化余量。request
        内部判重（内存/磁盘命中或已排队都跳过提交，磁盘命中还会顺带
        回填 meta），所以这里无需自己查缓存。

        节流按队列水位（WARMUP_QUEUE_TARGET）而非固定批量：本轮最多把
        队列补到水位，补满即止——吞吐自动跟上 worker 的消化速度，worker
        不会吃完工单后空转，GUI 压力也被队列上限封顶。
        """
        items = self.all_items
        if not items:
            self._warmup_timer.stop()
            return
        n = len(items)
        cursor = self._warmup_cursor
        # 轮询模式下从 0 重扫（失败重试/新增项）
        if cursor >= n:
            cursor = 0
        room = max(0, self.WARMUP_QUEUE_TARGET - self.thumbs.queued_count())
        submitted = 0
        while cursor < n and submitted < room:
            it = items[cursor]
            cursor += 1
            if it.is_archive:
                continue
            # request 返回 None=已提交解码/已排队；返回 QImage=内存命中
            # （无需处理）。磁盘缓存命中走 request 内部的 _load_from_disk
            # + _backfill_metadata 路径，同样提交。
            before = self.thumbs.peek(it)
            self.thumbs.request(it, priority=WARMUP_PRIO)
            if before is None:
                submitted += 1
        self._warmup_cursor = cursor
        if cursor >= n:
            # 本轮走完 → 切慢速轮询（15s 后从 0 重扫：失败项重试、
            # 扫描期间新发现的文件补探）
            if self._warmup_timer.interval() != self.WARMUP_POLL_MS:
                self._warmup_timer.setInterval(self.WARMUP_POLL_MS)
                self._warmup_timer.start()

    # -------------------------------------------------------------- viewer

    def _fs_model_for_panel(self):
        """Share the browser's directory model with the panel's browser tab."""
        self._materialize_tree()
        return self.fs_model

    def ensure_viewer(self) -> "Viewer":
        if self.viewer is None:
            from .viewer import Viewer  # imports mpv; deliberately deferred

            self.viewer = Viewer(self.thumbs, self._fs_model_for_panel)
            # Show/Hide 事件驱动缩略图视频解码余量：播放器不可见时放开
            # 第 2 路通用视频线程（见 ThumbnailCache.set_video_headroom）
            self.viewer.installEventFilter(self)
            self.viewer.index_changed.connect(self._on_viewer_index)
            self.viewer.folder_requested.connect(self._on_panel_folder_requested)
            self.viewer.playlist_changed.connect(self._on_playlist_changed)
            self.viewer.sort_requested.connect(self._on_panel_sort_requested)
        return self.viewer

    def eventFilter(self, obj, event) -> bool:
        if obj is self.viewer and event.type() in (QEvent.Show, QEvent.Hide):
            # 播放器不可见（关闭/隐藏）→ 放开第 2 路通用视频解码；
            # 可见（含最小化，Qt 语义里 minimized 仍算 visible）→ 收回
            # 单路，缩略图解码不与播放抢 CPU
            self.thumbs.set_video_headroom(obj.isVisible())
        return super().eventFilter(obj, event)

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
        if self.tiles is not None:
            self.tiles.relayout()

    def _open_viewer(self, row: int) -> None:
        if not self.model.items:
            return
        item = self.model.items[row] if 0 <= row < len(self.model.items) else None
        if item is not None and item.is_archive:
            self._open_archive(item.path)
            return
        if self.tiles is not None:
            self.tiles.set_current_row(row, scroll=False)
        self.ensure_viewer().open_playlist(self.model.items, row)

    def _on_viewer_index(self, row: int) -> None:
        if self.tiles is not None:
            self.tiles.set_current_row(row)
        if self.details is not None and self.stack.currentWidget() is self.details:
            self.details.selectRow(row)

    # ------------------------------------------------------- events / DnD

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.toLocalFile()]
        if not paths:
            return

        def _work() -> None:
            # 任何 stat 都在 worker 线程做：把死网络快捷方式拖进窗口时
            # UI 线程不该被一个 is_dir() 卡住数秒
            for p in paths:
                try:
                    if p.is_dir():
                        self._drop_open.emit(("folder", p))
                        return
                    if not p.is_file():
                        continue
                except OSError:
                    continue
                if media.classify(p) is not None:
                    self._drop_open.emit(("media", p))
                    return
                if media.is_archive_name(p.name):
                    self._drop_open.emit(("archive", p))
                    return
            self._drop_open.emit((None, None))

        import threading

        threading.Thread(target=_work, daemon=True).start()

    def _on_drop_resolved(self, decision) -> None:
        kind, p = decision
        if kind == "folder":
            self.set_folder(p)
        elif kind == "media":
            self.set_folder(p.parent)
            QTimer.singleShot(400, lambda target=p: self._open_path_when_listed(target))
        elif kind == "archive":
            self._open_archive(p)

    def _open_path(self, path: Path) -> None:
        for i, it in enumerate(self.model.items):
            if it.path == path:
                self._open_viewer(i)
                return

    def _open_path_when_listed(self, path: Path, attempts: int = 20) -> None:
        """等待文件夹扫描列出目标文件再播放（冷文件夹/网络盘首批要数秒，
        固定 400ms 的单次重试会静默丢弃）。"""
        for i, it in enumerate(self.model.items):
            if it.path == path:
                self._open_viewer(i)
                return
        if attempts > 0:
            QTimer.singleShot(
                400, lambda: self._open_path_when_listed(path, attempts - 1))

    def _open_recent_file(self, path) -> None:
        p = Path(path)
        if not p.is_file():
            return
        self.set_folder(p.parent)
        QTimer.singleShot(400, lambda target=p: self._open_path_when_listed(target))

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
        from PySide6.QtWidgets import QMessageBox

        from . import live_engine
        from .runtime import automation_mode

        # 自动化模式：不问"模型保留还是释放"，直接杀掉引擎干净退出
        # （不杀会留着占显存的子进程，脚本跑完机器上还挂着）
        if live_engine.alive() and automation_mode():
            live_engine.kill()
        elif live_engine.alive():
            box = QMessageBox(self)
            box.setWindowTitle(t("viewer.live_caption_quit_title"))
            box.setText(t("viewer.live_caption_quit_text").format(
                model=live_engine.model_label(),
                vram=f"{live_engine.vram_footprint_gb(include_translate=False):g}GB",
            ))
            box.setIcon(QMessageBox.Question)
            keep_btn = box.addButton(
                t("viewer.live_caption_quit_keep"), QMessageBox.AcceptRole
            )
            stop_btn = box.addButton(
                t("viewer.live_caption_quit_close"), QMessageBox.DestructiveRole
            )
            cancel_btn = box.addButton(
                t("viewer.live_caption_quit_cancel"), QMessageBox.RejectRole
            )
            box.setDefaultButton(keep_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is cancel_btn:
                e.ignore()
                return
            if clicked is stop_btn:
                live_engine.kill()

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
