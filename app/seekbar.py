"""Seek bar with a hover thumbnail bubble, painted by hand.

The bubble is parented to the viewer window (not a top-level popup) so it keeps
working in fullscreen without stealing focus.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QWidget

from . import theme
from .media import format_duration
from .thumbs import FramePreviewer

TRACK_H = 4
TRACK_H_HOVER = 6
HANDLE_R = 7


class PreviewBubble(QWidget):
    """Floating frame preview + timestamp shown above the seek bar."""

    MARGIN = 8

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setVisible(False)
        self._pix: QPixmap | None = None
        self._label = "--:--"

    def set_content(self, image: QImage | None, label: str) -> None:
        self._pix = QPixmap.fromImage(image) if image is not None and not image.isNull() else None
        self._label = label
        self._resize_to_content()
        self.update()

    def _resize_to_content(self) -> None:
        fm = QFontMetrics(self._text_font())
        # 标签可能两行（时间 + "转写至 XX"）：高度按行数算足，避免内容加载后
        # 气泡尺寸突变、位置上下跳动（原先在 paintEvent 里现场加高导致下坠）
        lines = self._label.split("\n")
        text_h = fm.height() * len(lines) + 6
        text_w = max(fm.horizontalAdvance(line) for line in lines)
        if self._pix is not None:
            w = max(self._pix.width() + self.MARGIN, text_w + 20)
            h = self._pix.height() + text_h + self.MARGIN
        else:
            w = text_w + 20
            h = text_h + 4
        self.resize(int(w), int(h))

    @staticmethod
    def _text_font() -> QFont:
        f = QFont("Microsoft YaHei UI")
        f.setPointSize(9)
        f.setBold(True)
        return f

    def show_above(self, global_center_x: int, global_top_y: int) -> None:
        p = self.parentWidget()
        local = p.mapFromGlobal(QPoint(global_center_x, global_top_y))
        x = local.x() - self.width() // 2
        x = max(6, min(p.width() - self.width() - 6, x))
        y = max(6, local.y() - self.height() - 10)
        self.move(x, y)
        self.raise_()
        self.setVisible(True)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(0, 0, -1, -1), 7, 7)
        p.fillPath(path, QColor(18, 20, 24, 238))
        p.setPen(QColor(255, 255, 255, 34))
        p.drawPath(path)

        fm = QFontMetrics(self._text_font())
        if self._pix is not None:
            x = (self.width() - self._pix.width()) // 2
            y = self.MARGIN // 2
            clip = QPainterPath()
            clip.addRoundedRect(x, y, self._pix.width(), self._pix.height(), 4, 4)
            p.save()
            p.setClipPath(clip)
            p.drawPixmap(x, y, self._pix)
            p.restore()
            text_rect = QRect(0, y + self._pix.height(), self.width(), fm.height() + 4)
        else:
            # 两行标签的高度已在 _resize_to_content 算足（paintEvent 里改尺寸
            # 会从左上角向下长，把气泡压到进度条上再跳回）
            text_rect = self.rect()
        p.setFont(self._text_font())
        p.setPen(QColor(theme.TEXT))
        p.drawText(text_rect, Qt.AlignCenter, self._label)


class SeekBar(QWidget):
    seek_requested = Signal(float)
    scrub_started = Signal()
    scrub_finished = Signal()

    def __init__(self, previewer: FramePreviewer, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(20)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._duration = 0.0
        self._position = 0.0
        self._cache_end = 0.0
        self._caption_ranges: list[tuple[float, float]] = []
        self._caption_front_text = ""
        self._ab_range: tuple[float, float] | None = None
        self._hover_x: int | None = None
        self._scrubbing = False
        self._enabled = True

        self.previewer = previewer
        self.previewer.frame_ready.connect(self._on_frame_ready)
        self._bubble: PreviewBubble | None = None
        self._hover_debounce = QTimer(self)
        self._hover_debounce.setSingleShot(True)
        self._hover_debounce.setInterval(110)
        self._hover_debounce.timeout.connect(self._request_hover_frame)

    # ------------------------------------------------------------ state

    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)
        self.update()

    def set_position(self, seconds: float) -> None:
        if not self._scrubbing:
            self._position = max(0.0, seconds)
            self.update()

    def set_cache_end(self, seconds: float) -> None:
        self._cache_end = max(0.0, seconds)
        self.update()

    def set_caption_ranges(self, ranges: list[tuple[float, float]]) -> None:
        """已转写覆盖（控制器已按任务合并为 [起点, 前沿] 区间），直接显示。"""
        self._caption_ranges = [
            (max(0.0, float(start)), max(0.0, float(end)))
            for start, end in ranges
            if end > start
        ]
        self.update()

    def set_caption_front_text(self, text: str) -> None:
        """实时字幕转写前沿提示（显示在 hover 气泡的时间下方）。"""
        if text != self._caption_front_text:
            self._caption_front_text = text
            self.update()
            # 行数变化会改气泡高度，可见时立即按新尺寸重新锚定到进度条上方
            if self._hover_x is not None and self._bubble is not None \
                    and self._bubble.isVisible():
                self._show_bubble(self._hover_x)

    def set_ab_range(self, ab_range: tuple[float, float] | None) -> None:
        """A-B 循环区间可视化（琥珀色段 + 两端刻度）。None 清除。"""
        self._ab_range = ab_range
        self.update()

    def set_active(self, active: bool) -> None:
        self._enabled = active
        if not active:
            self._hide_bubble()
        self.update()

    # ------------------------------------------------------- geometry help

    def _track_rect(self, hovered: bool) -> QRect:
        h = TRACK_H_HOVER if hovered else TRACK_H
        return QRect(HANDLE_R, (self.height() - h) // 2, max(1, self.width() - HANDLE_R * 2), h)

    def _time_at_x(self, x: int) -> float:
        tr = self._track_rect(True)
        if tr.width() <= 0 or self._duration <= 0:
            return 0.0
        frac = (x - tr.left()) / tr.width()
        return max(0.0, min(1.0, frac)) * self._duration

    def _x_at_time(self, t: float, hovered: bool) -> int:
        tr = self._track_rect(hovered)
        if self._duration <= 0:
            return tr.left()
        return tr.left() + int(tr.width() * max(0.0, min(1.0, t / self._duration)))

    # -------------------------------------------------------------- paint

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hovered = self._hover_x is not None or self._scrubbing
        tr = self._track_rect(hovered)
        radius = tr.height() / 2

        base = QPainterPath()
        base.addRoundedRect(tr, radius, radius)
        p.fillPath(base, QColor(255, 255, 255, 46))

        if not self._enabled or self._duration <= 0:
            return

        # 已转写范围（实时字幕）：主轨道内的亮青色段，三段分明——
        # 灰=未转写、青=已转写未播放、蓝=已播放。原先画在轨道上方的
        # 3px 暗色细线叠在视频画面上完全看不出来（用户反馈"进度条不更新"）。
        if self._caption_ranges:
            p.setPen(Qt.NoPen)
            cap_path = QPainterPath()
            for start, end in self._caption_ranges:
                x0 = self._x_at_time(start, hovered)
                x1 = self._x_at_time(end, hovered)
                if x1 <= x0:
                    continue
                seg = QRect(tr.left(), tr.top(), min(tr.width(), x1 - tr.left()), tr.height())
                cap_path.addRoundedRect(seg, radius, radius)
            if not cap_path.isEmpty():
                p.fillPath(cap_path, QColor(72, 209, 183, 185))

        if self._cache_end > self._position:
            cr = QRect(tr.left(), tr.top(), self._x_at_time(self._cache_end, hovered) - tr.left(), tr.height())
            cp = QPainterPath()
            cp.addRoundedRect(cr, radius, radius)
            p.fillPath(cp, QColor(255, 255, 255, 74))

        px = self._x_at_time(self._position, hovered)
        pr = QRect(tr.left(), tr.top(), max(0, px - tr.left()), tr.height())
        pp = QPainterPath()
        pp.addRoundedRect(pr, radius, radius)
        p.fillPath(pp, QColor(theme.ACCENT))

        # A-B 循环区间：琥珀色高亮 + 两端细刻度（mpv 只管循环，视觉在这画）
        if self._ab_range is not None:
            a, b = self._ab_range
            # 只标了 A 点时先画起点刻度；A-B 齐了再画高亮段
            if self._duration > 0 and b >= a:
                ax = self._x_at_time(a, hovered)
                bx = self._x_at_time(b, hovered)
                if b > a:
                    ab_r = QRect(ax, tr.top(), max(2, bx - ax), tr.height())
                    ab_path = QPainterPath()
                    ab_path.addRoundedRect(ab_r, radius, radius)
                    p.fillPath(ab_path, QColor(255, 176, 32, 88))
                p.setPen(QColor(255, 176, 32, 220))
                p.drawLine(ax, tr.top(), ax, tr.bottom())
                p.drawLine(bx, tr.top(), bx, tr.bottom())
                p.setPen(Qt.NoPen)

        if hovered:
            p.setBrush(QColor(theme.ACCENT))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPoint(px, tr.center().y() + 1), HANDLE_R, HANDLE_R)

    # -------------------------------------------------------- interaction

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self._duration <= 0 or not self._enabled:
            return
        self._scrubbing = True
        self.scrub_started.emit()
        self._position = self._time_at_x(int(e.position().x()))
        self.seek_requested.emit(self._position)
        self.update()

    def mouseMoveEvent(self, e):
        x = int(e.position().x())
        self._hover_x = x
        if self._scrubbing and self._duration > 0:
            self._position = self._time_at_x(x)
            self.seek_requested.emit(self._position)
        if self._enabled and self._duration > 0:
            self._show_bubble(x)
            self._hover_debounce.start()
        self.update()

    def mouseReleaseEvent(self, e):
        if self._scrubbing:
            self._scrubbing = False
            self.scrub_finished.emit()
            self.update()

    def leaveEvent(self, e):
        self._hover_x = None
        self._hide_bubble()
        self.update()

    def hideEvent(self, e) -> None:  # noqa: N802
        super().hideEvent(e)
        # 拖动中被隐藏（如滚轮切到图片收起视频控件）收不到 mouseRelease，
        # 不复位会让 _scrubbing 永久卡死、进度条与字幕 seek 检测全部失效
        if self._scrubbing:
            self._scrubbing = False
            self.scrub_finished.emit()

    def wheelEvent(self, e):
        # let the viewer handle wheel (media switching) rather than scrubbing
        e.ignore()

    # ------------------------------------------------------------- bubble

    def _ensure_bubble(self) -> PreviewBubble:
        if self._bubble is None or self._bubble.parentWidget() is not self.window():
            self._bubble = PreviewBubble(self.window())
        return self._bubble

    def _show_bubble(self, x: int) -> None:
        t = self._time_at_x(x)
        bubble = self._ensure_bubble()
        label = format_duration(t)
        if self._caption_front_text:
            label += chr(10) + self._caption_front_text
        bubble.set_content(self.previewer.cached(t), label)
        top_left_global = self.mapToGlobal(QPoint(0, 0))
        bubble.show_above(self.mapToGlobal(QPoint(x, 0)).x(), top_left_global.y())

    def _hide_bubble(self) -> None:
        self._hover_debounce.stop()
        if self._bubble is not None:
            self._bubble.setVisible(False)

    def _request_hover_frame(self) -> None:
        if self._hover_x is None or self._duration <= 0:
            return
        self.previewer.request(self._time_at_x(self._hover_x))

    def _on_frame_ready(self, _ts: float, _img: QImage) -> None:
        if self._hover_x is not None:
            self._show_bubble(self._hover_x)
