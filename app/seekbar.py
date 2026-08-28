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
        text_h = fm.height() + 6
        if self._pix is not None:
            w = max(self._pix.width() + self.MARGIN, fm.horizontalAdvance(self._label) + 20)
            h = self._pix.height() + text_h + self.MARGIN
        else:
            w = fm.horizontalAdvance(self._label) + 20
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
        """Caption coverage is shown as a subtle second track while hovered."""
        self._caption_ranges = [
            (max(0.0, float(start)), max(0.0, float(end)))
            for start, end in ranges
            if end > start
        ]
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

        if hovered and self._caption_ranges:
            caption_track = QRect(
                tr.left(), max(0, tr.top() - 8), tr.width(), 2
            )
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 42))
            p.drawRoundedRect(caption_track, 1, 1)
            p.setBrush(QColor(theme.ACCENT_DIM))
            for start, end in self._caption_ranges:
                x0 = self._x_at_time(start, hovered)
                x1 = self._x_at_time(end, hovered)
                if x1 <= x0:
                    continue
                p.drawRoundedRect(
                    QRect(caption_track.left(), caption_track.top(), max(1, x1 - x0), 2), 1, 1
                )

        if not self._enabled or self._duration <= 0:
            return

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
        bubble.set_content(self.previewer.cached(t), format_duration(t))
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
