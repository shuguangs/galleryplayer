"""PotPlayer-style docked side panel: playlist, albums and a folder browser.

Rows are drawn by one delegate that supports two densities — a thumbnail row for
skimming photos, and a compact text row that fits a few hundred episodes on screen.
Reordering rides on QListWidget's InternalMove so drag-and-drop comes for free.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QDir,
    QEvent,
    QModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
    QTimer,
)
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
        self.thumbs_paused = False

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
            if not missing and not self.owner.thumbs_paused:
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

    # ---- 大列表分块填充（十万级条目点开视频曾把 GUI 线程堵 2-4 秒：白屏。
    # ≤SYNC_MAX 的小列表行为与旧版完全一致；更大的先同步填播放行附近的
    # 立即窗口，其余由 QTimer 后台块填充——视频先播，列表渐满。
    # 行号==播放列表序号的不变式由"顺序追加"天然保持。
    SYNC_MAX = 3000            # 同步填充上限（小列表零行为差异）
    IMMEDIATE_MIN = 1000       # 立即窗口下限（列表顶端上下文）
    IMMEDIATE_PLAY_AHEAD = 500  # 播放行之后再多填一些（向下翻看）
    IMMEDIATE_MAX = 4000       # 立即窗口上限（约 80ms）
    CHUNK_ROWS = 1000          # 后台块起始行数（超时会自适应减半）
    CHUNK_MIN_ROWS = 250       # 自适应下限
    CHUNK_SLOW_ROWS = 250      # 播放中每块上限（GUI 线程要先喂 mpv 画面）
    CHUNK_SLOW_MS = 150        # 单块超过这个时长 → 下块行数减半
    CHUNK_INTERVAL_MS = 40
    CHUNK_INTERVAL_SLOW_MS = 250   # 播放中的块间隔

    @property
    def _chunk_rows(self) -> int:
        v = max(self.CHUNK_MIN_ROWS, self._chunk_rows_v)
        return min(v, self.CHUNK_SLOW_ROWS) if self._slow_pacing else v

    @_chunk_rows.setter
    def _chunk_rows(self, v: int) -> None:
        self._chunk_rows_v = max(self.CHUNK_MIN_ROWS, v)

    def _chunk_interval(self) -> int:
        return (self.CHUNK_INTERVAL_SLOW_MS if self._slow_pacing
                else self.CHUNK_INTERVAL_MS)

    def set_slow_pacing(self, slow: bool) -> None:
        """播放中放慢分块填充：每块更少行、块间隔更长。

        mpv 画面在 GUI 线程渲染（QOpenGLWidget.paintGL），填充块与出帧
        争的是同一个线程——十万级列表全速填充实测让播放持续掉帧。放慢
        后填充仍在推进，只是把 GUI 时间优先让给画面。
        """
        if slow == self._slow_pacing:
            return
        self._slow_pacing = slow
        if self._fill_timer.isActive():
            self._fill_timer.start(self._chunk_interval())

    def set_thumbs_paused(self, paused: bool) -> None:
        """Low-priority mode: stop requesting new thumbnails (playback first)."""
        if self.thumbs_paused == paused:
            return
        self.thumbs_paused = paused
        if not paused:
            self.viewport().update()  # repaint visible rows -> resume requests

    def __init__(self, thumbs: ThumbnailCache, parent=None) -> None:
        super().__init__(parent)
        self.thumb_mode = True
        self.playing_row = -1
        self.thumbs_paused = False  # low-priority mode: skip thumbnail requests
        self.setObjectName("PanelList")
        # 行高全局统一（缩略图模式 54 / 紧凑 26，切换时整表一起改，见
        # set_thumb_mode 与 MediaRowDelegate.sizeHint）——所以可以开
        # uniformItemSizes。关着的时候 QListView 每插一批行都要重算全表
        # 行位置：30 万行分块填充实测 109.3s GUI 时间，开了是 4.5s（24 倍）。
        # 单块 addItem 本身只 7-13ms，代价全在插完之后的布局，自适应减半
        # 量不到——这就是播放时每隔 1 秒一条 gui-stall 的来源。
        self.setUniformItemSizes(True)
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
        # 后台填充状态
        self._fill_source: list[MediaItem] = []
        self._fill_pos = 0
        self._filter_needle = ""
        self._had_filter = False
        self._scroll_pending_row = -1
        self._chunk_rows_v = self.CHUNK_ROWS
        self._slow_pacing = False   # 播放中放慢分块填充（见 set_slow_pacing）
        self._fill_timer = QTimer(self)
        self._fill_timer.setSingleShot(True)
        self._fill_timer.setInterval(self.CHUNK_INTERVAL_MS)
        self._fill_timer.timeout.connect(self._fill_chunk)
        # 分块替换（顺序变化的大列表：先分块移除旧行再接常规填充，
        # 避免同步 clear() 三十万行 20s 冻结）
        self._replace_phase = False
        self._replace_source: list[MediaItem] = []
        self._replace_playing = -1
        # 推迟填充（面板隐藏时）：列表内容作废，只记播放行
        self._deferred = False
        # 最近一次成功应用的列表（按对象身份留档）。下次 set_items 先跟它
        # 比一次，同序同项就只更新播放行——省掉从控件回读三十万行做对比
        # （实测每次点开视频 420-510ms，回读本身就是 30 万次 Qt 往返）。
        # 用户在面板里拖拽/上下移动/删除会绕开这里改行，那些地方调
        # invalidate_applied() 作废留档，退回回读对比。
        self._applied: list[MediaItem] | None = None

    @property
    def is_filling(self) -> bool:
        """后台填充进行中：items() 回读不完整，重排/删除必须被拦下。

        推迟填充（面板隐藏）期间同样为真：此时列表里还是上一份内容，
        回读它会把播放列表写坏。
        """
        return self._fill_timer.isActive() or self._deferred

    def mark_deferred(self, playing: int) -> None:
        """面板隐藏：本次填充整份推迟到显示时（见 PlaylistPanel.showEvent）。

        列表里的旧行原样留着（清掉三十万行本身就是要避开的开销），只记住
        播放行；is_filling 为真挡住一切回读，显示时再按最新内容填。
        """
        if self._replace_phase or self._fill_timer.isActive():
            self._applied = None   # 半成品：留档不再代表控件里的内容
        self._fill_timer.stop()
        self._replace_phase = False
        self._replace_source = []
        self._fill_source = []
        self._fill_pos = 0
        self._scroll_pending_row = -1
        self._deferred = True
        self.playing_row = playing

    def take_deferred_playing(self) -> int:
        """交出推迟期间记下的播放行，并解除推迟状态。"""
        self._deferred = False
        return self.playing_row

    def invalidate_applied(self) -> None:
        """用户在面板里直接改了行（拖拽/上下移动/删除）：留档作废。

        留档只用来判断"内容没变、什么都不用干"，作废后退回从控件回读的
        全行对比——慢一点，但绝不会把控件与播放列表判成一致却其实错位。
        """
        self._applied = None

    def _matches_applied(self, items: list[MediaItem]) -> bool:
        """与留档同序同项？三十万行身份对比约 10-20ms（回读对比要 450ms）。"""
        applied = self._applied
        if applied is None or len(applied) != len(items):
            return False
        for a, b in zip(applied, items):
            if a is not b:
                return False
        return True

    def _retarget_playing(self, playing: int, total: int) -> None:
        """内容不变、只换播放行：不碰任何行，正在跑的分块填充照常继续。"""
        self.playing_row = playing
        if 0 <= playing < self.count():
            self.scrollToItem(self.item(playing), QAbstractItemView.EnsureVisible)
        elif 0 <= playing < total:
            self._scroll_pending_row = playing
        self.viewport().update()

    def _resume_fill(self, items: list[MediaItem], playing: int) -> bool:
        """分块填充/替换途中来了新内容：能原地续填就续，别同步补齐。

        控件里此刻只有已填的前 _fill_pos 行，把它当"旧列表"做增量对比，
        剩下十几万行会被判成"新增"并同步插进去——实测 125000→309054 一次
        插了 2.1s，正好砸在点开视频那一瞬间（mpv 画面同在 GUI 线程渲染）。
        已填前缀与新列表逐项同源时，只换填充源、保留 _fill_pos 继续填。
        """
        if self._replace_phase:
            # 清空阶段：一行都还没按源填，直接换目标接着排空
            self._replace_source = list(items)
            self._replace_playing = playing
            self._applied = list(items)
            self._fill_timer.start(self._chunk_interval())
            return True
        pos = self._fill_pos
        if pos != self.count() or pos > len(items) or len(self._fill_source) < pos:
            return False
        for i in range(pos):
            if self._fill_source[i] is not items[i]:
                return False
        self._fill_source = list(items)
        self._applied = list(items)
        self.playing_row = playing
        if 0 <= playing < self.count():
            self.scrollToItem(self.item(playing), QAbstractItemView.EnsureVisible)
        elif 0 <= playing < len(items):
            self._scroll_pending_row = playing
        self._fill_timer.start(self._chunk_interval())
        from . import startup_log

        startup_log.stage(
            "panel-fill",
            f"续填 {pos}/{len(items)} 行（免去 {len(items) - pos} 行同步补齐）")
        return True

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

    def _make_entry(self, it: MediaItem) -> QListWidgetItem:
        h = ROW_H_THUMB if self.thumb_mode else ROW_H_COMPACT
        entry = QListWidgetItem()
        entry.setData(ITEM_ROLE, it)
        entry.setSizeHint(QSize(60, h))
        entry.setToolTip(str(it.path))
        if self._filter_needle and self._filter_needle not in it.name.lower():
            entry.setHidden(True)
        return entry

    def _append_rows(self, start: int, end: int) -> None:
        """把 source[start:end) 顺序追加（行号==序号的唯一入口）。"""
        self.setUpdatesEnabled(False)
        try:
            for it in self._fill_source[start:end]:
                self.addItem(self._make_entry(it))
        finally:
            self.setUpdatesEnabled(True)
        self._fill_pos = end

    def _fill_chunk(self) -> None:
        if self._replace_phase:
            import time as _time

            t0 = _time.perf_counter()
            target = max(0, self.count() - self._chunk_rows)
            # 尾部移除是 O(1) 级操作：一批一批把旧行请出去，GUI 每块之间
            # 照常处理事件，绝无未响应
            while self.count() > target:
                self.takeItem(self.count() - 1)
            elapsed_ms = (_time.perf_counter() - t0) * 1000
            if elapsed_ms > self.CHUNK_SLOW_MS and self._chunk_rows > self.CHUNK_MIN_ROWS:
                self._chunk_rows = self._chunk_rows // 2
            if self.count():
                self._fill_timer.start(self._chunk_interval())
                return
            # 清空完成 → 接常规大列表填充（立即窗口先填播放行附近）
            self._replace_phase = False
            items = self._replace_source
            self._replace_source = []
            playing = self._replace_playing
            self._filter_needle = ""
            self._had_filter = False
            self.setUpdatesEnabled(False)
            try:
                self.blockSignals(True)
                self._fill_source = list(items)
                self._fill_pos = 0
                self._chunk_rows = self.CHUNK_ROWS
                self.playing_row = playing
                immediate = min(self.IMMEDIATE_MAX,
                                max(self.IMMEDIATE_MIN, playing + self.IMMEDIATE_PLAY_AHEAD))
                immediate = max(immediate, 0)
                self._append_rows(0, immediate)
                self.blockSignals(False)
            finally:
                self.setUpdatesEnabled(True)
            if 0 <= playing < self.count():
                self.scrollToItem(self.item(playing), QAbstractItemView.EnsureVisible)
            else:
                self._scroll_pending_row = playing
            self.viewport().update()
            self._fill_timer.start(self._chunk_interval())
            return
        if self._fill_pos >= len(self._fill_source):
            self._finish_fill()
            return
        import time as _time

        t0 = _time.perf_counter()
        end = min(len(self._fill_source), self._fill_pos + self._chunk_rows)
        self._append_rows(self._fill_pos, end)
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        # 自适应：单块太慢（列表越大 addItem 越贵）→ 下块减半，间隙封顶
        if elapsed_ms > self.CHUNK_SLOW_MS and self._chunk_rows > self.CHUNK_MIN_ROWS:
            self._chunk_rows = self._chunk_rows // 2
        # 播放行此前不在已填充范围：现在够到了，补滚动
        if 0 <= self._scroll_pending_row < self.count():
            row = self._scroll_pending_row
            self._scroll_pending_row = -1
            self.scrollToItem(self.item(row), QAbstractItemView.EnsureVisible)
            self.viewport().update()
        if self._fill_pos >= len(self._fill_source):
            self._finish_fill()
            return
        self._fill_timer.start(self._chunk_interval())

    def _finish_fill(self) -> None:
        self._fill_source = []
        self._fill_pos = 0
        self._fill_timer.stop()
        # 恢复拖拽重排（填充期间禁用：dropEvent 回读的是不完整列表）
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.viewport().update()

    def _log_sync_fallback(self, total: int) -> None:
        from . import startup_log

        startup_log.stage(
            "panel-fill",
            f"增量同步放弃（顺序变化/混合增删），回退全量重填 {total} 行")

    def _sync_incremental(self, items: list[MediaItem], playing: int) -> bool:
        """同序增删的增量同步：旧列表在新列表里保持原相对顺序且连续时，
        只插入新行/删除消失的行，不清空重填（三十万行 clear 实测 19.4s）。

        返回是否已应用；乱序/重排返回 False 由调用方走全量路径。
        """
        import time as _time

        t0 = _time.perf_counter()
        old_count = self.count()
        old = [self.item(i).data(ITEM_ROLE) for i in range(old_count)]
        old_pos = {id(it): i for i, it in enumerate(old)}
        matched = [old_pos.get(id(it), -1) for it in items]
        # 纯新增：旧项全部按原顺序出现（下标严格 0,1,2,… 连续）；
        # 纯删除：新项全是旧对象且相对顺序保持（下标严格递增）。
        # 其余（重排/既有增又有删/洗牌）→ 放弃，回退全量重填。
        if len(items) > old_count:
            expect = 0
            for m in matched:
                if m == -1:
                    continue
                if m != expect:
                    self._log_sync_fallback(len(items))
                    return False
                expect += 1
            if expect != old_count:
                self._log_sync_fallback(len(items))
                return False
        elif len(items) < old_count:
            prev = -1
            for m in matched:
                if m == -1 or m <= prev:
                    self._log_sync_fallback(len(items))
                    return False
                prev = m
        else:
            # 等长：全行按原顺序一一对应才算"无变化"，只更新播放行；
            # 任何错位都是重排 → 回退全量
            prev = -1
            for m in matched:
                if m == -1 or m <= prev:
                    self._log_sync_fallback(len(items))
                    return False
                prev = m
            self._fill_source = []
            self._fill_pos = 0
            self._scroll_pending_row = -1
            self._chunk_rows = self.CHUNK_ROWS
            self._applied = list(items)
            self.playing_row = playing
            if 0 <= playing < self.count():
                self.scrollToItem(self.item(playing), QAbstractItemView.EnsureVisible)
            return True
        self.setUpdatesEnabled(False)
        try:
            self.blockSignals(True)
            if len(items) > old_count:
                # 纯新增：顺序遍历，新项插入到当前行位置
                r = 0
                for it, m in zip(items, matched):
                    if m == -1:
                        self.insertItem(r, self._make_entry(it))
                    r += 1
            elif len(items) < old_count:
                # 纯减少：倒序删除消失的行
                keep = set(matched)
                for i in range(old_count - 1, -1, -1):
                    if i not in keep:
                        self.takeItem(i)
        finally:
            self.blockSignals(False)
            self.setUpdatesEnabled(True)
        self._fill_source = []
        self._fill_pos = 0
        self._scroll_pending_row = -1
        self._chunk_rows = self.CHUNK_ROWS
        self._applied = list(items)
        self.playing_row = playing
        if 0 <= playing < self.count():
            self.scrollToItem(self.item(playing), QAbstractItemView.EnsureVisible)
        self.viewport().update()
        from . import startup_log

        startup_log.stage(
            "panel-fill",
            f"增量同步 {old_count}→{len(items)} 行"
            f" {(_time.perf_counter() - t0) * 1000:.0f}ms")
        return True

    def set_items(self, items: list[MediaItem], playing: int = -1) -> None:
        total = len(items)
        # 留档命中（同序同项）：内容压根没变，只换播放行。这是点开视频最常
        # 走的一条——播放列表就是浏览器那份，一字没动。此时绝不能停正在跑
        # 的分块填充：目标内容一致，停了列表就永远停在半成品上。
        if (total and not self._filter_needle and not self._deferred
                and self._matches_applied(items)):
            self._retarget_playing(playing, total)
            return
        filling = self._fill_timer.isActive() or self._replace_phase
        self._fill_timer.stop()
        # 填充途中内容真变了：控件里只有半份，先试原地续填（见 _resume_fill）
        if filling and self._resume_fill(items, playing):
            return
        # 增量同步：无变化（只更新播放行）/纯新增/纯减少在这里解决，
        # 顺序变化返回 False 走下面的全量路径。按对象身份全行对比——
        # 此前的"首行+播放行"两行快路径会漏判局部行序变化，让面板与
        # 播放器列表脱钩（行号≠序号，双击错位），已废弃。30 万行对比
        # 约几百 ms，对比 clear 重填的 20s 冻结可忽略。
        # filling 时禁走：那时回读到的是半成品（见 _resume_fill 的注释）。
        if (total and not self._filter_needle and self.count()
                and not filling and not self._deferred
                and self._sync_incremental(items, playing)):
            return
        # 顺序变化/混合增删的大列表：绝不同步 clear()（三十万行实测 20s
        # 冻结——元数据排序 + 5s 定时归位让顺序频繁微变，这条路径常走）。
        # 分块替换：每次 tick 从尾部廉价移除一批旧行，清空后接常规分块
        # 填充（立即窗口先填播放行附近）。is_filling 全程为真，拖拽/重排
        # 守卫自动生效。
        if self.count() > self.SYNC_MAX:
            from . import startup_log

            startup_log.stage(
                "panel-fill",
                f"分块替换 {self.count()}→{total} 行（顺序变化/混合增删）")
            self._replace_source = list(items)
            self._replace_playing = playing
            self._replace_phase = True
            self._applied = list(items)   # 留档＝要收敛到的内容（不是当前行）
            self.playing_row = -1
            self._fill_source = []
            self._fill_pos = 0
            self._scroll_pending_row = -1
            self.setDragDropMode(QAbstractItemView.NoDragDrop)
            self._fill_timer.start(self._chunk_interval())
            return
        import time as _time

        t0 = _time.perf_counter()
        self.setUpdatesEnabled(False)
        try:
            self.blockSignals(True)
            old_count = self.count()
            self.clear()
            clear_ms = (_time.perf_counter() - t0) * 1000
            self._fill_source = list(items)
            self._fill_pos = 0
            self._filter_needle = ""
            self._had_filter = False
            self._scroll_pending_row = -1
            self._chunk_rows = self.CHUNK_ROWS
            self._applied = list(items)
            self.playing_row = playing
            if total <= self.SYNC_MAX:
                # 小列表：与旧版完全一致的同步路径
                if total:
                    self._append_rows(0, total)
                self.blockSignals(False)
                return
            # 大列表：立即填播放行附近（封顶），其余后台分块
            immediate = min(self.IMMEDIATE_MAX,
                            max(self.IMMEDIATE_MIN, playing + self.IMMEDIATE_PLAY_AHEAD))
            immediate = max(immediate, 0)
            self._append_rows(0, immediate)
            self.blockSignals(False)
        finally:
            self.setUpdatesEnabled(True)
        if clear_ms > 100:
            from . import startup_log

            startup_log.stage(
                "panel-fill",
                f"clear() {old_count} 行耗时 {clear_ms:.0f}ms（重填 {total} 行）")
        self.viewport().update()
        if 0 <= playing < self.count():
            self.scrollToItem(self.item(playing), QAbstractItemView.EnsureVisible)
        elif 0 <= playing < total:
            # 播放行还没填到：先记下，_fill_chunk 够到时再滚
            self._scroll_pending_row = playing
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self._fill_timer.start(self._chunk_interval())

    def items(self) -> list[MediaItem]:
        return [self.item(i).data(ITEM_ROLE) for i in range(self.count())]

    def set_playing(self, row: int, scroll: bool = True) -> None:
        self.playing_row = row
        if self._deferred:
            return  # 填充推迟中：只记播放行，显示时按它定位
        if scroll and 0 <= row < self.count():
            self.scrollToItem(self.item(row), QAbstractItemView.EnsureVisible)
        elif scroll and row >= self.count() and self.is_filling:
            # 行还没填到：交给填充循环滚动（EnsureVisible 到已填末行也没意义）
            self._scroll_pending_row = row
        self.viewport().update()

    def dropEvent(self, e):
        if self.is_filling:
            return  # 填充期禁拖拽的双保险（setDragDropMode 已关）
        super().dropEvent(e)
        self.invalidate_applied()   # 行序被用户改了：留档不再代表控件
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
        if self.list.is_filling:
            # 后台填充中：items() 只有已填充部分，回读会把十万条截断成几千条
            self._toast(t("panel.list_filling"))
            return
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
            p0 = paths[0]
            if p0.suffix.lower() in media.IMAGE_EXTS:
                menu.addAction(t("menu.copy_image")).triggered.connect(
                    lambda _=False, p=p0: fileops.copy_image_to_clipboard(p)
                )
            menu.addAction(t("menu.copy_file")).triggered.connect(
                lambda _=False, p=p0: fileops.copy_files_to_clipboard([p])
            )
            menu.addAction(t("panel.rename_ellipsis")).triggered.connect(
                lambda _=False, p=paths[0]: self._rename(p)
            )
        elif paths:
            menu.addAction(t("menu.copy_file")).triggered.connect(
                lambda _=False, ps=tuple(paths): fileops.copy_files_to_clipboard(list(ps))
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
        # 列表与 _all_items 必须一起更新（对比 _remove_selected）：只发信号会
        # 让被删的行留在面板里，之后所有行号 → _all_items 的映射整体错位。
        # 高亮按 path 在**删除后**的列表里重算（删掉的项在它前面时下标会前移）
        row = self._current_index()
        playing = self._all_items[row].path if 0 <= row < len(self._all_items) else None
        new_row = next((i for i, x in enumerate(remaining) if x.path == playing), -1) \
            if playing is not None else -1
        self.set_playlist(remaining, new_row)
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
        row = getattr(self.list, "playing_row", -1)
        playing = None
        if 0 <= row < self.list.count():
            cur_item = self.list.item(row)
            playing = cur_item.data(ITEM_ROLE).path if cur_item is not None else None
        items = media.sort_items(self._all_items, key, desc)
        self._all_items = list(items)
        self.list.set_items(self._all_items, -1)
        self._apply_filter(self.search.text())
        # 排序后行号全变了：按 path 重新定位"正在播放"。原实现回填排序前的旧
        # 行号，高亮会落在一个不相干的文件上
        new_row = -1
        if playing is not None:
            for i in range(self.list.count()):
                if self.list.item(i).data(ITEM_ROLE).path == playing:
                    new_row = i
                    break
        self.list.set_playing(new_row)
        # shared with the main window: persist + tell it to re-sort the browser
        settings["sort_key"] = key
        settings["sort_desc"] = desc
        # 播放器也要同步新顺序：只发 sort_requested 时 viewer.items 仍是旧序，
        # 之后双击任一行播的都是错误的文件
        self.playlist_reordered.emit(self._all_items)
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
        # 过滤词记到控件上：后台分块填充的新行在 _make_entry 里同步应用，
        # 否则大列表边填边过滤会漏掉未填到的行
        lst._filter_needle = needle
        if not needle:
            # 空过滤：新 set 的行从不隐藏，跳过全量扫（十万行白花 0.2s）
            # 只有此前有过滤词时才需要解除隐藏
            if getattr(lst, "_had_filter", False):
                for i in range(lst.count()):
                    lst.item(i).setHidden(False)
                lst._had_filter = False
            return
        lst._had_filter = True
        for i in range(lst.count()):
            entry = lst.item(i)
            item = entry.data(ITEM_ROLE)
            entry.setHidden(needle not in item.name.lower())

    def _move_selected(self, delta: int) -> None:
        lst = self._active_list()
        rows = sorted({i.row() for i in lst.selectedIndexes()}, reverse=delta > 0)
        if not rows:
            return
        for row in rows:
            target = row + delta
            if not (0 <= target < lst.count()):
                return
        if lst.is_filling:
            # 填充中移动：_on_reordered 的 items() 回读不完整
            self._toast(t("panel.list_filling"))
            return
        moved = []
        for row in rows:
            entry = lst.takeItem(row)
            lst.insertItem(row + delta, entry)
            moved.append(row + delta)
        lst.invalidate_applied()
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
        if lst.is_filling:
            # 填充中删除：items() 回读不完整，会把列表截断
            self._toast(t("panel.list_filling"))
            return
        for row in rows:
            lst.takeItem(row)
        lst.invalidate_applied()
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

    def set_thumbs_paused(self, paused: bool) -> None:
        """Low-priority mode: stop requesting new thumbnails (playback first)."""
        self.list.set_thumbs_paused(paused)

    def set_playlist(self, items: list[MediaItem], current: int) -> None:
        # 直接沿用播放器给的顺序（它本身就是浏览器排好序的那一份）。这里绝不能
        # 自己再排一次：面板拿不到主窗口的随机种子（sort_items 默认 seed=0）、
        # settings["sort_key"] 也可能还没同步，重排后 _all_items 与 viewer.items
        # 顺序脱钩——双击第 N 行播的是另一个文件，"正在播放"高亮也落错行
        self._all_items = list(items)
        cur = current if 0 <= current < len(self._all_items) else -1
        # 面板隐藏（用户没开列表）＋大列表：整份填充推迟到面板显示时。
        # 三十万行的分块替换全落在 GUI 线程上，而 mpv 画面同样在 GUI 线程
        # 渲染（QOpenGLWidget.paintGL）——起播瞬间做这活儿直接掉帧，而
        # 列表此刻根本不可见。
        if not self.isVisible() and len(self._all_items) > self.list.SYNC_MAX:
            self.list.mark_deferred(cur)
            from . import startup_log

            startup_log.stage(
                "panel-fill",
                f"面板隐藏：{len(self._all_items)} 行填充推迟到显示时")
            return
        self.list.set_items(self._all_items, cur)
        self._apply_filter(self.search.text())
        self.list.set_playing(cur)

    def showEvent(self, e):
        super().showEvent(e)
        # 隐藏期间推迟掉的填充在这里补做（播放列表面板刚被打开）
        if self.list.is_filling and self.list._deferred and self._all_items:
            cur = self.list.take_deferred_playing()
            self.list.set_items(self._all_items, cur)
            self._apply_filter(self.search.text())
            self.list.set_playing(cur)

    def set_current(self, index: int) -> None:
        target = self._all_items[index].path if 0 <= index < len(self._all_items) else None
        if target is None:
            self.list.set_playing(-1)
            return
        if self.list.is_filling:
            # 填充中/推迟中：行号==播放序号（顺序追加保持不变式），直接用
            # index——去扫尚不完整/已作废的旧内容既没意义又是 O(n)
            self.list.set_playing(index)
            return
        # 快路径：没被用户拖动重排时行号==序号，O(1) 命中。逐行 data() 扫描
        # 在三十万行上是 GUI 线程的百毫秒级开销，而每次换片都要付一次。
        if 0 <= index < self.list.count() \
                and self.list.item(index).data(ITEM_ROLE).path == target:
            self.list.set_playing(index)
            return
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
