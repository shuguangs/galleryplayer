"""Browsing views: a custom-painted tile view (grid / waterfall) and a details table.

The tile view paints directly instead of using QListView + delegate so that
waterfall mode can give every item its own height, and so several hundred items
stay cheap (no widget per tile).
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QAbstractScrollArea, QTableView, QHeaderView

from . import theme
from .i18n import t
from .media import MediaItem, format_duration
from .thumbs import ThumbnailCache

TILE_GAP = 10
TILE_PAD = 6
NAME_ROW_H = 34
MIN_TILE_W = 96
# 4:3 rather than 16:9: a cover-cropped portrait photo keeps much more of its
# subject, and video thumbnails still read fine with slim side bars.
GRID_TILE_ASPECT = 3 / 4


class MediaModel(QAbstractTableModel):
    """Backing store for both views. Columns are only used by the details table."""

    # Column keys are i18n keys; headerData() renders them via t().
    COLUMNS = [
        "browser.col_name",
        "browser.col_type",
        "browser.col_size",
        "browser.col_resolution",
        "browser.col_duration",
        "browser.col_mtime",
    ]
    sort_requested = Signal(str, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[MediaItem] = []

    def set_items(self, items: list[MediaItem], reset_scroll: bool = True) -> None:
        """Replace the listing.

        reset_scroll=True: a different folder is on screen — go back to the top.
        reset_scroll=False: the same folder is being re-sorted — the tile view
        (via modelReset) anchors on the first visible item instead.
        """
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def item(self, row: int) -> MediaItem | None:
        return self.items[row] if 0 <= row < len(self.items) else None

    # -- QAbstractTableModel ---------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return t(self.COLUMNS[section])
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return it.name
            if col == 1:
                return t("browser.type_video") if it.is_video else t("browser.type_image")
            if col == 2:
                return it.size_text()
            if col == 3:
                return it.resolution_text()
            if col == 4:
                return it.duration_text() if it.is_video else ""
            if col == 5:
                import datetime

                return datetime.datetime.fromtimestamp(it.mtime).strftime("%Y-%m-%d %H:%M")
        elif role == Qt.TextAlignmentRole and col in (2, 3, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ForegroundRole and col != 0:
            return QBrush(QColor(theme.TEXT_DIM))
        elif role == Qt.ToolTipRole:
            return str(it.path)
        return None

    def sort(self, column, order=Qt.AscendingOrder):
        key = {0: "name", 1: "name", 2: "size", 3: "size", 4: "duration", 5: "mtime"}.get(
            column, "name"
        )
        self.sort_requested.emit(key, order == Qt.DescendingOrder)


class DetailsView(QTableView):
    activatedRow = Signal(int)
    contextRow = Signal(int, QPoint)   # row (-1 for empty space), global position

    def __init__(self, model: MediaModel, parent=None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.setSelectionBehavior(QTableView.SelectRows)
        self.setSelectionMode(QTableView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(28)
        hh = self.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(MediaModel.COLUMNS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.doubleClicked.connect(lambda idx: self.activatedRow.emit(idx.row()))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            idx = self.currentIndex()
            if idx.isValid():
                self.activatedRow.emit(idx.row())
                return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e):
        idx = self.indexAt(e.pos())
        if idx.isValid():
            self.selectRow(idx.row())
        self.contextRow.emit(idx.row() if idx.isValid() else -1, e.globalPos())


class TileView(QAbstractScrollArea):
    """Grid (uniform, cover-cropped) or waterfall (aspect-preserving columns).

    Grid geometry is computed from the row index rather than stored. Keeping a QRect
    per item meant every relayout — model reset, resize, a nudge of the column slider —
    allocated one object per file, and both hit-testing and "which tiles are on screen"
    walked the whole list. At twenty thousand files that was a third of a second of
    freeze per relayout and a linear scan on every repaint and every mouse move.

    Waterfall really does need the list, because each tile's height depends on every
    tile packed before it, so there it keeps one — alongside a top-sorted index so the
    same two questions stay logarithmic instead of linear.
    """

    activatedRow = Signal(int)
    currentRowChanged = Signal(int)
    contextRow = Signal(int, QPoint)   # row (-1 for empty space), global position

    def __init__(self, model: MediaModel, thumbs: ThumbnailCache, parent=None) -> None:
        super().__init__(parent)
        self.model = model
        self.thumbs = thumbs
        self.thumbs_suspended = False  # archive mode: skip thumbnail decode
        self.mode = "grid"
        self.columns = 5
        # waterfall only; grid computes its geometry on demand
        self._rects: list[QRect] = []
        self._wf_order: list[int] = []      # row indices, sorted by tile top
        self._wf_tops: list[int] = []       # their tops, for bisect
        self._tile_w = MIN_TILE_W
        self._tile_h = MIN_TILE_W
        self._content_h = 0
        self._current = -1
        self._hover = -1
        self._key_to_row: dict[str, int] = {}
        # 同文件夹重排（改排序/筛选/流式增量）时的滚动保持：锚定项（同集合
        # 重排）或像素偏移（流式增量）；锚定项消失时按滚动比例兜底。
        # 三者皆 None=全新内容（换文件夹），_on_model_reset 回顶部
        self._anchor_item: MediaItem | None = None
        self._keep_offset_px: int | None = None
        self._keep_scroll_ratio: float | None = None
        self._keep_anchor_visible = False  # 锚定项仅保持可见（meta 回填重排）
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().setAutoFillBackground(True)
        self.viewport().setStyleSheet(f"background:{theme.BG_BASE};")
        self.verticalScrollBar().setSingleStep(48)
        self.model._view_ref = self
        self.model.modelReset.connect(self._on_model_reset)
        self.thumbs.ready.connect(self._on_thumb_ready)
        self.thumbs.meta_ready.connect(self._on_meta_ready)

    # -- configuration ----------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.relayout()

    def set_columns(self, n: int) -> None:
        n = max(1, min(14, n))
        if n != self.columns:
            self.columns = n
            self.relayout()

    def current_row(self) -> int:
        return self._current

    def set_current_row(self, row: int, scroll: bool = True) -> None:
        if not (0 <= row < len(self.model.items)):
            row = -1
        if row == self._current:
            return
        self._current = row
        if scroll and row >= 0:
            self.scroll_to(row)
        self.currentRowChanged.emit(row)
        self.viewport().update()

    def scroll_to(self, row: int) -> None:
        if not (0 <= row < len(self.model.items)):
            return
        r = self.rect_for(row)
        bar = self.verticalScrollBar()
        top, bottom = bar.value(), bar.value() + self.viewport().height()
        if r.top() < top:
            bar.setValue(max(0, r.top() - TILE_GAP))
        elif r.bottom() > bottom:
            bar.setValue(r.bottom() - self.viewport().height() + TILE_GAP)

    # -- layout -----------------------------------------------------------

    def _on_model_reset(self) -> None:
        self._key_to_row = {it.cache_key: i for i, it in enumerate(self.model.items)}
        anchor = self._anchor_item
        offset = self._keep_offset_px
        ratio = self._keep_scroll_ratio
        keep_visible = self._keep_anchor_visible
        self._anchor_item = None
        self._keep_offset_px = None
        self._keep_scroll_ratio = None
        self._keep_anchor_visible = False
        self._current = -1
        self._hover = -1
        if anchor is not None:
            self.relayout()
            row = self._key_to_row.get(anchor.cache_key)
            if row is not None:
                r = self.rect_for(row)
                bar = self.verticalScrollBar()
                if keep_visible:
                    # meta 回填式重排（时长排序下项陆续搬家）：锚定项保持
                    # 在视口内即可（EnsureVisible 最小滚动）——对齐视口顶
                    # 会每批跳一次；保持像素偏移则内容大换血。
                    # 锚定项保留：下一轮 meta 重排继续跟随同一个项。
                    self._anchor_item = anchor
                    top, bottom = bar.value(), bar.value() + self.viewport().height()
                    if r.top() < top:
                        bar.setValue(max(0, r.top() - TILE_GAP))
                    elif r.bottom() > bottom:
                        bar.setValue(r.bottom() - self.viewport().height() + TILE_GAP)
                else:
                    # 用户主动改排序/筛选：锚定项对齐到视口顶——正在看的
                    # 内容保持在原地，其下按新顺序展开。EnsureVisible 语义
                    # 不够：排序反转时项的位置剧变，最小滚动拉不回视口。
                    bar.setValue(max(0, r.top() - TILE_GAP))
                return
            # 锚定项不在新列表（被筛选掉）：退回比例定位，绝不落在尾部
            self._scroll_to_ratio(ratio)
            return
        if offset is not None:
            self.relayout()
            # 流式增量（内容增长）：保持像素偏移，用户在哪就还在哪
            bar = self.verticalScrollBar()
            bar.setValue(min(offset, bar.maximum()))
            return
        # 全新内容（换文件夹）：回顶部
        self.verticalScrollBar().setValue(0)
        self.relayout()

    def _scroll_to_ratio(self, ratio: float | None) -> None:
        """按滚动比例恢复位置（锚定项消失时的兜底）。

        绝对偏移在内容变短时会被钳到 maximum（= 跳到尾部，实测大文件夹
        改排序时出现）；比例保持"看到的大致位置"。
        """
        bar = self.verticalScrollBar()
        if bar.maximum() <= 0:
            bar.setValue(0)
            return
        r = 0.5 if ratio is None else min(1.0, max(0.0, ratio))
        bar.setValue(int(bar.maximum() * r))

    def _first_visible_item(self) -> MediaItem | None:
        """视口顶部第一个可见项（重排前的锚点）。"""
        items = self.model.items
        if not items:
            return None
        off = self.verticalScrollBar().value()
        for row in self._visible_rows():
            if 0 <= row < len(items):
                return items[row]
        return None

    def set_items_keep_scroll(self, items: list[MediaItem]) -> None:
        """同集合重排入口：锚定视口首项 → set_items → 滚到它的新位置。

        同时记录滚动比例：锚定项被筛选掉时（改筛选条件）用它兜底——
        绝对偏移会被钳到新 maximum（= 跳到尾部）。
        """
        bar = self.verticalScrollBar()
        self._keep_scroll_ratio = (
            bar.value() / bar.maximum()) if bar.maximum() > 0 else 0.0
        self._anchor_item = self._first_visible_item()
        self.model.set_items(items, reset_scroll=False)

    def set_items_keep_anchor_visible(self, items: list[MediaItem]) -> None:
        """meta 回填式重排入口：锚定项保持在视口内（最小滚动）。

        时长排序下 meta 陆续到达会让项大搬家：对齐视口顶会每批跳一次，
        保持像素偏移则视口内容大换血——两者都不是用户想要的。
        EnsureVisible 式的最小滚动让"正在看的那个"始终在屏上。

        锚定项沿用既有值（用户切排序时设定的），不随视口重锚——重锚
        会累积跟随误差，10 轮搬家后锚定项仍可能漂出视口（实测）。
        """
        if self._anchor_item is None:
            self._anchor_item = self._first_visible_item()
        self._keep_scroll_ratio = None
        self._keep_anchor_visible = True
        self.model.set_items(items, reset_scroll=False)

    def set_items_keep_offset(self, items: list[MediaItem]) -> None:
        """流式增量入口：保持滚动像素偏移 → set_items（内容增长不推走视口）。"""
        self._keep_offset_px = self.verticalScrollBar().value()
        self.model.set_items(items, reset_scroll=False)

    def relayout(self) -> None:
        items = self.model.items
        w = max(MIN_TILE_W, self.viewport().width() - TILE_GAP)
        cols = max(1, self.columns)
        self._tile_w = max(MIN_TILE_W, (w - TILE_GAP * (cols - 1)) // cols - 1)

        if self.mode == "waterfall":
            heights = [TILE_GAP] * cols
            rects = []
            for it in items:
                c = heights.index(min(heights))
                img_h = int(self._tile_w / max(0.25, min(4.0, it.aspect)))
                img_h = max(60, min(int(self._tile_w * 2.2), img_h))
                h = img_h + TILE_PAD * 2
                x = TILE_GAP + c * (self._tile_w + TILE_GAP)
                rects.append(QRect(x, heights[c], self._tile_w, h))
                heights[c] += h + TILE_GAP
            self._rects = rects
            # Tiles are appended in column-fill order, so the list is not sorted by top;
            # this index is what lets _visible_rows() bisect instead of scan.
            self._wf_order = sorted(range(len(rects)), key=lambda i: rects[i].top())
            self._wf_tops = [rects[i].top() for i in self._wf_order]
            self._content_h = max(heights) if items else 0
        else:
            self._rects = []
            self._wf_order = []
            self._wf_tops = []
            self._tile_h = int(self._tile_w * GRID_TILE_ASPECT) + NAME_ROW_H + TILE_PAD * 2
            rows = (len(items) + cols - 1) // cols
            self._content_h = TILE_GAP + rows * (self._tile_h + TILE_GAP) if items else 0

        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, self._content_h - self.viewport().height()))
        bar.setPageStep(self.viewport().height())
        self.viewport().update()

    def rect_for(self, row: int) -> QRect:
        """Content-space geometry of one tile."""
        if self.mode == "waterfall":
            return self._rects[row] if 0 <= row < len(self._rects) else QRect()
        cols = max(1, self.columns)
        r, c = divmod(row, cols)
        return QRect(
            TILE_GAP + c * (self._tile_w + TILE_GAP),
            TILE_GAP + r * (self._tile_h + TILE_GAP),
            self._tile_w,
            self._tile_h,
        )

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.relayout()

    # -- hit testing ------------------------------------------------------

    def _row_at(self, pos: QPoint) -> int:
        p = QPoint(pos.x(), pos.y() + self.verticalScrollBar().value())
        n = len(self.model.items)
        if self.mode == "waterfall":
            # Only the tiles near the cursor's band can contain it.
            for i in self._visible_rows():
                if self._rects[i].contains(p):
                    return i
            return -1
        if n == 0:
            return -1
        cols = max(1, self.columns)
        col, x_in = divmod(p.x() - TILE_GAP, self._tile_w + TILE_GAP)
        row, y_in = divmod(p.y() - TILE_GAP, self._tile_h + TILE_GAP)
        # divmod on a negative offset lands in the gap before the first tile
        if p.x() < TILE_GAP or p.y() < TILE_GAP or not (0 <= col < cols):
            return -1
        if x_in >= self._tile_w or y_in >= self._tile_h:
            return -1  # in the gutter between tiles
        i = row * cols + col
        return i if 0 <= i < n else -1

    def _visible_rows(self):
        """Row indices whose tiles are on screen, plus a margin either way."""
        n = len(self.model.items)
        if n == 0:
            return range(0)
        off = self.verticalScrollBar().value()
        # a screenful of slack either way, so scrolling has thumbnails ready
        top, bottom = off - 200, off + self.viewport().height() + 400

        if self.mode == "waterfall":
            if not self._wf_tops:
                return range(0)
            # `_wf_order` is sorted by tile top, so the first tile that can still reach
            # the band is a bisect away — offset by the tallest a tile may be, since a
            # tall one starting above `top` can still cross into view.
            first = bisect_left(self._wf_tops, top - int(self._tile_w * 2.4) - TILE_PAD * 2)
            last = bisect_right(self._wf_tops, bottom)
            return [self._wf_order[k] for k in range(max(0, first), min(len(self._wf_order), last))]

        cols = max(1, self.columns)
        pitch = self._tile_h + TILE_GAP
        first_row = max(0, (top - TILE_GAP) // pitch)
        last_row = (bottom - TILE_GAP) // pitch
        return range(min(n, first_row * cols), min(n, (last_row + 1) * cols))

    # -- painting ---------------------------------------------------------

    def _on_thumb_ready(self, key: str, _img: QImage) -> None:
        if key in self._key_to_row:
            self.viewport().update()

    def _on_meta_ready(self, key: str, meta) -> None:
        row = self._key_to_row.get(key)
        if row is None:
            return
        it = self.model.items[row]
        it.duration, it.width, it.height = meta
        if self.mode == "waterfall":
            self.relayout()
        else:
            self.viewport().update()

    def paintEvent(self, e):
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.viewport().rect(), QColor(theme.BG_BASE))
        off = self.verticalScrollBar().value()

        if not self.model.items:
            p.setPen(QColor(theme.TEXT_FAINT))
            f = p.font()
            f.setPointSize(12)
            p.setFont(f)
            p.drawText(
                self.viewport().rect(),
                Qt.AlignCenter,
                t("browser.empty_folder"),
            )
            return

        rows = self._visible_rows()
        for i in rows:
            it = self.model.items[i]
            rect = self.rect_for(i).translated(0, -off)
            self._paint_tile(p, rect, it, i)
        # keep decode work focused on what the user can actually see
        if not self.thumbs_suspended:
            for i in rows:
                it = self.model.items[i]
                if not it.is_archive:
                    # priority=行号：视口内自上而下、行内自左向右的阅读
                    # 顺序渲染（旧"最新优先"会让视口从右下角往上出图）
                    self.thumbs.request(it, priority=i)

    def _paint_tile(self, p: QPainter, rect: QRect, it: MediaItem, row: int) -> None:
        selected = row == self._current
        hovered = row == self._hover

        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        p.fillPath(path, QColor(theme.BG_HOVER if hovered else theme.GRID_TILE_BG))

        if self.mode == "waterfall":
            img_rect = rect.adjusted(TILE_PAD, TILE_PAD, -TILE_PAD, -TILE_PAD)
        else:
            img_rect = QRect(
                rect.left() + TILE_PAD,
                rect.top() + TILE_PAD,
                rect.width() - TILE_PAD * 2,
                rect.height() - NAME_ROW_H - TILE_PAD * 2,
            )

        thumb = self.thumbs.peek(it)
        clip = QPainterPath()
        clip.addRoundedRect(img_rect, 5, 5)
        p.save()
        p.setClipPath(clip)
        if thumb is not None and not thumb.isNull():
            pix = QPixmap.fromImage(thumb)
            scaled = pix.scaled(
                img_rect.size(),
                Qt.KeepAspectRatioByExpanding if self.mode == "grid" else Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            x = img_rect.left() + (img_rect.width() - scaled.width()) // 2
            y = img_rect.top() + (img_rect.height() - scaled.height()) // 2
            p.fillRect(img_rect, QColor("#0d0e11"))
            p.drawPixmap(x, y, scaled)
        else:
            p.fillRect(img_rect, QColor("#14161a"))
            p.setPen(QColor(theme.TEXT_FAINT))
            f = p.font()
            f.setPointSize(9)
            p.setFont(f)
            p.drawText(img_rect, Qt.AlignCenter, it.suffix.lstrip(".").upper())
        p.restore()

        # badge: duration for video, GIF marker for animations
        badge = ""
        if it.is_video:
            badge = format_duration(it.duration) if it.duration else t("browser.badge_video")
        elif it.is_animated and it.suffix == ".gif":
            badge = "GIF"
        if badge and img_rect.width() > 60:
            f = QFont(p.font())
            f.setPointSize(8)
            f.setBold(True)
            p.setFont(f)
            fm = QFontMetrics(f)
            tw = fm.horizontalAdvance(badge)
            # sit above the waterfall name strip instead of being hidden behind it
            lift = 26 if self.mode == "waterfall" else 0
            br = QRect(
                img_rect.right() - tw - 12,
                img_rect.bottom() - fm.height() - 7 - lift,
                tw + 8,
                fm.height() + 3,
            )
            bp = QPainterPath()
            bp.addRoundedRect(br, 3, 3)
            p.fillPath(bp, QColor(0, 0, 0, 175))
            p.setPen(QColor("#ffffff"))
            p.drawText(br, Qt.AlignCenter, badge)

        # name
        if self.mode == "grid":
            name_rect = QRect(
                rect.left() + TILE_PAD + 2,
                img_rect.bottom() + 3,
                rect.width() - TILE_PAD * 2 - 4,
                NAME_ROW_H - 4,
            )
            f = QFont(p.font())
            f.setPointSize(8)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QColor(theme.TEXT if selected or hovered else theme.TEXT_DIM))
            fm = QFontMetrics(f)
            p.drawText(
                name_rect,
                Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                fm.elidedText(it.name, Qt.ElideMiddle, name_rect.width() * 2),
            )
        elif img_rect.height() > 60:
            # waterfall: name always readable, but only fully opaque under the cursor
            strip = QRect(img_rect.left(), img_rect.bottom() - 24, img_rect.width(), 24)
            p.save()
            p.setClipPath(clip)
            p.fillRect(strip, QColor(0, 0, 0, 185 if (hovered or selected) else 120))
            f = QFont(p.font())
            f.setPointSize(8)
            p.setFont(f)
            p.setPen(QColor("#ffffff" if (hovered or selected) else "#d6dae1"))
            fm = QFontMetrics(f)
            p.drawText(
                strip.adjusted(6, 0, -6, 0),
                Qt.AlignVCenter | Qt.AlignLeft,
                fm.elidedText(it.name, Qt.ElideMiddle, strip.width() - 12),
            )
            p.restore()

        if selected:
            p.setPen(QPen(QColor(theme.ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)

    # -- interaction ------------------------------------------------------

    def mouseMoveEvent(self, e):
        row = self._row_at(e.position().toPoint())
        if row != self._hover:
            self._hover = row
            self.setToolTip(str(self.model.items[row].path) if row >= 0 else "")
            self.viewport().update()

    def leaveEvent(self, e):
        if self._hover != -1:
            self._hover = -1
            self.viewport().update()

    def mousePressEvent(self, e):
        row = self._row_at(e.position().toPoint())
        if row >= 0:
            self.set_current_row(row, scroll=False)
        self.setFocus()

    def mouseDoubleClickEvent(self, e):
        row = self._row_at(e.position().toPoint())
        if row >= 0:
            self.set_current_row(row, scroll=False)
            self.activatedRow.emit(row)

    def contextMenuEvent(self, e):
        row = self._row_at(e.pos())
        if row >= 0:
            # right-clicking a tile selects it, so the menu acts on what was clicked
            self.set_current_row(row, scroll=False)
        self.contextRow.emit(row, e.globalPos())

    def keyPressEvent(self, e):
        n = len(self.model.items)
        if n == 0:
            return super().keyPressEvent(e)
        cur = self._current if self._current >= 0 else 0
        key = e.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if self._current >= 0:
                self.activatedRow.emit(self._current)
            return
        step_cols = self.columns if self.mode == "grid" else 1
        moves = {
            Qt.Key_Left: -1,
            Qt.Key_Right: 1,
            Qt.Key_Up: -step_cols,
            Qt.Key_Down: step_cols,
            Qt.Key_PageUp: -step_cols * 3,
            Qt.Key_PageDown: step_cols * 3,
        }
        if key in moves:
            self.set_current_row(max(0, min(n - 1, cur + moves[key])))
            return
        if key == Qt.Key_Home:
            self.set_current_row(0)
            return
        if key == Qt.Key_End:
            self.set_current_row(n - 1)
            return
        super().keyPressEvent(e)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.set_columns(self.columns - (1 if e.angleDelta().y() > 0 else -1))
            e.accept()
            return
        # 用户主动滚动：丢弃 meta 重排的跟随锚——否则下一轮 meta 回填
        # 重排会把视口拉回用户已离开的位置（滚动被"撤销"）
        self._anchor_item = None
        super().wheelEvent(e)

    def scrollContentsBy(self, dx, dy):
        self.viewport().update()
