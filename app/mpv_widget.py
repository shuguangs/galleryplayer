"""libmpv video surface rendered into a QOpenGLWidget.

Using mpv's render API (rather than handing mpv a native window id) is what lets
ordinary Qt widgets — the control bar, the top bar — sit on top of the video,
because Qt 6 composites QOpenGLWidget into the window's backing store.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtOpenGLWidgets import QOpenGLWidget

import mpv

from . import netpath
from .config import settings
from .i18n import t


class Track:
    __slots__ = ("id", "kind", "title", "lang", "codec", "selected", "external")

    def __init__(self, raw: dict) -> None:
        self.id = raw.get("id")
        self.kind = raw.get("type")
        self.title = raw.get("title") or ""
        self.lang = raw.get("lang") or ""
        self.codec = raw.get("codec") or ""
        self.selected = bool(raw.get("selected"))
        self.external = bool(raw.get("external"))

    def label(self) -> str:
        bits = [b for b in (self.title, self.lang.upper() if self.lang else "") if b]
        name = " · ".join(bits) if bits else t("mpv.track_label").format(id=self.id)
        if self.external:
            name += t("mpv.external")
        return name


class MpvWidget(QOpenGLWidget):
    position_changed = Signal(float)
    duration_changed = Signal(float)
    pause_changed = Signal(bool)
    speed_changed = Signal(float)
    volume_changed = Signal(float)
    mute_changed = Signal(bool)
    cache_changed = Signal(float)
    tracks_changed = Signal(list)
    file_loaded = Signal()
    eof_reached = Signal()
    error = Signal(str)
    # loadfile 下发那一刻同步发出（不等 mpv 解复用）：缩略图/浏览器后台活
    # 计要在起播前就让路。靠 duration>0 判"在播"要等 mpv 回报，实测慢
    # 0.8-6.5s，整个卡顿窗口刚好没人管（见 main_window._update_thumb_gates）。
    playback_starting = Signal()

    _redraw = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._ctx: mpv.MpvRenderContext | None = None
        self._alive = True
        self._observers: list[tuple[str, object]] = []
        self._event_callbacks: list[object] = []
        self._pending_seek: float | None = None
        # 有片子在手（loadfile 起播即真，stop() 归假）。判"在播"用它 +
        # paused，而不是 duration>0——duration 要等解复用完成才有值。
        self.media_loaded = False
        self.mpv = mpv.MPV(
            vo="libmpv",
            hwdec=str(settings["hwdec"]),
            keep_open="always",
            idle="yes",
            terminal="no",
            osc="no",
            ytdl="no",
            input_default_bindings="no",
            input_vo_keyboard="no",
            load_scripts="no",
            hr_seek="yes",
            volume=float(settings["volume"]),
            mute="yes" if settings["muted"] else "no",
            **{
                "sub-auto": "fuzzy",
                "sub-font-size": str(int(settings["sub_font_size"])),
                "sub-border-size": "2.4",
                "sub-shadow-offset": "0.6",
                "sub-color": str(settings["sub_color"] or "#ffffff"),
                "sub-visibility": "yes" if settings["sub_visible"] else "no",
                "demuxer-max-bytes": "32MiB",  # plenty for local files
                "cache": "yes",
                "audio-file-auto": "fuzzy",
                "screenshot-format": "png",
                "alang": "chi,zho,jpn,eng",
                "slang": "chi,zho,eng",
            },
        )
        self._redraw.connect(self.update, Qt.QueuedConnection)
        self._wire_observers()

    # -------------------------------------------------------------- wiring

    def _observe(self, name: str, fn) -> None:
        """Register a property observer that is inert once the widget is shut down.

        mpv delivers property changes on its own event thread. Without this guard a
        change that lands after teardown emits into an already-deleted QObject, which
        crashes the process with an access violation on exit.
        """

        def handler(_name, value, fn=fn):
            if not self._alive:
                return
            try:
                fn(value)
            except RuntimeError:
                pass  # underlying C++ object is gone

        self._observers.append((name, handler))
        self.mpv.observe_property(name, handler)

    def _wire_observers(self) -> None:
        m = self.mpv
        observe = self._observe

        observe("time-pos", lambda v: self.position_changed.emit(float(v)) if v is not None else None)
        observe("duration", lambda v: self.duration_changed.emit(float(v)) if v else None)
        observe("pause", lambda v: self.pause_changed.emit(bool(v)))
        observe("speed", lambda v: self.speed_changed.emit(float(v)) if v else None)
        observe("volume", lambda v: self.volume_changed.emit(float(v)) if v is not None else None)
        observe("mute", lambda v: self.mute_changed.emit(bool(v)))
        observe("demuxer-cache-time", lambda v: self.cache_changed.emit(float(v)) if v else None)
        observe("track-list", self._on_track_list)
        observe("eof-reached", lambda v: self.eof_reached.emit() if v else None)

        @m.event_callback("file-loaded")
        def _loaded(_evt):  # noqa: ANN001
            if not self._alive:
                return
            # Resume by a *keyframe* seek here rather than an exact --start seek on
            # load: an exact seek decodes from the previous keyframe forward to the
            # target, which stalls 1-2s on long clips with sparse keyframes and is
            # felt as a freeze when switching videos. A keyframe seek is ~20x faster
            # and lands a little before the saved spot -- fine for "continue".
            seek_to = self._pending_seek
            self._pending_seek = None
            if seek_to:
                try:
                    self.mpv.command("seek", seek_to, "absolute+keyframes")
                except Exception:
                    pass
            try:
                self.file_loaded.emit()
            except RuntimeError:
                pass

        @m.event_callback("end-file")
        def _end(evt):  # noqa: ANN001
            if not self._alive:
                return
            data = getattr(evt, "data", None) or {}
            reason = data.get("reason") if isinstance(data, dict) else None
            if reason == "error":
                try:
                    self.error.emit(str(data.get("file_error") or t("mpv.play_failed")))
                except RuntimeError:
                    pass

        self._event_callbacks = [_loaded, _end]

    def _on_track_list(self, raw) -> None:
        try:
            self.tracks_changed.emit([Track(t) for t in (raw or [])])
        except Exception:
            self.tracks_changed.emit([])

    # ------------------------------------------------------------- OpenGL

    def initializeGL(self) -> None:
        glctx = self.context()

        def get_proc(_ctx, name):
            if isinstance(name, bytes):
                name = name.decode()
            addr = glctx.getProcAddress(name)
            return int(addr) if addr else 0

        self._ctx = mpv.MpvRenderContext(
            self.mpv,
            "opengl",
            opengl_init_params={"get_proc_address": mpv.MpvGlGetProcAddressFn(get_proc)},
        )
        self._ctx.update_cb = self._request_redraw

    def _request_redraw(self) -> None:
        """Called from mpv's render thread whenever a new frame is ready."""
        if not self._alive:
            return
        try:
            self._redraw.emit()
        except RuntimeError:
            pass  # widget already destroyed

    def paintGL(self) -> None:
        if not self._ctx or not self._alive:
            return
        r = self.devicePixelRatioF()
        try:
            self._ctx.render(
                flip_y=True,
                opengl_fbo={
                    "w": max(1, int(self.width() * r)),
                    "h": max(1, int(self.height() * r)),
                    "fbo": self.defaultFramebufferObject(),
                },
            )
        except Exception:
            pass

    # ------------------------------------------------------- playback API

    # Local files can be read on demand; a share needs a real read-ahead buffer or
    # every seek and every bitrate spike turns into a stall.
    LOCAL_CACHE = {"demuxer-max-bytes": "32MiB", "cache-secs": "10", "demuxer-readahead-secs": "5"}
    REMOTE_CACHE = {"demuxer-max-bytes": "192MiB", "cache-secs": "60", "demuxer-readahead-secs": "30"}

    def load(self, path: str | Path, start_at: float | None = None) -> None:
        for key, value in (self.REMOTE_CACHE if netpath.is_remote(path) else self.LOCAL_CACHE).items():
            try:
                self.mpv[key] = value
            except Exception:
                pass
        self._pending_seek = float(start_at) if (start_at and start_at > 1) else None
        self.media_loaded = True
        # 先让路、再下发：信号是同步的，接收方（后台加载让路）在 loadfile
        # 之前就已生效，第一帧不用跟三十万行的填充抢 GUI 线程。
        self.playback_starting.emit()
        self.mpv.loadfile(str(path), "replace")
        self.mpv.pause = False

    def stop(self) -> None:
        self.media_loaded = False
        try:
            self.mpv.command("stop")
        except Exception:
            pass

    def toggle_pause(self) -> None:
        self.mpv.pause = not bool(self.mpv.pause)

    def set_pause(self, value: bool) -> None:
        self.mpv.pause = bool(value)

    @property
    def paused(self) -> bool:
        return bool(self.mpv.pause)

    @property
    def position(self) -> float:
        return float(self.mpv.time_pos or 0.0)

    @property
    def duration(self) -> float:
        return float(self.mpv.duration or 0.0)

    def seek_absolute(self, seconds: float) -> None:
        try:
            self.mpv.command("seek", max(0.0, seconds), "absolute+exact")
        except Exception:
            pass

    def seek_relative(self, delta: float) -> None:
        try:
            self.mpv.command("seek", delta, "relative")
        except Exception:
            pass

    def set_speed(self, value: float) -> None:
        self.mpv.speed = max(0.1, min(8.0, value))
        settings["speed"] = float(self.mpv.speed)

    # ------------------------------------------------------- aspect ratio

    # 菜单规范键 → mpv video-aspect-override 的浮点读回值（mpv 会把
    # "no"/"4:3" 之类的字符串规范化成浮点：no=-2、-1=拉伸、其余=宽高比）
    _ASPECT_FLOATS = {
        "no": -2.0,
        "-1": -1.0,
        "4:3": 4 / 3,
        "16:9": 16 / 9,
        "16:10": 16 / 10,
        "21:9": 21 / 9,
        "1:1": 1.0,
        "2.35": 2.35,
    }

    def set_aspect_mode(self, mode: str) -> None:
        """画面比例：no=跟随源 / -1=拉伸铺满 / "w:h"=指定比例 / cropfill=裁切铺满。

        属性跨换片持续生效（mpv 实例级），菜单的勾选态由
        current_aspect_mode 从 mpv 读回，不另存状态。
        """
        try:
            if mode == "cropfill":
                self.mpv["video-aspect-override"] = "no"
                self.mpv["panscan"] = 1.0
            else:
                self.mpv["panscan"] = 0.0
                self.mpv["video-aspect-override"] = mode
        except Exception:
            pass  # 属性设置失败不拖垮播放

    def current_aspect_mode(self) -> str:
        """读回当前模式（规范键）。mpv 以浮点回报比例，按容差映射回键。"""
        try:
            aspect = float(self.mpv["video-aspect-override"])
            panscan = float(self.mpv["panscan"] or 0.0)
        except Exception:
            return "no"
        if panscan >= 0.99:
            return "cropfill"
        for key, value in self._ASPECT_FLOATS.items():
            if abs(aspect - value) < 0.01:
                return key
        return ""   # 非预设值（外部改过）：菜单里不勾任何项

    @property
    def speed(self) -> float:
        return float(self.mpv.speed or 1.0)

    def set_volume(self, value: float) -> None:
        v = max(0.0, min(150.0, value))
        self.mpv.volume = v
        settings["volume"] = int(v)

    @property
    def volume(self) -> float:
        return float(self.mpv.volume or 0.0)

    def toggle_mute(self) -> None:
        self.mpv.mute = not bool(self.mpv.mute)
        settings["muted"] = bool(self.mpv.mute)

    @property
    def muted(self) -> bool:
        return bool(self.mpv.mute)

    def set_loop(self, on: bool) -> None:
        self.mpv.loop_file = "inf" if on else "no"

    # ------------------------------------------------------ subs / audio

    def set_track(self, kind: str, track_id) -> None:
        prop = {"sub": "sid", "audio": "aid", "video": "vid"}[kind]
        try:
            setattr(self.mpv, prop, track_id)
        except Exception:
            pass

    def current_track(self, kind: str):
        prop = {"sub": "sid", "audio": "aid", "video": "vid"}[kind]
        try:
            return getattr(self.mpv, prop)
        except Exception:
            return None

    def cycle_track(self, kind: str) -> None:
        """Advance to the next track of `kind`, wrapping.

        mpv's own `cycle audio` also steps through the no-track state, so pressing A
        on a two-language file lands on silence every third press. Cycling only over
        real tracks is what the key is for; V / the menu turn things off.
        """
        try:
            ids = [
                t.get("id")
                for t in (self.mpv.track_list or [])
                if t.get("type") == kind and t.get("id") is not None
            ]
        except Exception:
            ids = []
        if not ids:
            return
        current = self.current_track(kind)
        try:
            nxt = ids[(ids.index(current) + 1) % len(ids)]
        except ValueError:
            nxt = ids[0]
        self.set_track(kind, nxt)

    def add_subtitle_file(self, path: str) -> None:
        try:
            self.mpv.command("sub-add", path, "select")
        except Exception:
            pass

    def set_sub_visible(self, visible: bool) -> None:
        self.mpv.sub_visibility = bool(visible)
        settings["sub_visible"] = bool(visible)

    def set_file_subtitle_visible(self, visible: bool) -> None:
        """Change mpv subtitle visibility without changing the user preference."""
        self.mpv.sub_visibility = bool(visible)

    @property
    def sub_visible(self) -> bool:
        return bool(self.mpv.sub_visibility)

    def set_sub_font_size(self, size: int) -> None:
        size = max(16, min(96, size))
        self.mpv.sub_font_size = size
        settings["sub_font_size"] = size

    def apply_sub_color_style(self) -> None:
        """字幕颜色 + 反色描边（sub-color/sub-border-color），实时生效。

        白色字幕在白色画面上不可见（用户实测）——允许改颜色；描边档位
        0=关，>0=反色描边（白字黑边/黑字白边）且粗细随档位放大。
        """
        color = str(settings["sub_color"] or "#ffffff")
        outline = int(settings["sub_outline"] or 0)
        try:
            self.mpv.sub_color = color
            if outline > 0:
                # 反色（mpv 的 sub-border-color 同为 #RRGGBB）
                r = int(color[1:3], 16)
                g = int(color[3:5], 16)
                b = int(color[5:7], 16)
                inverse = f"#{255 - r:02x}{255 - g:02x}{255 - b:02x}"
                self.mpv.sub_border_color = inverse
                self.mpv.sub_border_size = 1.2 + outline * 0.9
            else:
                self.mpv.sub_border_size = 2.4  # 默认黑边
                self.mpv.sub_border_color = "#000000"
        except Exception:
            pass

    @property
    def sub_font_size(self) -> int:
        try:
            return int(self.mpv.sub_font_size)
        except Exception:
            return int(settings["sub_font_size"])

    def adjust_sub_delay(self, delta: float) -> float:
        try:
            self.mpv.sub_delay = float(self.mpv.sub_delay or 0.0) + delta
            return float(self.mpv.sub_delay)
        except Exception:
            return 0.0

    @property
    def sub_delay(self) -> float:
        try:
            return float(self.mpv.sub_delay or 0.0)
        except Exception:
            return 0.0

    @property
    def media_title(self) -> str:
        try:
            return str(self.mpv.media_title or "")
        except Exception:
            return ""

    @property
    def hwdec_active(self) -> str:
        try:
            return str(self.mpv.hwdec_current or "no")
        except Exception:
            return "no"

    def set_hwdec(self, mode: str) -> None:
        """Switch hardware/software decoding at runtime.

        mpv accepts 'auto-safe' / 'auto' / 'auto-copy' / 'no' (and a few more).
        'no' forces CPU decoding; the others let the GPU handle it when possible.
        """
        try:
            self.mpv.hwdec = str(mode)
        except Exception:
            pass

    @property
    def video_native_size(self) -> tuple[int, int]:
        """Return the video's native (width, height) in pixels, or (0, 0) if unknown.

        'dwidth'/'dheight' are the destination dimensions after any rotation or
        anamorphic stretch, which is what the window should match; 'video-params'
        gives the raw codec dimensions, which can differ for anamorphic sources.
        """
        try:
            w = int(self.mpv.dwidth or 0)
            h = int(self.mpv.dheight or 0)
            if w and h:
                return w, h
        except Exception:
            pass
        try:
            params = self.mpv.video_params or {}
            return int(params.get("w", 0) or 0), int(params.get("h", 0) or 0)
        except Exception:
            return 0, 0

    @property
    def video_codec(self) -> str:
        try:
            return str(self.mpv.video_codec or "")
        except Exception:
            return ""

    def screenshot(self, path: str) -> bool:
        try:
            self.mpv.command("screenshot-to-file", path, "video")
            return True
        except Exception:
            return False

    def grab_frame(self):
        """Current video frame as a PIL image (or None). Used for GIF capture."""
        try:
            return self.mpv.screenshot_raw(includes="video")
        except Exception:
            return None

    # ------------------------------------------------------ A-B loop / step

    def set_ab_loop(self, which: str, pos: float | None) -> None:
        """Set the A or B point of mpv's native A-B loop (None clears that point)."""
        prop = "ab-loop-a" if which == "a" else "ab-loop-b"
        try:
            self.mpv[prop] = "no" if pos is None else float(pos)
        except Exception:
            pass

    def clear_ab_loop(self) -> None:
        for prop in ("ab-loop-a", "ab-loop-b"):
            try:
                self.mpv[prop] = "no"
            except Exception:
                pass

    def frame_step(self, back: bool = False) -> None:
        """Advance (or rewind) exactly one frame; mpv pauses as a side effect."""
        try:
            self.mpv.command("frame-back-step" if back else "frame-step")
        except Exception:
            pass

    # ------------------------------------------------------ video equalizer

    # mpv exposes these as integer properties in the -100..100 range; 0 is neutral.
    EQ_PROPS = ("brightness", "contrast", "saturation", "gamma", "hue")

    def set_video_eq(self, name: str, value: int) -> None:
        if name not in self.EQ_PROPS:
            return
        try:
            setattr(self.mpv, name, max(-100, min(100, int(value))))
        except Exception:
            pass

    def get_video_eq(self, name: str) -> int:
        try:
            return int(getattr(self.mpv, name) or 0)
        except Exception:
            return 0

    def reset_video_eq(self) -> None:
        for name in self.EQ_PROPS:
            self.set_video_eq(name, 0)

    # ------------------------------------------------------------ teardown

    def shutdown(self) -> None:
        if not self._alive:
            return
        self._alive = False  # from here on, mpv-thread callbacks are no-ops

        for name, handler in self._observers:
            try:
                self.mpv.unobserve_property(name, handler)
            except Exception:
                pass
        self._observers.clear()
        for cb in self._event_callbacks:
            try:
                cb.unregister_mpv_event_callback()
            except Exception:
                pass
        self._event_callbacks.clear()
        try:
            self.mpv.command("stop")
        except Exception:
            pass

        ctx, self._ctx = self._ctx, None
        if ctx is not None:
            # NOT None: python-mpv wraps the value in `lambda: func()` and re-registers
            # it, so assigning None installs a callback that raises on the next frame.
            ctx.update_cb = lambda: None
            # mpv_render_context_free() must run with the GL context current on this
            # thread; freeing it without makeCurrent() faults inside the driver.
            made_current = False
            try:
                self.makeCurrent()
                made_current = True
            except Exception:
                pass
            try:
                ctx.free()
            except Exception:
                pass
            if made_current:
                try:
                    self.doneCurrent()
                except Exception:
                    pass
        try:
            self.mpv.terminate()
        except Exception:
            pass
