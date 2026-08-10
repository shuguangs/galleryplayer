"""PotPlayer-style docked side panel: playlist, albums and a folder browser.

Rows are drawn by one delegate that supports two densities — a thumbnail row for
skimming photos, and a compact text row that fits a few hundred episodes on screen.
Reordering rides on QListWidget's InternalMove so drag-and-drop comes for free.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir, QEvent, QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFileSystemModel,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import fileops, icons, media, theme
from .albums import DEFAULT_ALBUM, albums
from .config import settings
from .i18n import t
from .media import MediaItem, format_duration
from .thumbs import ThumbnailCache

ITEM_ROLE = Qt.UserRole + 1

ROW_H_THUMB = 54
ROW_H_COMPACT = 26
THUMB_W = 72
PANEL_MIN_W = 210
PANEL_MAX_W = 640
GRIP_W = 5

LOOP_MODES = ["off", "list", "one", "shuffle"]
LOOP_LABELS = {
    "off": "panel.loop_off",
    "list": "panel.loop_list",
    "one": "panel.loop_one",
    "shuffle": "panel.loop_shuffle",
}
LOOP_ICONS = {
    "off": icons.REPEAT_ALL,
    "list": icons.REPEAT_ALL,
    "one": icons.REPEAT_ONE,
    "shuffle": icons.SHUFFLE,
}


def _tool(glyph: str, tip: str, width: int = 28, icon: bool = True) -> QToolButton:
    b = QToolButton()
    b.setObjectName("PanelIcon" if icon else "PanelBtn")
    b.setText(glyph)
    b.setToolTip(tip)
    b.setFixedHeight(24)
    if width:
        b.setFixedWidth(width)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    return b


# --------------------------------------------------------------------- delegate


class MediaRowDelegate(QStyledItemDelegate):
    """Paints one media row in either thumbnail or compact density."""

    def __init__(self, thumbs: ThumbnailCache, owner: "MediaListWidget") -> None:
        super().__init__(owner)
        self.thumbs = thumbs
        self.owner = owner

    def sizeHint(self, option, index) -> QSize:
        h = ROW_H_THUMB if self.owner.thumb_mode else ROW_H_COMPACT
        return QSize(60, h)

    def paint(self, p: QPainter, option, index: QModelIndex) -> None:
        item: MediaItem | None = index.data(ITEM_ROLE)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        playing = index.row() == self.owner.playing_row
        missing = item is not None and not item.exists

        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if playing:
            p.fillRect(rect, QColor(theme.BG_SELECT))
        elif selected:
            p.fillRect(rect, QColor(58, 64, 78))
        elif hovered:
            p.fillRect(rect, QColor(255, 255, 255, 16))

        if playing:
            p.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), QColor(theme.ACCENT))

        if item is None:
            p.restore()
            return

        text_color = QColor(theme.TEXT_FAINT) if missing else QColor(
            "#ffffff" if (playing or selected) else theme.TEXT
        )
        dim_color = QColor(theme.TEXT_FAINT if missing else theme.TEXT_DIM)

        if self.owner.thumb_mode:
            self._paint_thumb_row(p, rect, item, index, text_color, dim_color, missing)
        else:
            self._paint_compact_row(p, rect, item, index, text_color, dim_color)
        p.restore()

    # -- densities --------------------------------------------------------

    def _paint_thumb_row(self, p, rect, item, index, text_color, dim_color, missing):
        pad = 5
        thumb_h = rect.height() - pad * 2
        img_rect = QRect(rect.left() + pad + 3, rect.top() + pad, THUMB_W, thumb_h)

        clip = QPainterPath()
        clip.addRoundedRect(img_rect, 3, 3)
        p.save()
        p.setClipPath(clip)
        p.fillRect(img_rect, QColor("#101216"))
        thumb: QImage | None = self.thumbs.peek(item) if not missing else None
        if thumb is not None and not thumb.isNull():
            pix = QPixmap.fromImage(thumb).scaled(
                img_rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            p.drawPixmap(
                img_rect.left() + (img_rect.width() - pix.width()) // 2,
                img_rect.top() + (img_rect.height() - pix.height()) // 2,
                pix,
            )
        else:
            f = QFont(p.font()); f.setPointSize(7); p.setFont(f)
            p.setPen(QColor(theme.TEXT_FAINT))
            p.drawText(img_rect, Qt.AlignCenter, item.suffix.lstrip(".").upper())
            if not missing:
                self.thumbs.request(item)
        p.restore()

        left = img_rect.right() + 8
        avail = rect.right() - left - 8
        f = QFont(p.font()); f.setPointSize(8); p.setFont(f)
        fm = QFontMetrics(f)
        p.setPen(text_color)
        p.drawText(
            QRect(left, rect.top() + 7, avail, fm.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            fm.elidedText(f"{index.row() + 1}. {item.name}", Qt.ElideMiddle, avail),
        )
        bits = []
        if item.is_video and item.duration:
            bits.append(item.duration_text())
        if item.resolution_text():
            bits.append(item.resolution_text())
        bits.append(item.size_text())
        if missing:
            bits = [t("panel.file_missing")]
        f2 = QFont(p.font()); f2.setPointSize(7); p.setFont(f2)
        fm2 = QFontMetrics(f2)
        p.setPen(dim_color)
        p.drawText(
            QRect(left, rect.top() + 7 + fm.height() + 1, avail, fm2.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            fm2.elidedText("　·　".join(bits), Qt.ElideRight, avail),
        )

    def _paint_compact_row(self, p, rect, item, index, text_color, dim_color):
        f = QFont(p.font()); f.setPointSize(8); p.setFont(f)
        fm = QFontMetrics(f)
        dur = item.duration_text() if (item.is_video and item.duration) else ""
        if not item.exists:
            dur = t("panel.missing")
        dur_w = fm.horizontalAdvance(dur) + 10 if dur else 0
        left = rect.left() + 9
        name_w = rect.width() - 18 - dur_w
        p.setPen(text_color)
        p.drawText(
            QRect(left, rect.top(), name_w, rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            fm.elidedText(f"{index.row() + 1:02d}. {item.name}", Qt.ElideMiddle, name_w),
        )
        if dur:
            p.setPen(dim_color)
            p.drawText(
                QRect(rect.right() - dur_w - 6, rect.top(), dur_w, rect.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                dur,
            )


# ------------------------------------------------------------------ list widget


class MediaListWidget(QListWidget):
    reordered = Signal()
    activate_row = Signal(int)

    def __init__(self, thumbs: ThumbnailCache, parent=None) -> None:
        super().__init__(parent)
        self.thumb_mode = True
        self.playing_row = -1
        self.setObjectName("PanelList")
        self.setUniformItemSizes(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setDelegateInstance(thumbs)
        self.doubleClicked.connect(lambda idx: self.activate_row.emit(idx.row()))
        thumbs.ready.connect(self._on_thumb_ready)
        thumbs.meta_ready.connect(self._on_thumb_ready)

    def setDelegateInstance(self, thumbs: ThumbnailCache) -> None:
        self._delegate = MediaRowDelegate(thumbs, self)
        self.setItemDelegate(self._delegate)

    def set_thumb_mode(self, on: bool) -> None:
        if on == self.thumb_mode:
            return
        self.thumb_mode = on
        # re-emitting the size hints is what makes the rows change height
        for i in range(self.count()):
            self.item(i).setSizeHint(QSize(60, ROW_H_THUMB if on else ROW_H_COMPACT))
        self.doItemsLayout()
        self.viewport().update()

    def _on_thumb_ready(self, *_args) -> None:
        self.viewport().update()

    def set_items(self, items: list[MediaItem], playing: int = -1) -> None:
        self.blockSignals(True)
        self.clear()
        h = ROW_H_THUMB if self.thumb_mode else ROW_H_COMPACT
        for it in items:
            entry = QListWidgetItem()
            entry.setData(ITEM_ROLE, it)
            entry.setSizeHint(QSize(60, h))
            entry.setToolTip(str(it.path))
            self.addItem(entry)
        self.blockSignals(False)
        self.playing_row = playing
        self.viewport().update()

    def items(self) -> list[MediaItem]:
        return [self.item(i).data(ITEM_ROLE) for i in range(self.count())]

    def set_playing(self, row: int, scroll: bool = True) -> None:
        self.playing_row = row
        if scroll and 0 <= row < self.count():
            self.scrollToItem(self.item(row), QAbstractItemView.EnsureVisible)
        self.viewport().update()

    def dropEvent(self, e):
        super().dropEvent(e)
        self.reordered.emit()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            idx = self.currentRow()
            if idx >= 0:
                self.activate_row.emit(idx)
                return
        super().keyPressEvent(e)


# ------------------------------------------------------------------- the panel


class PlaylistPanel(QWidget):
    play_index = Signal(int)                  # jump within the current playlist
    playlist_reordered = Signal(list)         # new MediaItem order
    playlist_removed = Signal(list)           # rows removed
    folder_requested = Signal(object)         # Path, from the browser tab
    album_play_requested = Signal(list, int)  # [MediaItem], start index
    loop_mode_changed = Signal(str)
    autoplay_changed = Signal(bool)
    sort_requested = Signal(str, bool)  # (sort_key, desc) — shared with main window
    playlist_imported = Signal(list)          # [MediaItem] loaded from a .m3u/.m3u8
    closed = Signal()
    width_changed = Signal(int)

    def __init__(self, thumbs: ThumbnailCache, fs_model_provider=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self.thumbs = thumbs
        self._fs_model_provider = fs_model_provider
        self._all_items: list[MediaItem] = []
        self._current_album = DEFAULT_ALBUM
        self._resizing = False
        self._resize_origin = 0
        self._resize_start_w = 0
        self.setMouseTracking(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(GRIP_W, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_tabs())
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_playlist_tab())
        self.stack.addWidget(self._build_album_tab())
        self.stack.addWidget(self._build_browser_tab())
        root.addWidget(self.stack, 1)
        root.addWidget(self._build_footer())

        self._restore()
        self._select_tab(0)

    # ------------------------------------------------------------ tab bar

    def _build_tabs(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("PanelHeader")
        bar.setFixedHeight(34)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(4, 4, 4, 3)
        lay.setSpacing(2)

        self.tab_buttons: list[QToolButton] = []
        for i, (label, tip) in enumerate(
            ((t("panel.playlist_tab"), t("panel.playlist_tab_tip")),
             (t("panel.album_tab"), t("panel.album_tab_tip")),
             (t("panel.browser_tab"), t("panel.browser_tab_tip")))
        ):
            b = QToolButton()
            b.setObjectName("PanelTab")
            b.setText(label)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, n=i: self._select_tab(n))
            lay.addWidget(b)
            self.tab_buttons.append(b)

        lay.addStretch(1)
        self.btn_density = _tool(icons.VIEW_TILES, t("panel.density_tip"))
        self.btn_density.clicked.connect(self._toggle_density)
        lay.addWidget(self.btn_density)
        btn_close = _tool(icons.CHEVRON_RIGHT, t("panel.collapse_tip"))
        btn_close.clicked.connect(self.closed)
        lay.addWidget(btn_close)
        return bar

    def _select_tab(self, n: int) -> None:
        for i, b in enumerate(self.tab_buttons):
            b.setChecked(i == n)
        self.stack.setCurrentIndex(n)
        self.footer.setVisible(n in (0, 1))
        settings["panel_tab"] = n
        if n == 1:
            self._reload_album_tabs()
        elif n == 2:
            self._ensure_tree()

    # -------------------------------------------------------- playlist tab

    def _build_playlist_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.list = MediaListWidget(self.thumbs)
        self.list.activate_row.connect(self._on_activate_playlist)
        self.list.reordered.connect(self._on_reordered)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._playlist_menu)
        lay.addWidget(self.list)
        return page

    def _on_activate_playlist(self, row: int) -> None:
        item = self.list.item(row)
        if item is None:
            return
        target = item.data(ITEM_ROLE)
        for i, it in enumerate(self._all_items):
            if it.path == target.path:
                self.play_index.emit(i)
                return

    def _on_reordered(self) -> None:
        order = self.list.items()
        self._all_items = order
        self.playlist_reordered.emit(order)

    def _playlist_menu(self, pos: QPoint) -> None:
        rows = sorted({i.row() for i in self.list.selectedIndexes()})
        if not rows:
            return
        menu = QMenu(self)
        act_play = QAction(t("panel.play"), menu)
        act_play.triggered.connect(lambda: self._on_activate_playlist(rows[0]))
        menu.addAction(act_play)
        menu.addSeparator()

        sub = menu.addMenu(t("panel.add_to_album"))
        for name in albums.names():
            a = QAction(name, sub)
            a.triggered.connect(lambda _=False, n=name, r=tuple(rows): self._add_to_album(n, r))
            sub.addAction(a)
        sub.addSeparator()
        a_new = QAction(t("panel.new_album_ellipsis"), sub)
        a_new.triggered.connect(lambda _=False, r=tuple(rows): self._add_to_new_album(r))
        sub.addAction(a_new)

        menu.addSeparator()
        paths = self._paths_for(rows)
        if len(paths) == 1:
            menu.addAction(t("panel.open_default")).triggered.connect(
                lambda _=False, p=paths[0]: fileops.open_default(p)
            )
        menu.addAction(t("panel.reveal_in_explorer")).triggered.connect(
            lambda _=False, r=rows[0]: self._reveal(r)
        )
        if paths:
            menu.addAction(t("panel.copy_full_path")).triggered.connect(
                lambda _=False, ps=tuple(paths): fileops.copy_to_clipboard(
                    "\n".join(str(p) for p in ps)
                )
            )
        if len(paths) == 1:
            menu.addAction(t("panel.copy_filename")).triggered.connect(
                lambda _=False, p=paths[0]: fileops.copy_to_clipboard(p.name)
            )
            menu.addAction(t("panel.rename_ellipsis")).triggered.connect(
                lambda _=False, p=paths[0]: self._rename(p)
            )

        menu.addSeparator()
        act_rm = QAction(t("panel.remove_from_list"), menu)
        act_rm.triggered.connect(self._remove_selected)
        menu.addAction(act_rm)
        if paths:
            menu.addAction(t("panel.recycle_ellipsis")).triggered.connect(
                lambda _=False, ps=tuple(paths): self._recycle(list(ps))
            )
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _paths_for(self, rows) -> list[Path]:
        out = []
        for r in rows:
            it = self.list.item(r)
            if it is not None:
                out.append(it.data(ITEM_ROLE).path)
        return out

    def _reveal(self, row: int) -> None:
        item = self.list.item(row)
        if item is not None:
            fileops.reveal(item.data(ITEM_ROLE).path)

    def _current_index(self) -> int:
        row = self.list.playing_row
        entry = self.list.item(row) if 0 <= row < self.list.count() else None
        if entry is None:
            return -1
        target = entry.data(ITEM_ROLE).path
        return next((i for i, x in enumerate(self._all_items) if x.path == target), -1)

    def _rename(self, path: Path) -> None:
        target = fileops.rename(self, path)
        if target is None:
            return
        # Rebuild the rows in place; a full rescan belongs to the browser window, and
        # doing one here would interrupt whatever is playing.
        for it in self._all_items:
            if it.path == path:
                it.retarget(target)
                break
        self.set_playlist(self._all_items, self._current_index())
        self.playlist_reordered.emit(self._all_items)

    def _recycle(self, paths: list[Path]) -> None:
        if not fileops.confirm_recycle(self, paths):
            return
        done, err = fileops.recycle(paths)
        if not done:
            self._toast(err or t("panel.no_files_deleted"))
            return
        gone = {p for p in paths if not p.exists()}
        remaining = [i for i in self._all_items if i.path not in gone]
        self._toast(t("panel.recycled_count").format(done=done))
        self.playlist_removed.emit(remaining)

    def _add_to_album(self, name: str, rows: tuple[int, ...]) -> None:
        paths = [self.list.item(r).data(ITEM_ROLE).path for r in rows if self.list.item(r)]
        added = albums.add(name, paths)
        albums.save()
        self._toast(t("panel.added_to_album").format(added=added, name=name) if added else t("panel.already_in_album"))

    def _add_to_new_album(self, rows: tuple[int, ...]) -> None:
        name, ok = QInputDialog.getText(self, t("panel.new_album_dialog"), t("panel.album_name_label"), text=t("panel.new_album_default"))
        if not ok:
            return
        created = albums.create(name.strip() or t("panel.new_album_default"))
        self._add_to_album(created, rows)
        self._reload_album_tabs()

    # ----------------------------------------------------------- album tab

    def _build_album_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        strip = QWidget()
        strip.setObjectName("AlbumStrip")
        strip.setFixedHeight(30)
        self.album_strip = QHBoxLayout(strip)
        self.album_strip.setContentsMargins(4, 3, 4, 3)
        self.album_strip.setSpacing(2)
        lay.addWidget(strip)

        self.album_list = MediaListWidget(self.thumbs)
        self.album_list.activate_row.connect(self._on_activate_album)
        self.album_list.reordered.connect(self._on_album_reordered)
        self.album_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.album_list.customContextMenuRequested.connect(self._album_menu)
        lay.addWidget(self.album_list, 1)
        return page

    def _reload_album_tabs(self) -> None:
        while self.album_strip.count():
            it = self.album_strip.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        names = albums.names()
        if self._current_album not in names:
            self._current_album = names[0] if names else DEFAULT_ALBUM
        for name in names:
            b = QToolButton()
            b.setObjectName("AlbumTab")
            b.setText(name)
            b.setCheckable(True)
            b.setChecked(name == self._current_album)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, n=name: self._switch_album(n))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(
                lambda _p, n=name: self._album_tab_menu(n)
            )
            self.album_strip.addWidget(b)
        plus = _tool(icons.NEW_ALBUM, t("panel.new_album_tip"), 26)
        plus.clicked.connect(self._new_album)
        self.album_strip.addWidget(plus)
        self.album_strip.addStretch(1)
        self._reload_album_items()

    def _album_tab_menu(self, name: str) -> None:
        menu = QMenu(self)
        a1 = QAction(t("panel.rename_ellipsis"), menu)
        a1.triggered.connect(lambda: self._rename_album(name))
        menu.addAction(a1)
        a2 = QAction(t("panel.prune_album"), menu)
        a2.triggered.connect(lambda: self._prune_album(name))
        menu.addAction(a2)
        menu.addSeparator()
        a3 = QAction(t("panel.delete_album"), menu)
        a3.triggered.connect(lambda: self._delete_album(name))
        menu.addAction(a3)
        menu.exec(QCursor.pos())

    def _switch_album(self, name: str) -> None:
        self._current_album = name
        self._reload_album_tabs()

    def _new_album(self) -> None:
        name, ok = QInputDialog.getText(self, t("panel.new_album_dialog"), t("panel.album_name_label"), text=t("panel.new_album_default"))
        if not ok:
            return
        self._current_album = albums.create(name.strip() or t("panel.new_album_default"))
        albums.save()
        self._reload_album_tabs()

    def _rename_album(self, name: str) -> None:
        new, ok = QInputDialog.getText(self, t("panel.rename_album_dialog"), t("panel.new_name_label"), text=name)
        if not ok:
            return
        if albums.rename(name, new.strip()):
            if self._current_album == name:
                self._current_album = new.strip()
            albums.save()
            self._reload_album_tabs()
        else:
            self._toast(t("panel.rename_failed"))

    def _delete_album(self, name: str) -> None:
        if QMessageBox.question(self, t("panel.delete_album_title"), t("panel.delete_album_confirm").format(name=name)) != QMessageBox.Yes:
            return
        albums.delete(name)
        albums.save()
        self._reload_album_tabs()

    def _prune_album(self, name: str) -> None:
        removed = albums.prune_missing(name)
        albums.save()
        self._reload_album_items()
        self._toast(t("panel.pruned_count").format(removed=removed) if removed else t("panel.no_stale_items"))

    def _reload_album_items(self) -> None:
        paths = albums.paths(self._current_album)
        items = [media.item_for_path(Path(p)) for p in paths]
        items = [i for i in items if i is not None]
        self.album_list.set_items(items)

    def _on_activate_album(self, row: int) -> None:
        items = [i for i in self.album_list.items() if i.exists]
        if not items:
            self._toast(t("panel.album_files_gone"))
            return
        target = self.album_list.item(row).data(ITEM_ROLE)
        start = next((i for i, it in enumerate(items) if it.path == target.path), 0)
        self.album_play_requested.emit(items, start)

    def _on_album_reordered(self) -> None:
        albums.set_order(self._current_album, [str(i.path) for i in self.album_list.items()])
        albums.save()

    def _album_menu(self, pos: QPoint) -> None:
        rows = sorted({i.row() for i in self.album_list.selectedIndexes()})
        if not rows:
            return
        menu = QMenu(self)
        a0 = QAction(t("panel.play"), menu)
        a0.triggered.connect(lambda: self._on_activate_album(rows[0]))
        menu.addAction(a0)
        menu.addSeparator()
        a1 = QAction(t("panel.remove_from_album"), menu)
        a1.triggered.connect(lambda: self._album_remove(tuple(rows)))
        menu.addAction(a1)
        menu.exec(self.album_list.viewport().mapToGlobal(pos))

    def _album_remove(self, rows: tuple[int, ...]) -> None:
        paths = [self.album_list.item(r).data(ITEM_ROLE).path for r in rows if self.album_list.item(r)]
        albums.remove(self._current_album, paths)
        albums.save()
        self._reload_album_items()

    def _album_add_files(self) -> None:
        paths, _sel = QFileDialog.getOpenFileNames(
            self, t("panel.add_to_album_files"), str(settings["last_folder"] or ""),
            t("panel.filter_media") + ";;" + t("panel.filter_all"),
        )
        if paths:
            albums.add(self._current_album, paths)
            albums.save()
            self._reload_album_items()

    # ---------------------------------------------------------- browser tab

    def _build_browser_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.tree: QTreeView | None = None
        self._tree_layout = lay
        return page

    def _ensure_tree(self) -> None:
        if self.tree is not None:
            return
        model = None
        if self._fs_model_provider is not None:
            try:
                model = self._fs_model_provider()
            except Exception:
                model = None
        if model is None:
            model = QFileSystemModel(self)
            model.setFilter(QDir.Dirs | QDir.Drives | QDir.NoDotAndDotDot)
            model.setOption(QFileSystemModel.DontWatchForChanges, True)
            model.setRootPath("")
        self.tree = QTreeView()
        self.tree.setObjectName("PanelTree")
        self.tree.setModel(model)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(13)
        self.tree.setAnimated(False)
        self.tree.setFocusPolicy(Qt.ClickFocus)
        for c in range(1, 4):
            self.tree.hideColumn(c)
        self.tree.clicked.connect(self._on_tree_clicked)
        self._tree_layout.addWidget(self.tree)

    def _on_tree_clicked(self, index: QModelIndex) -> None:
        model = self.tree.model()
        try:
            path = Path(model.filePath(index))
        except Exception:
            return
        if path.is_dir():
            self.folder_requested.emit(path)

    def reveal_folder(self, folder: Path) -> None:
        if self.tree is None:
            return
        model = self.tree.model()
        try:
            idx = model.index(str(folder))
        except Exception:
            return
        if idx.isValid():
            self.tree.setCurrentIndex(idx)
            self.tree.expand(idx)
            self.tree.scrollTo(idx, QAbstractItemView.PositionAtCenter)

    # -------------------------------------------------------------- footer

    def _build_footer(self) -> QWidget:
        self.footer = QWidget()
        self.footer.setObjectName("PanelFooter")
        outer = QVBoxLayout(self.footer)
        outer.setContentsMargins(5, 4, 5, 8)
        outer.setSpacing(4)

        row1 = QHBoxLayout()
        row1.setSpacing(3)
        self.search = QLineEdit()
        self.search.setObjectName("PanelSearch")
        self.search.setPlaceholderText(t("panel.search_placeholder"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        row1.addWidget(self.search, 1)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(3)
        b_up = _tool(icons.MOVE_UP, t("panel.move_up_tip"))
        b_up.clicked.connect(lambda: self._move_selected(-1))
        row2.addWidget(b_up)
        b_down = _tool(icons.MOVE_DOWN, t("panel.move_down_tip"))
        b_down.clicked.connect(lambda: self._move_selected(1))
        row2.addWidget(b_down)
        b_add = _tool(icons.PLUS, t("panel.add_files_tip"))
        b_add.clicked.connect(self._footer_add)
        row2.addWidget(b_add)
        b_rm = _tool(icons.MINUS, t("panel.remove_selected_tip"))
        b_rm.clicked.connect(self._remove_selected)
        row2.addWidget(b_rm)

        self.btn_io = _tool(icons.PLAYLIST, t("panel.io_tip"))
        self.btn_io.setPopupMode(QToolButton.InstantPopup)
        io_menu = QMenu(self)
        act_imp = QAction(t("panel.import_playlist_ellipsis"), io_menu)
        act_imp.triggered.connect(self._import_playlist)
        io_menu.addAction(act_imp)
        act_exp = QAction(t("panel.export_playlist_ellipsis"), io_menu)
        act_exp.triggered.connect(self._export_playlist)
        io_menu.addAction(act_exp)
        self.btn_io.setMenu(io_menu)
        row2.addWidget(self.btn_io)

        # ---- sort the panel's list; shares settings["sort_key"] with the main
        #      window so the playlist always inherits the browser's ordering ----
        self.sort_combo = icons.ArrowComboBox()
        for key, label in media.SORT_LABELS.items():
            self.sort_combo.addItem(t(label), key)
        idx = self.sort_combo.findData(str(settings["sort_key"]))
        self.sort_combo.setCurrentIndex(max(0, idx))
        self.sort_combo.currentIndexChanged.connect(self._apply_panel_sort)
        row2.addWidget(self.sort_combo)
        self.btn_sort_desc = _tool(icons.SORT_ASC, t("main_window.sort_toggle_tip"), 22)
        self.btn_sort_desc.setCheckable(True)
        self.btn_sort_desc.setChecked(bool(settings["sort_desc"]))
        self.btn_sort_desc.clicked.connect(self._apply_panel_sort)
        row2.addWidget(self.btn_sort_desc)

        row2.addStretch(1)
        self.btn_loop = _tool(icons.REPEAT_ALL, t("panel.loop_tip"), 28)
        self.btn_loop.clicked.connect(self._cycle_loop)
        row2.addWidget(self.btn_loop)
        self.btn_autoplay = QToolButton()
        self.btn_autoplay.setObjectName("PanelBtn")
        self.btn_autoplay.setText(t("panel.autoplay_text"))
        self.btn_autoplay.setToolTip(t("panel.autoplay_tip"))
        self.btn_autoplay.setCheckable(True)
        self.btn_autoplay.setFixedHeight(24)
        self.btn_autoplay.setCursor(Qt.PointingHandCursor)
        self.btn_autoplay.setFocusPolicy(Qt.NoFocus)
        self.btn_autoplay.toggled.connect(self._on_autoplay)
        row2.addWidget(self.btn_autoplay)
        outer.addLayout(row2)
        return self.footer

    def _apply_panel_sort(self) -> None:
        key = self.sort_combo.currentData() or "name"
        desc = self.btn_sort_desc.isChecked()
        items = media.sort_items(self._all_items, key, desc)
        current = getattr(self.list, "playing_row", -1)
        self._all_items = list(items)
        self.list.set_items(self._all_items, -1)
        self._apply_filter(self.search.text())
        self.list.set_playing(current)
        # shared with the main window: persist + tell it to re-sort the browser
        settings["sort_key"] = key
        settings["sort_desc"] = desc
        self.sort_requested.emit(key, desc)

    def _footer_add(self) -> None:
        if self.stack.currentIndex() == 1:
            self._album_add_files()
        else:
            self._select_tab(1)
            self._toast(t("panel.switch_to_album_hint"))

    def _active_list(self) -> MediaListWidget:
        return self.album_list if self.stack.currentIndex() == 1 else self.list

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        lst = self._active_list()
        for i in range(lst.count()):
            entry = lst.item(i)
            item = entry.data(ITEM_ROLE)
            hide = bool(needle) and needle not in item.name.lower()
            entry.setHidden(hide)

    def _move_selected(self, delta: int) -> None:
        lst = self._active_list()
        rows = sorted({i.row() for i in lst.selectedIndexes()}, reverse=delta > 0)
        if not rows:
            return
        for row in rows:
            target = row + delta
            if not (0 <= target < lst.count()):
                return
        moved = []
        for row in rows:
            entry = lst.takeItem(row)
            lst.insertItem(row + delta, entry)
            moved.append(row + delta)
        lst.clearSelection()
        for r in moved:
            lst.item(r).setSelected(True)
        if lst is self.album_list:
            self._on_album_reordered()
        else:
            self._on_reordered()

    def _remove_selected(self) -> None:
        lst = self._active_list()
        rows = sorted({i.row() for i in lst.selectedIndexes()}, reverse=True)
        if not rows:
            return
        if lst is self.album_list:
            self._album_remove(tuple(sorted(rows)))
            return
        for row in rows:
            lst.takeItem(row)
        remaining = lst.items()
        self._all_items = remaining
        self.playlist_removed.emit(remaining)

    # ------------------------------------------------------- import / export

    def _export_playlist(self) -> None:
        items = self.list.items()
        if not items:
            self._toast(t("panel.playlist_empty"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, t("panel.export_dialog_title"), "playlist.m3u8", t("panel.filter_playlist")
        )
        if not path:
            return
        try:
            lines = ["#EXTM3U"]
            for it in items:
                lines.append(f"#EXTINF:-1,{it.name}")
                lines.append(str(it.path))
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._toast(t("panel.exported_count").format(count=len(items)))
        except OSError as exc:
            self._toast(t("panel.export_failed").format(err=exc))

    def _import_playlist(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, t("panel.import_dialog_title"), "", t("panel.filter_playlist_all")
        )
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError as exc:
            self._toast(t("panel.read_failed").format(err=exc))
            return
        base = Path(path).parent
        items = []
        seen = set()
        for line in raw:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            candidate = Path(line)
            if not candidate.is_absolute():
                candidate = base / candidate
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            it = media.item_for_path(candidate)
            if it is not None:
                items.append(it)
        if not items:
            self._toast(t("panel.no_playable_items"))
            return
        self._toast(t("panel.imported_count").format(count=len(items)))
        self.playlist_imported.emit(items)

    # ---------------------------------------------------------- loop modes

    def _cycle_loop(self) -> None:
        mode = str(settings["loop_mode"])
        nxt = LOOP_MODES[(LOOP_MODES.index(mode) + 1) % len(LOOP_MODES)] if mode in LOOP_MODES else "list"
        self.set_loop_mode(nxt)
        self.loop_mode_changed.emit(nxt)

    def set_loop_mode(self, mode: str) -> None:
        if mode not in LOOP_MODES:
            mode = "off"
        settings["loop_mode"] = mode
        self.btn_loop.setText(LOOP_ICONS[mode])
        self.btn_loop.setToolTip(t("panel.loop_tooltip").format(mode=t(LOOP_LABELS[mode])))
        self.btn_loop.setProperty("active", mode != "off")
        self.btn_loop.style().unpolish(self.btn_loop)
        self.btn_loop.style().polish(self.btn_loop)

    def _on_autoplay(self, on: bool) -> None:
        settings["autoplay_next"] = on
        self.autoplay_changed.emit(on)

    def set_autoplay(self, on: bool) -> None:
        self.btn_autoplay.blockSignals(True)
        self.btn_autoplay.setChecked(on)
        self.btn_autoplay.blockSignals(False)

    # ------------------------------------------------------------- density

    def _toggle_density(self) -> None:
        self.set_thumb_mode(not self.list.thumb_mode)

    def set_thumb_mode(self, on: bool) -> None:
        self.list.set_thumb_mode(on)
        self.album_list.set_thumb_mode(on)
        self.btn_density.setText(icons.VIEW_TILES if on else icons.VIEW_LIST)
        self.btn_density.setToolTip(t("panel.density_thumb_tip") if on else t("panel.density_compact_tip"))
        settings["panel_thumb_mode"] = on

    # -------------------------------------------------------------- public

    def set_playlist(self, items: list[MediaItem], current: int) -> None:
        # Inherit the browser's current ordering: the playlist opens sorted the
        # same way the main window's file browser is sorted.
        target = items[current].path if 0 <= current < len(items) else None
        items = media.sort_items(
            list(items), str(settings["sort_key"]), bool(settings["sort_desc"])
        )
        self._all_items = list(items)
        cur = -1
        if target is not None:
            for i, it in enumerate(items):
                if it.path == target:
                    cur = i
                    break
        self.list.set_items(self._all_items, cur)
        self._apply_filter(self.search.text())
        self.list.set_playing(cur)

    def set_current(self, index: int) -> None:
        target = self._all_items[index].path if 0 <= index < len(self._all_items) else None
        row = -1
        for i in range(self.list.count()):
            if self.list.item(i).data(ITEM_ROLE).path == target:
                row = i
                break
        self.list.set_playing(row)

    def _restore(self) -> None:
        self.set_thumb_mode(bool(settings["panel_thumb_mode"]))
        self.set_loop_mode(str(settings["loop_mode"]))
        self.set_autoplay(bool(settings["autoplay_next"]))

    def _toast(self, text: str) -> None:
        parent = self.parentWidget()
        fn = getattr(parent, "_show_toast", None)
        if callable(fn):
            fn(text)

    # ------------------------------------------------- left-edge resizing

    def mouseMoveEvent(self, e):
        if self._resizing:
            delta = self._resize_origin - e.globalPosition().toPoint().x()
            new_w = max(PANEL_MIN_W, min(PANEL_MAX_W, self._resize_start_w + delta))
            self.width_changed.emit(new_w)
            return
        self.setCursor(
            Qt.SizeHorCursor if e.position().x() <= GRIP_W else Qt.ArrowCursor
        )
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().x() <= GRIP_W:
            self._resizing = True
            self._resize_origin = e.globalPosition().toPoint().x()
            self._resize_start_w = self.width()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if self._resizing:
            self._resizing = False
            settings["panel_width"] = self.width()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def leaveEvent(self, e):
        self.unsetCursor()
        super().leaveEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BG_PANEL))
        p.fillRect(QRect(0, 0, 1, self.height()), QColor(theme.BORDER))
        grip = QRect(1, self.height() // 2 - 14, GRIP_W - 1, 28)
        p.fillRect(grip, QColor(255, 255, 255, 18))
