"""Image pane: fit/zoom/pan/rotate, plus animated GIF-WebP-APNG playback.

Qt's own image plugins are tried first (fast, handles animation); Pillow is the
fallback that brings HEIC/AVIF/JXL and anything else Qt does not know.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QImageReader,
    QPainter,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import QWidget

from . import theme
from .i18n import t

MIN_SCALE = 0.04
MAX_SCALE = 32.0
ZOOM_STEP = 1.18
MAX_ANIM_FRAMES = 600

# 超过该像素数 / 帧数的图在后台线程解码（表头即可判断，不额外读像素），
# 避免百兆像素大图或超长 GIF 冻结 UI；小图仍同步解码，"秒开"手感不变
ASYNC_PIXELS = 24_000_000
ASYNC_FRAMES = 120


def _decode_image(path: Path) -> tuple[list[tuple[QImage, int]], str | None]:
    """Qt 插件优先、Pillow 兜底的完整解码。返回 (frames, 错误消息)。"""
    frames: list[tuple[QImage, int]] = []
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    if reader.canRead():
        if reader.supportsAnimation() and reader.imageCount() > 1:
            count = 0
            while count < MAX_ANIM_FRAMES:
                img = reader.read()
                if img.isNull():
                    break
                delay = reader.nextImageDelay() or 100
                frames.append((img, max(20, delay)))
                count += 1
        else:
            img = reader.read()
            if not img.isNull():
                frames.append((img, 0))

    if not frames:
        try:
            frames, _ = _load_with_pillow(path)
        except Exception as exc:
            return [], str(exc)
    return frames, None


def _load_with_pillow(path: Path) -> tuple[list[tuple[QImage, int]], bool]:
    from PIL import Image, ImageOps, ImageSequence

    from .thumbs import pil_to_qimage  # registers HEIF opener on import

    frames: list[tuple[QImage, int]] = []
    with Image.open(path) as im:
        n = getattr(im, "n_frames", 1)
        if n > 1:
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                if i >= MAX_ANIM_FRAMES:
                    break
                delay = int(frame.info.get("duration", 100) or 100)
                frames.append((pil_to_qimage(frame.convert("RGBA")), max(20, delay)))
        else:
            frames.append((pil_to_qimage(ImageOps.exif_transpose(im)), 0))
    return frames, len(frames) > 1


class ImageView(QWidget):
    zoom_changed = Signal(float)
    load_failed = Signal(str)
    loaded = Signal(int, int)  # 后台解码完成：(宽, 高)，供 viewer 回填尺寸
    _decoded = Signal(int, str, object, object)  # seq, 文件名, frames, error（跨线程排队）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)

        self._frames: list[tuple[QPixmap, int]] = []
        self._frame_index = 0
        self._rotation = 0
        self._scale = 1.0
        self._pan = QPointF(0, 0)
        self._fit_mode = True
        self._dragging = False
        self._drag_origin = QPoint()
        self._pan_origin = QPointF()
        self._source_size = (0, 0)

        self._anim = QTimer(self)
        self._anim.setSingleShot(True)
        self._anim.timeout.connect(self._next_frame)
        self._decoded.connect(self._on_decoded)  # worker 线程解码结果回 UI
        self._load_seq = 0

    # -------------------------------------------------------------- loading

    def clear(self) -> None:
        self._anim.stop()
        self._frames = []
        self._frame_index = 0
        self._rotation = 0
        self._load_seq += 1  # 作废在途的后台解码
        self.update()

    def load(self, path: Path) -> bool:
        self._anim.stop()
        self._frames = []
        self._frame_index = 0
        self._rotation = 0
        self._load_seq += 1
        seq = self._load_seq

        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        if reader.canRead():
            size = reader.size()
            pixels = max(0, size.width()) * max(0, size.height())
            n_frames = reader.imageCount() if reader.supportsAnimation() else 1
            if pixels > ASYNC_PIXELS or n_frames > ASYNC_FRAMES:
                # 大图后台解码：UI 不冻结，完成后经 loaded 信号回填
                threading.Thread(target=self._decode_background,
                                 args=(path, seq), daemon=True).start()
                self.update()
                return True

        frames, error = _decode_image(path)
        return self._apply_result(seq, path, frames, error, background=False)

    def _decode_background(self, path: Path, seq: int) -> None:
        frames, error = _decode_image(path)
        self._decoded.emit(seq, path.name, frames, error)  # 排队连接，回 GUI 线程

    def _on_decoded(self, seq: int, name: str, frames, error) -> None:
        if seq != self._load_seq:
            return  # 用户已翻页，丢弃过期结果
        self._apply_result(seq, name, frames, error, background=True)

    def _apply_result(self, seq: int, name: str, frames, error,
                      background: bool) -> bool:
        if error is not None:
            self.load_failed.emit(
                t("image_view.load_failed").format(name=name, error=error))
            self.update()
            return False
        if not frames:
            self.load_failed.emit(
                t("image_view.decode_failed").format(name=name))
            self.update()
            return False

        self._frames = [(QPixmap.fromImage(img), delay) for img, delay in frames]
        self._source_size = (self._frames[0][0].width(), self._frames[0][0].height())
        self.fit_to_window()
        if len(self._frames) > 1:
            self._anim.start(self._frames[0][1])
        if background:
            self.loaded.emit(self._source_size[0], self._source_size[1])
        return True

    def _next_frame(self) -> None:
        if len(self._frames) < 2:
            return
        self._frame_index = (self._frame_index + 1) % len(self._frames)
        self.update()
        self._anim.start(self._frames[self._frame_index][1])

    # ---------------------------------------------------------------- state

    @property
    def has_image(self) -> bool:
        return bool(self._frames)

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def source_size(self) -> tuple[int, int]:
        return self._source_size

    def _current_pixmap(self) -> QPixmap | None:
        if not self._frames:
            return None
        pix = self._frames[self._frame_index][0]
        if self._rotation:
            pix = pix.transformed(QTransform().rotate(self._rotation), Qt.SmoothTransformation)
        return pix

    def _rotated_size(self) -> tuple[int, int]:
        w, h = self._source_size
        return (h, w) if self._rotation % 180 else (w, h)

    # ----------------------------------------------------------- transforms

    def fit_to_window(self) -> None:
        w, h = self._rotated_size()
        if not w or not h:
            return
        vw, vh = max(1, self.width()), max(1, self.height())
        scale = min(vw / w, vh / h)
        self._scale = min(scale, 1.0)  # never upscale just to fill the window
        self._fit_mode = True
        self._center()
        self.zoom_changed.emit(self._scale)
        self.update()

    def actual_size(self) -> None:
        self._set_scale(1.0, QPointF(self.width() / 2, self.height() / 2))
        self._fit_mode = False

    def toggle_fit_actual(self) -> None:
        if self._fit_mode and self._scale < 1.0:
            self.actual_size()
        else:
            self.fit_to_window()

    def zoom_by_steps(self, steps: int, anchor: QPointF | None = None) -> None:
        if not self._frames or steps == 0:
            return
        anchor = anchor or QPointF(self.width() / 2, self.height() / 2)
        self._set_scale(self._scale * (ZOOM_STEP ** steps), anchor)
        self._fit_mode = False

    def _set_scale(self, new_scale: float, anchor: QPointF) -> None:
        new_scale = max(MIN_SCALE, min(MAX_SCALE, new_scale))
        if abs(new_scale - self._scale) < 1e-6:
            return
        ratio = new_scale / self._scale
        self._pan = anchor - (anchor - self._pan) * ratio
        self._scale = new_scale
        self._clamp_pan()
        self.zoom_changed.emit(self._scale)
        self.update()

    def rotate(self, degrees: int) -> None:
        if not self._frames:
            return
        self._rotation = (self._rotation + degrees) % 360
        self.fit_to_window()

    def reset(self) -> None:
        self._rotation = 0
        self.fit_to_window()

    def _center(self) -> None:
        w, h = self._rotated_size()
        self._pan = QPointF(
            (self.width() - w * self._scale) / 2, (self.height() - h * self._scale) / 2
        )

    def _clamp_pan(self) -> None:
        w, h = self._rotated_size()
        dw, dh = w * self._scale, h * self._scale
        x, y = self._pan.x(), self._pan.y()
        if dw <= self.width():
            x = (self.width() - dw) / 2
        else:
            x = min(0.0, max(self.width() - dw, x))
        if dh <= self.height():
            y = (self.height() - dh) / 2
        else:
            y = min(0.0, max(self.height() - dh, y))
        self._pan = QPointF(x, y)

    @property
    def can_pan(self) -> bool:
        w, h = self._rotated_size()
        return w * self._scale > self.width() + 1 or h * self._scale > self.height() + 1

    # ------------------------------------------------------------ rendering

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._fit_mode:
            self.fit_to_window()
        else:
            self._clamp_pan()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BG_DEEP))
        pix = self._current_pixmap()
        if pix is None:
            return
        target = QRectF(
            self._pan.x(), self._pan.y(), pix.width() * self._scale, pix.height() * self._scale
        )
        p.setRenderHint(QPainter.SmoothPixmapTransform, self._scale < 4.0)
        p.drawPixmap(target, pix, QRectF(pix.rect()))

    # ---------------------------------------------------------- interaction

    def mousePressEvent(self, e):
        if e.button() in (Qt.LeftButton, Qt.MiddleButton) and self.can_pan:
            self._dragging = True
            self._drag_origin = e.position().toPoint()
            self._pan_origin = QPointF(self._pan)
            self.setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        e.ignore()

    def mouseMoveEvent(self, e):
        if self._dragging:
            delta = e.position().toPoint() - self._drag_origin
            self._pan = self._pan_origin + QPointF(delta.x(), delta.y())
            self._clamp_pan()
            self.update()
            e.accept()
            return
        self.setCursor(Qt.OpenHandCursor if self.can_pan else Qt.ArrowCursor)
        e.ignore()

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._dragging = False
            self.setCursor(Qt.OpenHandCursor if self.can_pan else Qt.ArrowCursor)
            e.accept()
            return
        e.ignore()
