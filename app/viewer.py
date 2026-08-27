"""Immersive viewer: one surface for images, one for video, floating overlay bars.

Wheel switches media (Telegram-style); Ctrl+wheel zooms an image or nudges the
volume on a video. Bars fade out while the mouse is still.
"""
from __future__ import annotations

import os
import json
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QProcess, QProcessEnvironment, QTimer, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QLabel, QMenu, QStackedLayout, QWidget

from . import fileops, icons, theme
from .config import resume, settings
from .controls import SPEEDS, ControlBar, StageButton, TopBar
from .i18n import t
from .image_view import ImageView
from .media import MediaItem, format_duration, item_for_path
from .mpv_widget import MpvWidget
from .playlist_panel import PlaylistPanel
from .runtime import APP_DIR
from .thumbs import FramePreviewer, ThumbnailCache
from .welcome import remember_recent_file

HIDE_DELAY_MS = 2600
# Accumulated mouse travel (px) required to bring the hidden overlay back:
# filters out the tiny jitter of a hand resting on the mouse or desk vibration.
SHOW_MOVE_THRESHOLD_PX = 28
SEEK_SMALL = 5.0
SEEK_TINY = 1.0
SEEK_BIG = 60.0
# GIF capture: sample the live frame at ~10fps, cap one clip at ~15s, and shrink
# frames so the file stays a sane size.
GIF_FRAME_MS = 100
GIF_MAX_FRAMES = 150
GIF_MAX_WIDTH = 480


class Viewer(QWidget):
    index_changed = Signal(int)
    folder_requested = Signal(object)     # Path, from the panel's browser tab
    playlist_changed = Signal(list)       # reordered / trimmed MediaItem list
    sort_requested = Signal(str, bool)    # (sort_key, desc) from the panel
    _capture_saved = Signal(str)          # toast text, emitted from a worker thread

    def __init__(self, thumbs: ThumbnailCache, fs_model_provider=None) -> None:
        super().__init__(None)
        self.setWindowTitle(t("viewer.window_title"))
        self.setObjectName("ViewerRoot")
        self.setStyleSheet(f"QWidget#ViewerRoot {{ background: {theme.BG_DEEP}; }}")
        self.resize(1280, 800)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

        self.thumbs = thumbs
        self.previewer = FramePreviewer(self)
        self.items: list[MediaItem] = []
        self.index = -1
        self._scrubbing = False
        self._at_eof = False                  # 播放完停在最后一帧（等待再次播放从头开始）
        self._bars_visible = True
        self._move_accum = 0.0                    # travel since the bars hid
        self._last_move_pos = None                # None after leaving the window
        self._wheel_accum = 0
        self._pending_click: QTimer | None = None
        self._shutdown = False
        # ---- GIF recording state
        self._gif_recording = False
        self._gif_frames: list = []
        self._gif_interval = GIF_FRAME_MS
        self._gif_max = GIF_MAX_FRAMES
        self._gif_w = GIF_MAX_WIDTH
        self._gif_timer = QTimer(self)
        self._gif_timer.setInterval(GIF_FRAME_MS)
        self._gif_timer.timeout.connect(self._gif_tick)
        self._capture_saved.connect(self._show_toast)
        # ---- A-B loop state (mpv loops between these two marks when both are set)
        self._ab_a: float | None = None
        self._ab_b: float | None = None

        # ---- stage
        self.stage = QWidget(self)
        self.stage.setObjectName("ViewerStage")
        self.stage.setMouseTracking(True)
        self.stack = QStackedLayout(self.stage)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setStackingMode(QStackedLayout.StackOne)

        self.image_view = ImageView()
        self.image_view.setMouseTracking(True)
        self.image_view.zoom_changed.connect(self._on_zoom_changed)
        self.image_view.load_failed.connect(self._on_load_failed)
        self.video_view = MpvWidget()
        self.video_view.setMouseTracking(True)
        self.stack.addWidget(self.image_view)
        self.stack.addWidget(self.video_view)

        # 实时听译字幕 overlay（环路录音 → whisper → Ollama 翻译）
        self._live_proc: QProcess | None = None
        self._live_on = False
        self._live_paused = False
        self._live_rows: list[tuple[float, float, str, str]] = []
        self._live_label = QLabel("", self.stage)
        self._live_label.setObjectName("LiveCaption")
        self._live_label.setAlignment(Qt.AlignCenter)
        self._live_label.setWordWrap(True)
        self._live_label.hide()
        self._apply_live_caption_style(relayout=False)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("OverlayError")
        self.error_label.setAlignment(Qt.AlignCenter)
        self.error_label.setVisible(False)

        self.toast = QLabel("", self)
        self.toast.setObjectName("OverlayDim")
        self.toast.setAlignment(Qt.AlignCenter)
        self.toast.setStyleSheet(
            "background: rgba(15,17,20,225); color:#e8eaed; border-radius:7px; padding:7px 13px;"
        )
        self.toast.setVisible(False)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self.toast.setVisible(False))

        # ---- docked side panel (playlist / albums / browser)
        self.panel = PlaylistPanel(thumbs, fs_model_provider, self)
        self.panel.setVisible(bool(settings["panel_visible"]))
        self.panel.resize(int(settings["panel_width"]), self.height())
        self._wire_panel()

        # ---- overlay bars
        self.top_bar = TopBar(self)
        self.top_bar.close_clicked.connect(self.close)
        self.controls = ControlBar(self.previewer, self)
        self._wire_controls()

        # ---- on-stage buttons (play/pause in the middle, prev/next at the edges)
        # The middle one is plain (no translucent circle) and stays hidden while
        # playing; it only appears as a play glyph when the video is paused.
        self.btn_stage_play = StageButton(icons.PLAY, t("viewer.play_pause"), self, plain=True)
        self.btn_stage_play.clicked.connect(self._toggle_pause)
        self.btn_stage_prev = StageButton(icons.CHEVRON_LEFT, t("viewer.previous"), self)
        self.btn_stage_prev.clicked.connect(lambda: self.step(-1))
        self.btn_stage_next = StageButton(icons.CHEVRON_RIGHT, t("viewer.next"), self)
        self.btn_stage_next.clicked.connect(lambda: self.step(1))
        self._stage_buttons = (self.btn_stage_play, self.btn_stage_prev, self.btn_stage_next)
        for b in self._stage_buttons:
            b.setVisible(False)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._hide_bars)

        self._wire_player()
        self.controls.set_volume(int(settings["volume"]), bool(settings["muted"]))
        self.controls.set_speed(float(settings["speed"]))
        self.controls.set_hwdec_mode(str(settings["hwdec"]))
        self.controls.set_loop_mode(str(settings["loop_mode"]))
        self.controls.set_panel_open(self.panel.isVisible())
        self.video_view.set_loop(str(settings["loop_mode"]) == "one")
        # Track whether we've already fitted the window to the first video's native
        # size in this session: subsequent videos keep the user's chosen window size.
        self._native_size_applied = False

    # ------------------------------------------------------------- wiring

    def _wire_controls(self) -> None:
        c = self.controls
        c.play_pause.connect(self._toggle_pause)
        c.prev_media.connect(lambda: self.step(-1))
        c.next_media.connect(lambda: self.step(1))
        c.seek_requested.connect(self._seek_absolute)
        c.scrub_started.connect(lambda: setattr(self, "_scrubbing", True))
        c.scrub_finished.connect(lambda: setattr(self, "_scrubbing", False))
        c.speed_selected.connect(self._set_speed)
        c.hwdec_selected.connect(self._set_hwdec)
        c.screenshot_requested.connect(self._take_screenshot)
        c.gif_toggle_requested.connect(self._toggle_gif)
        c.video_eq_changed.connect(self.video_view.set_video_eq)
        c.video_eq_reset.connect(self._reset_video_eq)
        c.volume_selected.connect(lambda v: self.video_view.set_volume(float(v)))
        c.mute_toggled.connect(self._toggle_mute)
        c.loop_cycle_requested.connect(self.panel._cycle_loop)
        c.panel_toggled.connect(self.toggle_panel)
        c.fullscreen_toggled.connect(self.toggle_fullscreen)
        c.sub_track_selected.connect(lambda tid: self.video_view.set_track("sub", tid))
        c.audio_track_selected.connect(lambda tid: self.video_view.set_track("audio", tid))
        c.sub_file_requested.connect(self.video_view.add_subtitle_file)
        c.sub_font_step.connect(self._step_sub_font)
        c.sub_delay_step.connect(self._step_sub_delay)
        c.sub_visibility_toggled.connect(self._toggle_sub_visible)
        c.zoom_step.connect(lambda s: self.image_view.zoom_by_steps(s))
        c.zoom_fit.connect(self.image_view.toggle_fit_actual)
        c.zoom_actual.connect(self.image_view.actual_size)
        c.rotate_requested.connect(self.image_view.rotate)

    def _wire_panel(self) -> None:
        p = self.panel
        p.play_index.connect(self.show_index)
        p.playlist_reordered.connect(self._on_playlist_reordered)
        p.playlist_removed.connect(self._on_playlist_removed)
        p.album_play_requested.connect(self._on_album_play)
        p.folder_requested.connect(self.folder_requested)
        p.loop_mode_changed.connect(self._on_loop_mode)
        p.autoplay_changed.connect(lambda on: settings.__setitem__("autoplay_next", on))
        p.sort_requested.connect(self.sort_requested)
        p.playlist_imported.connect(self._on_playlist_imported)
        p.closed.connect(lambda: self.set_panel_visible(False))
        p.width_changed.connect(self._on_panel_width)

    def _on_panel_width(self, width: int) -> None:
        self.panel.resize(width, self.height())
        self._relayout()

    def set_panel_visible(self, visible: bool) -> None:
        settings["panel_visible"] = visible
        self.panel.setVisible(visible)
        self.controls.set_panel_open(visible)
        self._relayout()
        if visible:
            self.panel.set_playlist(self.items, self.index)
        self._show_bars()

    def toggle_panel(self) -> None:
        self.set_panel_visible(not self.panel.isVisible())

    def _on_playlist_reordered(self, items: list) -> None:
        current = self.items[self.index].path if 0 <= self.index < len(self.items) else None
        self.items = list(items)
        if current is not None:
            for i, it in enumerate(self.items):
                if it.path == current:
                    self.index = i
                    break
        self.panel.set_current(self.index)
        self._update_top_bar(self.items[self.index]) if 0 <= self.index < len(self.items) else None
        self.controls.set_navigation(self.index > 0, self.index < len(self.items) - 1)
        self.playlist_changed.emit(self.items)

    def _on_playlist_imported(self, items: list) -> None:
        if not items:
            return
        self.open_playlist(items, 0)
        self.playlist_changed.emit(self.items)

    def _on_playlist_removed(self, remaining: list) -> None:
        current = self.items[self.index].path if 0 <= self.index < len(self.items) else None
        self.items = list(remaining)
        if not self.items:
            self.close()
            return
        found = next((i for i, it in enumerate(self.items) if it.path == current), None)
        if found is None:
            # the playing item itself was removed: stay at the same position in the list
            self.index = min(self.index, len(self.items) - 1)
            self.show_index(self.index)
        else:
            self.index = found
            self.panel.set_current(self.index)
        self.controls.set_navigation(self.index > 0, self.index < len(self.items) - 1)
        self._update_top_bar(self.items[self.index])
        self.playlist_changed.emit(self.items)

    def _on_album_play(self, items: list, start: int) -> None:
        self.open_playlist(items, start)

    def _on_loop_mode(self, mode: str) -> None:
        self.controls.set_loop_mode(mode)
        # mpv can loop a single file by itself; the other modes are handled at EOF
        self.video_view.set_loop(mode == "one")

    def _wire_player(self) -> None:
        v = self.video_view
        v.position_changed.connect(self._on_position)
        v.duration_changed.connect(self._on_duration)
        v.pause_changed.connect(self._on_pause_changed)
        v.speed_changed.connect(self.controls.set_speed)
        v.volume_changed.connect(lambda x: self.controls.set_volume(int(x), v.muted))
        v.mute_changed.connect(lambda m: self.controls.set_volume(int(v.volume), m))
        v.cache_changed.connect(self.controls.set_cache_end)
        v.tracks_changed.connect(self._on_tracks)
        v.file_loaded.connect(self._on_file_loaded)
        v.eof_reached.connect(self._on_eof)
        v.error.connect(self._on_load_failed)

    # ------------------------------------------------------------ playlist

    def open_playlist(self, items: list[MediaItem], index: int) -> None:
        # Archives are browsable but not playable: keep them out of the playlist.
        target = items[index].path if 0 <= index < len(items) else None
        items = [i for i in items if not getattr(i, "is_archive", False)]
        if not items:
            return
        self.items = list(items)
        if target is not None:
            for i, it in enumerate(items):
                if it.path == target:
                    index = i
                    break
            else:
                index = 0
        if index >= len(items):
            index = len(items) - 1
        self.index = -1
        self._native_size_applied = False
        if not self.isVisible():
            # If the user wants the window to fit the video's native pixels, don't
            # maximize: we'll resize to the video's size once it's loaded. Otherwise
            # maximize as before.
            if not settings["open_native_size"]:
                self.showMaximized()
            else:
                self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self.panel.set_playlist(self.items, index)
        self._relayout()
        self.show_index(index)
        self._show_bars()
        # Thumbnails for the (possibly huge) new playlist must not compete with
        # playback: pause their generation for a moment, then resume.
        self.panel.set_thumbs_paused(True)
        QTimer.singleShot(3000, lambda: self.panel.set_thumbs_paused(False))

    def extend_playlist(self, items: list[MediaItem], index: int) -> None:
        """Replace the playlist with a fuller listing without restarting playback.

        Used when a background folder scan completes after the player launched
        with a single file: the current playback keeps running, only the panel
        list and navigation states are updated.
        """
        items = [i for i in items if not getattr(i, "is_archive", False)]
        if not items:
            return
        self.items = list(items)
        if self.index >= len(self.items):
            self.index = len(self.items) - 1
        if index >= len(self.items):
            index = len(self.items) - 1
        self.panel.set_playlist(self.items, self.index)
        self.controls.set_navigation(self.index > 0, self.index < len(self.items) - 1)
        # Same low-priority treatment as open_playlist: pause thumbnail requests
        # briefly so the scan-completion swap never stalls the playing video.
        self.panel.set_thumbs_paused(True)
        QTimer.singleShot(3000, lambda: self.panel.set_thumbs_paused(False))

    def step(self, delta: int) -> None:
        if not self.items:
            return
        target = self.index + delta
        if target < 0 or target >= len(self.items):
            self._show_toast(t("viewer.already_first") if target < 0 else t("viewer.already_last"))
            return
        self.show_index(target)

    def show_index(self, index: int) -> None:
        if not (0 <= index < len(self.items)):
            return
        self._at_eof = False                  # 换片后不再处于“播完停帧”状态
        self._remember_position()
        self.index = index
        item = self.items[index]
        self.error_label.setVisible(False)
        self.setWindowTitle(t("viewer.window_title_media").format(name=item.name))
        self._update_top_bar(item)
        self.controls.set_media_kind(item.is_video)
        self.controls.set_navigation(index > 0, index < len(self.items) - 1)
        self.panel.set_current(index)
        self.index_changed.emit(index)
        remember_recent_file(item.path)

        if item.is_video:
            self.image_view.clear()
            self.stack.setCurrentWidget(self.video_view)
            self.previewer.set_media(item.path)
            self.controls.set_duration(item.duration or 0.0)
            self.controls.set_cache_end(0.0)
            # A new clip starts without the previous one's A-B loop marks.
            self._ab_a = self._ab_b = None
            self.video_view.clear_ab_loop()
            start = resume.lookup(item.path) if settings["resume_enabled"] else None
            self.video_view.load(item.path, start)
            self.video_view.set_speed(float(settings["speed"]))
            if start:
                self._show_toast(t("viewer.resume_playback").format(pos=format_duration(start)))
        else:
            self.video_view.stop()
            self.previewer.set_media(None)
            self.stack.setCurrentWidget(self.image_view)
            if self.image_view.load(item.path):
                w, h = self.image_view.source_size
                item.width, item.height = w, h
                self._update_top_bar(item)
        self._show_bars()

    def _remember_position(self) -> None:
        if not (0 <= self.index < len(self.items)):
            return
        item = self.items[self.index]
        if not item.is_video or not settings["resume_enabled"]:
            return
        try:
            pos, dur = self.video_view.position, self.video_view.duration
        except Exception:
            return
        if pos > 0:
            resume.remember(item.path, pos, dur or None)

    # ---------------------------------------------------------- player events

    def _on_position(self, pos: float) -> None:
        if not self._scrubbing:
            self.controls.set_position(pos, self.video_view.duration)

    def _on_duration(self, dur: float) -> None:
        self.controls.set_duration(dur)
        if 0 <= self.index < len(self.items):
            self.items[self.index].duration = dur

    def _on_pause_changed(self, paused: bool) -> None:
        self.controls.set_playing(not paused)
        self._update_stage_buttons()
        if paused:
            self._show_bars()

    def _on_tracks(self, tracks) -> None:
        self.controls.set_tracks(
            tracks, self.video_view.current_track("sub"), self.video_view.current_track("audio")
        )
        self.controls.set_sub_visible(self.video_view.sub_visible)

    def _on_file_loaded(self) -> None:
        if 0 <= self.index < len(self.items):
            item = self.items[self.index]
            if item.is_video:
                # Refresh the top bar: now the codec and hwdec_current are known.
                self._update_top_bar(item)
                # the pause observer may not re-fire if the state did not change
                self.controls.set_playing(not self.video_view.paused)
                # Fit the window to the video's native pixels on first load only.
                if settings["open_native_size"] and not self._native_size_applied:
                    self._native_size_applied = True
                    self._fit_window_to_video()

    def _set_hwdec(self, mode: str) -> None:
        self.video_view.set_hwdec(mode)
        self.controls.set_hwdec_mode(mode)
        # Top bar's hwdec tag is stale until the next frame; refresh now.
        if 0 <= self.index < len(self.items) and self.items[self.index].is_video:
            self._update_top_bar(self.items[self.index])
        label = {
            "auto-safe": t("viewer.hwdec_auto_safe"),
            "auto": t("viewer.hwdec_auto"),
            "no": t("viewer.hwdec_no"),
        }.get(mode, mode)
        self._show_toast(t("viewer.hwdec_mode").format(label=label))

    # ------------------------------------------------------------ capture

    def _capture_path(self, ext: str) -> Path:
        """Where to write a capture: user-chosen dir, then beside the source video,
        then a `截图` folder next to the app (network shares are often read-only)."""
        from datetime import datetime

        item = self.items[self.index]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = Path(item.name).stem or "capture"
        name = f"{stem}_{stamp}.{ext}"

        # 1) user-configured path
        custom = str(settings["capture_path"] or "").strip()
        if custom:
            d = Path(custom)
            try:
                d.mkdir(parents=True, exist_ok=True)
                return d / name
            except Exception:
                pass

        # 2) beside the source video
        # 3) app-level fallback
        for d in (Path(item.path).parent, APP_DIR / "截图"):
            try:
                d.mkdir(parents=True, exist_ok=True)
                return d / name
            except Exception:
                continue
        return APP_DIR / name

    def _take_screenshot(self) -> None:
        if not self._current_is_video():
            self._show_toast(t("viewer.video_only_screenshot"))
            return
        path = self._capture_path("png")
        if self.video_view.screenshot(str(path)):
            self._show_toast(t("viewer.screenshot_saved").format(path=path))
            return
        # Source folder may be read-only: fall back to the app's 截图 folder.
        alt = APP_DIR / "截图" / path.name
        try:
            alt.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if self.video_view.screenshot(str(alt)):
            self._show_toast(t("viewer.screenshot_saved").format(path=alt))
        else:
            self._show_toast(t("viewer.screenshot_failed"))

    def _toggle_gif(self) -> None:
        if self._gif_recording:
            self._finish_gif()
            return
        if not self._current_is_video():
            self._show_toast(t("viewer.video_only_gif"))
            return
        # Read the (user-configurable) capture parameters afresh each time.
        fps = max(2, min(30, int(settings["gif_fps"])))
        secs = max(1, min(120, int(settings["gif_max_seconds"])))
        self._gif_interval = max(20, int(1000 / fps))
        self._gif_max = fps * secs
        self._gif_w = max(120, min(1920, int(settings["gif_max_width"])))
        self._gif_recording = True
        self._gif_frames = []
        self._gif_timer.setInterval(self._gif_interval)
        self._gif_timer.start()
        self.controls.set_gif_recording(True)
        self._show_toast(t("viewer.gif_recording"))

    def _gif_tick(self) -> None:
        if not self._gif_recording:
            return
        frame = self.video_view.grab_frame()
        if frame is not None:
            self._gif_frames.append(frame)
        if len(self._gif_frames) >= self._gif_max:
            self._finish_gif()

    def _finish_gif(self) -> None:
        self._gif_timer.stop()
        self._gif_recording = False
        self.controls.set_gif_recording(False)
        frames, self._gif_frames = self._gif_frames, []
        if len(frames) < 2:
            self._show_toast(t("viewer.gif_too_short"))
            return
        path = self._capture_path("gif")
        self._show_toast(t("viewer.gif_generating").format(count=len(frames)))
        import threading

        threading.Thread(
            target=self._save_gif, args=(frames, path), daemon=True
        ).start()

    def _save_gif(self, frames: list, path) -> None:
        """Assemble collected frames into a looping GIF (runs off the GUI thread)."""
        try:
            from PIL import Image

            smalls = []
            for f in frames:
                im = f.convert("RGB")
                w, h = im.size
                if w > self._gif_w:
                    im = im.resize(
                        (self._gif_w, max(1, int(h * self._gif_w / w))), Image.BILINEAR
                    )
                smalls.append(im)
            try:
                target = path
                smalls[0].save(
                    str(target), save_all=True, append_images=smalls[1:],
                    duration=self._gif_interval, loop=0, optimize=True, disposal=2,
                )
            except Exception:
                target = APP_DIR / "截图" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                smalls[0].save(
                    str(target), save_all=True, append_images=smalls[1:],
                    duration=self._gif_interval, loop=0, optimize=True, disposal=2,
                )
            self._capture_saved.emit(t("viewer.gif_saved").format(path=target))
        except Exception:
            self._capture_saved.emit(t("viewer.gif_failed"))

    # ------------------------------------------------------- picture / A-B

    def _reset_video_eq(self) -> None:
        self.video_view.reset_video_eq()
        self._show_toast(t("viewer.eq_reset"))

    def _set_ab_point(self) -> None:
        """I key: set A, then B, then (third press) clear -- an in/out loop."""
        if not self._current_is_video():
            return
        pos = self.video_view.position
        if self._ab_a is None:
            self._ab_a = pos
            self.video_view.set_ab_loop("a", pos)
            self._show_toast(t("viewer.ab_loop_start").format(pos=format_duration(pos)))
        elif self._ab_b is None:
            if pos <= self._ab_a:
                self._show_toast(t("viewer.ab_end_after_start"))
                return
            self._ab_b = pos
            self.video_view.set_ab_loop("b", pos)
            self._show_toast(
                t("viewer.ab_loop_set").format(a=format_duration(self._ab_a), b=format_duration(pos))
            )
        else:
            self._clear_ab_loop()

    def _clear_ab_loop(self) -> None:
        if self._ab_a is None and self._ab_b is None:
            return
        self._ab_a = self._ab_b = None
        self.video_view.clear_ab_loop()
        self._show_toast(t("viewer.ab_loop_cleared"))

    # ------------------------------------------------------------ drag & drop

    def dragEnterEvent(self, e):  # noqa: ANN001
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: ANN001
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.toLocalFile()]
        self._open_dropped(paths)

    def _open_dropped(self, paths: list) -> None:
        # A single dropped folder: hand it to the browser to scan and play.
        if len(paths) == 1 and paths[0].is_dir():
            self.folder_requested.emit(paths[0])
            return
        items = []
        for p in paths:
            if p.is_dir():
                continue
            it = item_for_path(p)
            if it is not None:
                items.append(it)
        if items:
            self.open_playlist(items, 0)
            self.playlist_changed.emit(self.items)
        else:
            self._show_toast(t("viewer.no_playable_media"))

    def _fit_window_to_video(self) -> None:
        """Resize the window so the video area matches the video's native pixels.

        Accounts for devicePixelRatio (HiDPI) and clamps to 92% of the screen the
        window is on, so an 8K video doesn't blow past the monitor.
        """
        if self.isFullScreen() or self.isMaximized():
            return
        vw, vh = self.video_view.video_native_size
        if vw <= 0 or vh <= 0:
            return
        dpr = self.devicePixelRatioF()
        # Window chrome: total size minus the video area's current size.
        chrome_w = self.width() - int(self.stage.width() * dpr)
        chrome_h = self.height() - int(self.stage.height() * dpr)
        target_w = int(vw / dpr) + chrome_w
        target_h = int(vh / dpr) + chrome_h
        # Clamp to the screen the window is currently on.
        screen = self.screen() or self.windowHandle().screen() if self.windowHandle() else None
        if screen is not None:
            avail = screen.availableGeometry()
            max_w = int(avail.width() * 0.92)
            max_h = int(avail.height() * 0.92)
            target_w = min(target_w, max_w)
            target_h = min(target_h, max_h)
            # Centre on that screen.
            cx = avail.x() + (avail.width() - target_w) // 2
            cy = avail.y() + (avail.height() - target_h) // 2
            self.setGeometry(cx, cy, target_w, target_h)
        else:
            self.resize(target_w, target_h)

    def _on_eof(self) -> None:
        if 0 <= self.index < len(self.items):
            resume.forget(self.items[self.index].path)
        mode = str(settings["loop_mode"])
        n = len(self.items)

        if mode == "one":
            self._at_eof = False
            self.video_view.seek_absolute(0)   # mpv's loop-file also covers this
            self.video_view.set_pause(False)
            return
        if mode == "shuffle" and n > 1:
            import random as _random

            choices = [i for i in range(n) if i != self.index]
            self.show_index(_random.choice(choices))
            return
        if mode == "list" and n > 1:
            self.show_index((self.index + 1) % n)   # wraps at the end
            return
        if settings["autoplay_next"] and self.index < n - 1:
            self.step(1)
            return
        # 不自动切换：停在最后一帧的暂停状态，等待用户再次点播放时从头开始
        self.video_view.set_pause(True)
        self._at_eof = True
        self._show_bars()

    def _on_load_failed(self, message: str) -> None:
        self.error_label.setText(f"⚠  {message}")
        self.error_label.setVisible(True)
        self.error_label.raise_()

    def _on_zoom_changed(self, scale: float) -> None:
        self.controls.set_zoom(scale)

    # -------------------------------------------------------------- actions

    def _toggle_pause(self) -> None:
        if not self._current_is_video():
            return
        if self._at_eof:
            # 上一段已播完停在最后一帧：再次播放从该视频头部重新开始
            self._at_eof = False
            self.video_view.seek_absolute(0)
            self.video_view.set_pause(False)
            return
        self.video_view.toggle_pause()

    def _seek_absolute(self, seconds: float) -> None:
        """手动定位后播放应从该处继续（退出“播完停帧”状态）。"""
        self._at_eof = False
        self.video_view.seek_absolute(seconds)

    def _seek_relative(self, delta: float) -> None:
        self._at_eof = False
        self.video_view.seek_relative(delta)

    def _toggle_mute(self) -> None:
        self.video_view.toggle_mute()

    def _set_speed(self, value: float) -> None:
        self.video_view.set_speed(value)
        self._show_toast(t("viewer.speed_toast").format(speed=f"{value:g}"))

    def _nudge_speed(self, direction: int) -> None:
        cur = self.video_view.speed
        nearest = min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - cur))
        self._set_speed(SPEEDS[max(0, min(len(SPEEDS) - 1, nearest + direction))])

    def _step_sub_font(self, step: int) -> None:
        self.video_view.set_sub_font_size(self.video_view.sub_font_size + step)
        self._show_toast(t("viewer.sub_font_toast").format(size=self.video_view.sub_font_size))

    def _step_sub_delay(self, delta: float) -> None:
        if delta == 0.0:
            self.video_view.mpv.sub_delay = 0.0
        else:
            self.video_view.adjust_sub_delay(delta)
        self._show_toast(t("viewer.sub_delay_toast").format(delay=f"{self.video_view.sub_delay:+.1f}"))

    def _toggle_sub_visible(self) -> None:
        vis = not self.video_view.sub_visible
        self.video_view.set_sub_visible(vis)
        self.controls.set_sub_visible(vis)
        self._show_toast(t("viewer.sub_visible") if vis else t("viewer.sub_hidden"))

    def _adjust_volume(self, delta: int) -> None:
        self.video_view.set_volume(self.video_view.volume + delta)
        self._show_toast(t("viewer.volume_toast").format(volume=int(self.video_view.volume)))

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showMaximized()
        else:
            self.showFullScreen()
        self.controls.set_fullscreen(self.isFullScreen())
        self._show_bars()

    def _current_is_video(self) -> bool:
        return 0 <= self.index < len(self.items) and self.items[self.index].is_video

    # ------------------------------------------------------------ overlays

    def _update_top_bar(self, item: MediaItem) -> None:
        self.top_bar.title.setText(item.name)
        self.top_bar.counter.setText(f"{self.index + 1} / {len(self.items)}")
        bits: list[str] = []
        if item.is_video:
            if item.resolution_text():
                bits.append(item.resolution_text())
            codec = self.video_view.video_codec
            if codec:
                bits.append(codec.split("(")[0].strip())
            hw = self.video_view.hwdec_active
            bits.append(t("viewer.hwdec_on").format(mode=hw) if hw and hw != "no" else t("viewer.swdec"))
        else:
            if item.resolution_text():
                bits.append(item.resolution_text())
            bits.append(item.suffix.lstrip(".").upper())
        bits.append(item.size_text())
        self.top_bar.info.setText("　·　".join(bits))

    def _show_toast(self, text: str) -> None:
        self.toast.setText(text)
        self.toast.adjustSize()
        self.toast.move(
            (self.width() - self.toast.width()) // 2, self.height() - self.controls.height() - 54
        )
        self.toast.setVisible(True)
        self.toast.raise_()
        self._toast_timer.start(1800)

    def _show_bars(self) -> None:
        self._move_accum = 0.0
        if not self._bars_visible:
            self._bars_visible = True
            self.top_bar.setVisible(True)
            self.controls.setVisible(True)
        self.top_bar.raise_()
        self.controls.raise_()
        self._update_stage_buttons()
        self.unsetCursor()
        self._hide_timer.start()

    def _hide_bars(self) -> None:
        if self._under_bars(self.mapFromGlobal(QCursor.pos())):
            self._hide_timer.start()
            return
        if self._current_is_video() and self.video_view.paused:
            self._hide_timer.start()
            return
        self._bars_visible = False
        self._move_accum = 0.0  # a fresh count starts once the bars are gone
        self.top_bar.setVisible(False)
        self.controls.setVisible(False)
        self._update_stage_buttons()
        if self._current_is_video():
            self.setCursor(Qt.BlankCursor)

    def _under_bars(self, pos: QPoint | None) -> bool:
        if pos is None:
            return False
        if self.top_bar.geometry().contains(pos) or self.controls.geometry().contains(pos):
            return True
        # Keep the bars up while the cursor rests on a stage button, so it does not
        # vanish from under the pointer just as it is about to be clicked.
        return any(b.isVisible() and b.geometry().contains(pos) for b in self._stage_buttons)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self) -> None:
        """Video area shrinks to make room for the panel; overlays follow it."""
        panel_w = self.panel.width() if self.panel.isVisible() else 0
        panel_w = min(panel_w, max(0, self.width() - 320))  # never squeeze out the video
        video_w = max(1, self.width() - panel_w)

        self.stage.setGeometry(0, 0, video_w, self.height())
        if panel_w:
            self.panel.setGeometry(video_w, 0, panel_w, self.height())
        self.top_bar.setGeometry(0, 0, video_w, self.top_bar.height())
        ch = self.controls.sizeHint().height()
        self.controls.setGeometry(0, self.height() - ch, video_w, ch)
        self.error_label.setGeometry(0, 0, video_w, self.height())
        if self._live_label.isVisible():
            width_pct = self._live_caption_display_value("width")
            height_pct = self._live_caption_display_value("height")
            w = max(240, int(video_w * width_pct / 100))
            h = max(48, int(self.height() * height_pct / 100))
            self._live_label.setGeometry(
                (video_w - w) // 2, max(0, self.height() - h - ch - 24),
                w, h,
            )
            self._live_label.raise_()
        self._layout_stage_buttons(video_w, ch)
        self.top_bar.raise_()
        self.controls.raise_()

    # The brief asks for side buttons "smaller than a quarter of the player"; a quarter
    # of the shorter edge is the ceiling, and in practice the scale below keeps them
    # well under it while staying comfortably clickable on a small window.
    STAGE_BTN_SCALE = 0.085
    STAGE_BTN_MIN = 38
    STAGE_BTN_MAX = 84

    def _stage_button_size(self, video_w: int) -> int:
        d = int(min(video_w, self.height()) * self.STAGE_BTN_SCALE)
        d = max(self.STAGE_BTN_MIN, min(self.STAGE_BTN_MAX, d))
        # hard ceiling, so a very small window can never grow them past the limit
        return min(d, max(24, int(min(video_w, self.height()) / 4) - 1))

    def _layout_stage_buttons(self, video_w: int, controls_h: int) -> None:
        if not hasattr(self, "_stage_buttons"):
            return  # a resize can arrive while the window is still being built
        d = self._stage_button_size(video_w)
        # Vertically centred on the picture rather than the window: the bottom bar
        # covers the lower strip, so the true middle sits a little above centre.
        cy = (self.height() - controls_h // 2) // 2
        self.btn_stage_prev.setGeometry(int(d * 0.45), cy - d // 2, d, d)
        self.btn_stage_next.setGeometry(video_w - d - int(d * 0.45), cy - d // 2, d, d)
        # Same footprint as the side buttons: the plain glyph needs no extra room.
        self.btn_stage_play.setGeometry((video_w - d) // 2, cy - d // 2, d, d)

    def _update_stage_buttons(self) -> None:
        """Show what makes sense for the current item, and only while the bars are up.

        The bars already track "has the mouse moved recently", which is exactly the
        hover behaviour wanted here, so these ride along with them instead of keeping a
        second timer that could disagree with the first.
        """
        show = self._bars_visible and bool(self.items)
        is_video = self._current_is_video()
        # Hidden by default while playing: the centre button only surfaces as a
        # play glyph when the video is paused, regardless of the bars.
        paused = is_video and self.video_view.paused
        self.btn_stage_play.setVisible(bool(self.items) and paused)
        self.btn_stage_prev.setVisible(show and self.index > 0)
        self.btn_stage_next.setVisible(show and self.index < len(self.items) - 1)
        if is_video:
            self.btn_stage_play.set_glyph(
                icons.PLAY if self.video_view.paused else icons.PAUSE
            )
        for b in self._stage_buttons:
            if b.isVisible():
                b.raise_()

    # ---------------------------------------------------------- input

    def wheelEvent(self, e):
        mods = e.modifiers()
        dy = e.angleDelta().y()
        if mods & Qt.ControlModifier:
            if self._current_is_video():
                self._adjust_volume(5 if dy > 0 else -5)
            else:
                self.image_view.zoom_by_steps(
                    1 if dy > 0 else -1, e.position()
                )
            e.accept()
            return
        # plain wheel = previous / next media
        self._wheel_accum += dy
        while abs(self._wheel_accum) >= 120:
            direction = -1 if self._wheel_accum > 0 else 1
            self._wheel_accum -= 120 if self._wheel_accum > 0 else -120
            self.step(direction)
        self._show_bars()
        e.accept()

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._bars_visible:
            self._show_bars()  # already up: just keep the hide timer fresh
        else:
            # Only deliberate movement brings the bars back: accumulate travel
            # and ignore anything below the jitter threshold.
            if self._last_move_pos is not None:
                d = pos - self._last_move_pos
                self._move_accum += (d.x() ** 2 + d.y() ** 2) ** 0.5
            if self._move_accum >= SHOW_MOVE_THRESHOLD_PX:
                self._show_bars()
        self._last_move_pos = pos
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        # Forget the last position: re-entering at the far side of the window
        # must not count the teleport as deliberate travel.
        self._last_move_pos = None
        super().leaveEvent(e)

    def contextMenuEvent(self, e) -> None:
        """Right-click on the playing media: subtitle controls + copy actions."""
        if not self.items or not (0 <= self.index < len(self.items)):
            return
        item = self.items[self.index]
        menu = QMenu(self)

        if item.is_video:
            sub = menu.addMenu(t("viewer.sub_menu"))
            a = sub.addAction(t("viewer.sub_font_bigger"))
            a.triggered.connect(lambda _=False: self._step_sub_font(2))
            a = sub.addAction(t("viewer.sub_font_smaller"))
            a.triggered.connect(lambda _=False: self._step_sub_font(-2))
            a = sub.addAction(t("viewer.sub_font_reset"))
            a.triggered.connect(lambda _=False: self._reset_sub_font())
            sub.addSeparator()
            a = sub.addAction(
                t("viewer.sub_hide") if self.video_view.sub_visible else t("viewer.sub_show")
            )
            a.triggered.connect(lambda _=False: self._toggle_sub_visible())
            sub.addSeparator()
            a = sub.addAction(t("viewer.sub_load_file"))
            a.triggered.connect(lambda _=False: self._pick_subtitle_file())
            sub.addSeparator()
            a = sub.addAction(
                t("viewer.live_caption_off") if self._live_on else t("viewer.live_caption_on")
            )
            a.triggered.connect(lambda _=False: self._toggle_live_caption())
            display = sub.addMenu(t("viewer.live_caption_display_menu"))
            a = display.addAction(t("viewer.live_caption_font_bigger"))
            a.triggered.connect(lambda _=False: self._step_live_caption_display("font", 2))
            a = display.addAction(t("viewer.live_caption_font_smaller"))
            a.triggered.connect(lambda _=False: self._step_live_caption_display("font", -2))
            display.addSeparator()
            a = display.addAction(t("viewer.live_caption_width_wider"))
            a.triggered.connect(lambda _=False: self._step_live_caption_display("width", 4))
            a = display.addAction(t("viewer.live_caption_width_narrower"))
            a.triggered.connect(lambda _=False: self._step_live_caption_display("width", -4))
            a = display.addAction(t("viewer.live_caption_height_taller"))
            a.triggered.connect(lambda _=False: self._step_live_caption_display("height", 2))
            a = display.addAction(t("viewer.live_caption_height_shorter"))
            a.triggered.connect(lambda _=False: self._step_live_caption_display("height", -2))
            display.addSeparator()
            a = display.addAction(t("viewer.live_caption_display_reset"))
            a.triggered.connect(lambda _=False: self._reset_live_caption_display())
            menu.addSeparator()

        if not item.is_video:
            act = menu.addAction(t("menu.copy_image"))
            act.triggered.connect(lambda _=False, it=item: self._copy_current_image(it))
        act = menu.addAction(t("menu.copy_file"))
        act.triggered.connect(lambda _=False, it=item: self._copy_current_file(it))
        menu.exec(e.globalPos())

    def _reset_sub_font(self) -> None:
        self.video_view.set_sub_font_size(int(settings["sub_font_size"]))
        self._show_toast(t("viewer.sub_font_toast").format(size=self.video_view.sub_font_size))

    def _pick_subtitle_file(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        start = str(self.items[self.index].path.parent) if self.items else ""
        path, _ = QFileDialog.getOpenFileName(
            self, t("viewer.sub_load_file"), start,
            t("viewer.sub_file_filter"),
        )
        if path:
            self.video_view.add_subtitle_file(path)
            self._show_toast(t("viewer.sub_loaded"))

    def _toggle_live_caption(self) -> None:
        if self._live_on or self._live_paused:
            self._stop_live_caption()
        else:
            self._start_live_caption()

    def _live_caption_display_value(self, key: str) -> int:
        ranges = {
            "font": (12, 96),
            "width": (40, 100),
            "height": (8, 40),
        }
        lo, hi = ranges[key]
        return max(lo, min(hi, int(settings[f"live_caption_{key}"])))

    def _apply_live_caption_style(self, relayout: bool = True) -> None:
        """Apply the user-editable live-caption font and readable backdrop."""
        size = self._live_caption_display_value("font")
        self._live_label.setStyleSheet(
            "QLabel#LiveCaption {"
            "background: rgba(0, 0, 0, 150);"
            "color: #ffffff;"
            "border-radius: 6px;"
            "padding: 8px 12px;"
            f"font-size: {size}px;"
            "font-weight: 600;"
            "}"
        )
        if relayout:
            self._relayout()

    def _step_live_caption_display(self, key: str, delta: int) -> None:
        setting = f"live_caption_{key}"
        value = max(12 if key == "font" else 8 if key == "height" else 40,
                    min(96 if key == "font" else 40 if key == "height" else 100,
                        self._live_caption_display_value(key) + delta))
        settings[setting] = value
        if key == "font":
            self._apply_live_caption_style()
        else:
            self._relayout()
        message = {
            "font": t("viewer.live_caption_font_toast").format(size=value),
            "width": t("viewer.live_caption_width_toast").format(width=value),
            "height": t("viewer.live_caption_height_toast").format(height=value),
        }[key]
        self._show_toast(message)
        from .config import flush

        flush()

    def _reset_live_caption_display(self) -> None:
        settings["live_caption_font_size"] = 32
        settings["live_caption_width"] = 84
        settings["live_caption_height"] = 16
        self._apply_live_caption_style()
        self._show_toast(t("viewer.live_caption_display_reset"))
        from .config import flush

        flush()

    def _find_pipeline(self):
        from .config import find_subtitle_pipeline_dir

        return find_subtitle_pipeline_dir()

    def _start_live_caption(self) -> None:
        """启动环路录音实时听译（解耦进程 + log 文件监视）。

        - 进程用 subprocess 分离启动（不随本界面销毁），JSON 行写 log 文件
        - 已有活跃进程（上次开着/关界面未杀）→ 直接复用，秒出
        - 常驻模式：停止只暂停显示；关闭播放界面默认不杀模型
        """
        import subprocess
        from pathlib import Path as _Path

        from .config import settings as _settings

        pipe = self._find_pipeline()
        if pipe is None:
            self._show_toast(t("viewer.live_caption_no_engine"))
            return

        self._live_log = pipe / "live-caption.log"
        self._live_pid = pipe / "live-caption.log.pid"

        source = str(_settings["live_caption_source"])
        self._live_source = source
        current_media = None
        if source == "audio":
            # 音轨模式：直接读当前播放的视频音轨（无录音设备、不受系统声音干扰）
            if not self.items or not (0 <= self.index < len(self.items)):
                self._show_toast(t("viewer.live_caption_no_media"))
                return
            current_media = self.items[self.index].path
        tr_model = str(_settings["live_ollama_model"])
        state_file = pipe / "live-caption.state"
        wanted_state = {"source": source, "media": str(current_media or ""),
                        "translate": tr_model}

        def _state_matches() -> bool:
            try:
                import json as _json

                state = _json.loads(state_file.read_text(encoding="utf-8"))
                return state == wanted_state
            except Exception:
                return False

        # 1) 常驻/复用：相同来源/媒体/翻译模型的活跃进程 → 直接开始监视，秒出
        if self._check_live_alive() and _state_matches():
            self._live_paused = False
            self._live_on = True
            self._live_rows = []
            self._live_label.setText(t("viewer.live_caption_running"))
            self._live_label.show()
            self._live_label.raise_()
            self._relayout()
            self._show_toast(t("viewer.live_caption_resumed"))
            self._start_live_poll()
            return

        # 2) 启动解耦子进程（先清残留旧实例：含锁丢失的僵尸进程，保证单进程）
        self._kill_all_live_procs()
        exe = pipe / ".venv" / "Scripts" / "pythonw.exe"
        log_path = str(self._live_log)
        log_path = str(_Path(log_path))
        lang = str(_settings["live_caption_lang"])
        asr_model = str(_settings["live_asr_model"]) or "medium"
        asr_dir = str(_settings["live_asr_dir"] or "").strip()
        translate_on = tr_model != "none"
        common = ["--log", log_path, "--lang", lang, "--model", asr_model,
                  "--ollama-model", tr_model]
        if asr_dir:
            common += ["--model-dir", asr_dir]
        if source == "audio":
            media = self.items[self.index].path
            script = pipe / "live_transcribe.py"
            args = [str(script), str(media)] + common + (["--translate"] if translate_on else [])
            # 从当前播放位置追赶（seek 跳转后同样用最新位置）
            try:
                pos = float(self.video_view.position or 0.0)
            except Exception:
                pos = 0.0
            if pos > 10:
                args += ["--seek", str(int(pos))]
        else:
            # 系统声音（环路录音）模式
            script = pipe / "live_capture.py"
            args = [str(script), "--json"] + common + (["--translate"] if translate_on else [])
        env = {k: v for k, v in os.environ.items()}
        nv = pipe / ".venv" / "Lib" / "site-packages" / "nvidia"
        for d in ("cublas", "cudnn", "cuda_nvrtc"):
            p = str(nv / d / "bin")
            if p not in env.get("PATH", ""):
                env["PATH"] = p + os.pathsep + env.get("PATH", "")
        # 强制 HF 缓存到引擎目录（覆盖用户环境的 E 盘变量，防止模型下载失败崩溃）
        env["HUGGINGFACE_HUB_CACHE"] = str(pipe / "models" / "hf" / "hub")
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS \
            if hasattr(subprocess, "DETACHED_PROCESS") else 0
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE：双保险，绝不弹 .venv 命令行窗口
        # stderr 落盘：崩溃 Traceback（CUDA OOM/模型加载失败）不再被 DEVNULL 吞掉
        self._live_log.unlink(missing_ok=True)
        self._live_pid.unlink(missing_ok=True)
        state_file.write_text(json.dumps(wanted_state, ensure_ascii=False), encoding="utf-8")
        err_path = pipe / "live-caption.err"
        try:
            self._live_err_fh = open(err_path, "w", encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            self._live_err_fh = subprocess.DEVNULL
        try:
            self._live_proc = subprocess.Popen(
                [str(exe), *args], env=env,
                stdout=subprocess.DEVNULL, stderr=self._live_err_fh,
                creationflags=flags, startupinfo=si,
            )
        except Exception as exc:  # noqa: BLE001
            self._show_toast(t("viewer.live_caption_error").format(err=str(exc)[:120]))
            return
        # 保存启动参数，供 seek 越界自动重转复用
        self._live_args_base = [str(exe), *args]
        self._live_env = env
        self._live_flags = flags
        self._live_si = si
        # 新进程刚起，作废旧的健康缓存
        self._live_alive_cache = None

        import time as _time

        self._live_started_at = _time.time()

        self._live_rows = []
        self._live_label.setText(t("viewer.live_caption_starting"))
        self._live_label.show()
        self._live_label.raise_()
        self._relayout()
        self._live_on = True
        self._start_live_poll()
        self._show_toast(t("viewer.live_caption_running"))

    # ------------------------------------------------------------------ log watch

    def _live_log_path(self):
        return getattr(self, "_live_log", None)

    def _is_live_alive(self) -> bool:
        """非阻塞存活判定：读缓存（<2s 新鲜直接用），过期则触发后台检查。

        tasklist/wmic 在 UI 线程同步执行会阻塞数百毫秒（每 600ms 轮询一次
        就是持续掉帧），因此实际命令一律放到后台线程，UI 线程只读缓存。
        """
        import time as _time

        cache = getattr(self, "_live_alive_cache", None)
        now = _time.time()
        if cache is not None and now - cache[1] < 2.0:
            return cache[0]
        if not getattr(self, "_live_checking", False):
            self._live_checking = True

            def _job():
                try:
                    result = self._check_live_alive()
                except Exception:  # noqa: BLE001
                    result = True
                self._live_alive_cache = (result, _time.time())
                self._live_checking = False

            threading.Thread(target=_job, daemon=True).start()
        # 无缓存时乐观返回 True，避免误判导致刚启动的进程被复位
        return True if cache is None else cache[0]

    def _check_live_alive(self) -> bool:
        """按 pid 锁 + log 新鲜度判断已有字幕进程是否存活且健康（阻塞，勿在 UI 线程直接调用）。

        不健康（进程活着但 log 长期无产出 = 卡死）返回 False，由启动逻辑
        杀掉旧进程再重建，杜绝多进程积累。
        """
        import subprocess as _sp
        import time as _time

        pid_file = getattr(self, "_live_pid", None)
        if pid_file is None or not pid_file.is_file():
            return False
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            return False
        try:
            out = _sp.run(["tasklist", "/FI", f"PID eq {pid}"],
                          capture_output=True, text=True, timeout=10,
                          errors="replace",
                          creationflags=_sp.CREATE_NO_WINDOW).stdout
            if str(pid) not in out or "python" not in out.lower():
                return False
        except Exception:
            return False
        # 健康检查：log 在 60s 内有产出 = 正常工作；log 尚无但进程启动 <90s = 加载中
        log = self._live_log_path()
        if log is not None and log.is_file() and log.stat().st_mtime > _time.time() - 60:
            return True
        try:
            fi = _sp.run(["wmic", "process", "where", f"ProcessId={pid}",
                          "get", "CreationDate", "/value"],
                         capture_output=True, text=True, timeout=10,
                         errors="replace",
                         creationflags=_sp.CREATE_NO_WINDOW).stdout
            born = fi.split("=")[-1].strip()
            ts = _time.mktime(_time.strptime(born[:14], "%Y%m%d%H%M%S"))
            if _time.time() - ts < 90:
                return True  # 刚启动仍在加载模型（GPU 竞争时 medium 可到 40s+）
        except Exception:
            return True  # 拿不到时间就视为健康，避免误杀
        print(f"[viewer] pid={pid} 字幕进程 90s 无产出，判定卡死")
        return False

    def _kill_live_proc(self) -> None:
        """终止字幕进程（释放显存），并清锁；随后由启动逻辑重建。"""
        import subprocess as _sp

        pid_file = getattr(self, "_live_pid", None)
        if pid_file is not None and pid_file.is_file():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                _sp.run(["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True, timeout=10,
                        creationflags=_sp.CREATE_NO_WINDOW)
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass
        if getattr(self, "_live_proc", None) is not None:
            try:
                self._live_proc.kill()
            except Exception:
                pass
        self._live_proc = None

    def _kill_all_live_procs(self) -> None:
        """按命令行匹配杀掉所有实时字幕进程（含锁丢失的孤儿），保证单进程。

        同时匹配 live_capture 与 live_transcribe（音轨模式残留此前清不掉）；
        wmic 在新 Win11 已移除，失败时回退 PowerShell CIM 查询。
        """
        ids: list[int] = []
        for cmd in (
            ["wmic", "process", "where",
             "name like '%pythonw%.exe' and (commandline like '%live_capture%' "
             "or commandline like '%live_transcribe%')",
             "get", "ProcessId", "/value"],
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'pythonw%.exe'\" | "
             "Where-Object { $_.CommandLine -match 'live_(capture|transcribe)' } | "
             "ForEach-Object { 'ProcessId=' + $_.ProcessId }"],
        ):
            try:
                done = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15,
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                out = done.stdout
                ids = [int(line.split("=")[-1]) for line in out.splitlines()
                       if "ProcessId=" in line and line.split("=")[-1].strip().isdigit()]
                if done.returncode == 0 or ids:
                    break
            except Exception:
                continue
        for pid in ids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception:
                pass
        pid_file = getattr(self, "_live_pid", None)
        if pid_file is not None:
            pid_file.unlink(missing_ok=True)
        self._live_proc = None
        self._live_alive_cache = None

    def _start_live_poll(self) -> None:
        if getattr(self, "_live_poll", None) is None:
            self._live_poll = QTimer(self)
            self._live_poll.setInterval(600)
            self._live_poll.timeout.connect(self._poll_live_log)
        self._live_poll.start()
        self._live_log_pos = 0

    def _stop_live_poll(self) -> None:
        tmr = getattr(self, "_live_poll", None)
        if tmr is not None:
            tmr.stop()

    def _poll_live_log(self) -> None:
        """读 log 新增行：JSON→显示；错误行→提示；启动宽限期外进程死了→复位。"""
        import json as _json
        import time as _time

        # 启动宽限期：模型加载（GPU 竞争时 medium 实测 40s+）期间不判死，
        # 但日志仍然实时读取，让翻译状态尽早可见。
        started = getattr(self, "_live_started_at", None)
        in_grace = started is not None and _time.time() - started < 90

        log = self._live_log_path()
        if log is None or not log.is_file():
            if not in_grace and not self._is_live_alive() and self._live_on:
                self._live_on = False
                self._live_label.hide()
                self._show_toast(t("viewer.live_caption_exited"))
            return
        try:
            with open(log, "r", encoding="utf-8") as f:
                f.seek(getattr(self, "_live_log_pos", 0))
                new = f.read()
                self._live_log_pos = f.tell()
        except Exception:
            return
        for line in new.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    obj = _json.loads(line)
                except Exception:
                    continue
                t0 = float(obj.get("t", 0))
                seg = obj.get("text", "").strip()
                zh = obj.get("zh", "").strip()
                if not seg or self._live_paused or not self._live_on:
                    continue
                self._live_rows.append((t0, t0, seg, zh))
            elif line.startswith("# TRANSLATE_READY "):
                model = line.split(" ", 2)[2].strip()
                self._show_toast(t("viewer.live_caption_translation_ready").format(model=model))
            elif line.startswith("# TRANSLATE_ERROR "):
                error = line.split(" ", 2)[2].strip()
                self._show_toast(t("viewer.live_caption_translation_error").format(error=error))
            else:
                if any(k in line for k in ("Traceback", "Error", "✗", "RuntimeError")):
                    self._show_toast(t("viewer.live_caption_error").format(err=line[:120]))
        # 显示：音轨模式按播放位置选行；环路模式显示最新行
        if self._live_on and not self._live_paused and self._live_rows:
            if getattr(self, "_live_source", "audio") == "audio":
                try:
                    pos = float(self.video_view.position or 0.0)
                except Exception:
                    pos = 0.0
                rows = sorted(self._live_rows, key=lambda r: r[0])
                cur = [r for r in rows if r[0] <= pos]
                if cur:
                    _t0, _t1, seg, zh = cur[-1]
                    self._live_label.setText((seg + "\n" + zh).strip() if zh else seg)
                    self._live_label.show()
                    self._live_label.raise_()
                # 播放位置远超已转写末尾（seek 跳转/转写未追上）→ 自动重转
                if pos > rows[-1][0] + 30 and hasattr(self, "_live_args_base"):
                    self._show_toast(t("viewer.live_caption_catching"))
                    self._restart_live_for_seek(int(pos))
            else:
                _t0, _t1, seg, zh = self._live_rows[-1]
                self._live_label.setText((seg + "\n" + zh).strip() if zh else seg)
                self._live_label.show()
                self._live_label.raise_()
        # 进程死掉（非用户主动停止）——显式提示，不再静默复位
        if self._live_on and not in_grace and not self._is_live_alive():
            self._live_on = False
            self._live_label.hide()
            tail = self._live_err_tail()
            msg = t("viewer.live_caption_exited")
            if tail:
                msg += f"\n{tail}"
            self._show_toast(msg)

    def _live_err_tail(self, limit: int = 3) -> str:
        """读 live-caption.err 最后几行（子进程崩溃原因），供退出提示展示。"""
        err_file = getattr(self, "_live_log", None)
        if err_file is None:
            return ""
        err_path = err_file.parent / "live-caption.err"
        try:
            lines = [ln for ln in err_path.read_text(
                encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            return "\n".join(lines[-limit:])[:200]
        except Exception:
            return ""

    def _stop_live_caption(self) -> None:
        """停止显示并保存会话。常驻模式进程保活（模型保留，重开秒出）。"""
        from .config import settings as _settings

        resident = bool(_settings["live_caption_resident"])
        self._stop_live_poll()
        self._live_on = False
        self._live_label.hide()
        self._save_live_srt()
        if resident:
            # 常驻：进程保活，仅暂停显示/收集
            self._live_paused = True
            self._show_toast(t("viewer.live_caption_resident"))
        else:
            self._kill_live_proc()
            self._show_toast(t("viewer.live_caption_stopped"))

    def _save_live_srt(self) -> None:
        from .config import settings as _settings

        if not self._live_rows:
            return
        # 音轨模式：end 占位（end<=start）用下一句开始时间补全
        rows = sorted(self._live_rows, key=lambda r: r[0])
        fixed = []
        for i, (st, en, seg, zh) in enumerate(rows):
            end = en if en > st else (rows[i + 1][0] if i + 1 < len(rows) else st + 8.0)
            fixed.append((st, end, seg, zh))
        if _settings["subtitle_save_dir"] == "player":
            out_dir = APP_DIR
        else:
            out_dir = self.items[self.index].path.parent if self.items else APP_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        name = self.items[self.index].path.stem if self.items else "live"
        srt = out_dir / f"{name}.live.srt"
        _write_srt_file(srt, fixed)
        self._show_toast(t("viewer.live_caption_saved").format(path=srt.name))

    def _restart_live_for_seek(self, pos: int) -> None:
        """播放位置超出已转写范围 → 以 --seek pos 重启转写进程（追进度）。"""
        import time as _time

        base = getattr(self, "_live_args_base", None)
        if not base:
            return
        # 轮询线程内：只做 pid 快杀（不走 wmic 全量匹配，避免 UI 长阻塞）
        self._kill_live_proc()
        self._live_rows = []
        self._live_log_pos = 0
        self._live_log.unlink(missing_ok=True)
        # 剔除旧的 --seek 值再追加最新位置，避免参数重复
        args = []
        skip = False
        for a in base:
            if a == "--seek":
                skip = True
                continue
            if skip:
                skip = False
                continue
            args.append(a)
        args += ["--seek", str(pos)]
        err_path = self._live_log.parent / "live-caption.err"
        try:
            self._live_err_fh = open(err_path, "w", encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            self._live_err_fh = subprocess.DEVNULL
        try:
            self._live_proc = subprocess.Popen(
                args, env=self._live_env,
                stdout=subprocess.DEVNULL, stderr=self._live_err_fh,
                creationflags=self._live_flags, startupinfo=self._live_si,
            )
        except Exception as exc:  # noqa: BLE001
            self._show_toast(t("viewer.live_caption_error").format(err=str(exc)[:120]))
            return
        self._live_started_at = _time.time()
        self._live_alive_cache = None

    def _copy_current_image(self, item: MediaItem) -> None:
        ok = fileops.copy_image_to_clipboard(item.path)
        self._show_toast(t("menu.copy_done") if ok else t("menu.copy_failed"))

    def _copy_current_file(self, item: MediaItem) -> None:
        fileops.copy_files_to_clipboard([item.path])
        self._show_toast(t("menu.copy_done"))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._current_is_video():
            # defer, so a double click (fullscreen) does not also toggle pause
            if self._pending_click is not None:
                self._pending_click.stop()
            self._pending_click = QTimer(self)
            self._pending_click.setSingleShot(True)
            self._pending_click.timeout.connect(self._toggle_pause)
            self._pending_click.start(230)
        elif e.button() == Qt.MiddleButton:
            self.toggle_fullscreen()
        elif e.button() == Qt.BackButton:
            self.step(-1)
        elif e.button() == Qt.ForwardButton:
            self.step(1)
        self._show_bars()

    def mouseDoubleClickEvent(self, e):
        if self._pending_click is not None:
            self._pending_click.stop()
            self._pending_click = None
        if e.button() != Qt.LeftButton:
            return
        if self._current_is_video():
            self.toggle_fullscreen()
        else:
            self.image_view.toggle_fit_actual()

    def keyPressEvent(self, e):  # noqa: C901 - a key map is inherently branchy
        k = e.key()
        mods = e.modifiers()
        is_video = self._current_is_video()

        if k == Qt.Key_Escape:
            if self.isFullScreen():
                self.showMaximized()
            else:
                self.close()
            return
        if k in (Qt.Key_F, Qt.Key_Return, Qt.Key_Enter):
            self.toggle_fullscreen()
            return
        if k == Qt.Key_Tab:
            self.toggle_panel()
            return
        if k in (Qt.Key_F1, Qt.Key_Question):
            self._show_help()
            return
        if k == Qt.Key_Delete and self.panel.isVisible():
            self.panel._remove_selected()
            return
        if k in (Qt.Key_PageDown,):
            self.step(1)
            return
        if k in (Qt.Key_PageUp,):
            self.step(-1)
            return
        if k == Qt.Key_Space or k == Qt.Key_K:
            if is_video:
                self._toggle_pause()
            else:
                self.step(1)
            return

        if is_video:
            if k == Qt.Key_Right:
                amount = SEEK_TINY if mods & Qt.ShiftModifier else (
                    SEEK_BIG if mods & Qt.ControlModifier else SEEK_SMALL
                )
                self._seek_relative(amount)
                self._show_bars()
                return
            if k == Qt.Key_Left:
                amount = SEEK_TINY if mods & Qt.ShiftModifier else (
                    SEEK_BIG if mods & Qt.ControlModifier else SEEK_SMALL
                )
                self._seek_relative(-amount)
                self._show_bars()
                return
            if k == Qt.Key_Up:
                self._adjust_volume(5)
                return
            if k == Qt.Key_Down:
                self._adjust_volume(-5)
                return
            if k == Qt.Key_Home:
                self._seek_absolute(0)
                return
            if k == Qt.Key_End:
                self._seek_absolute(max(0.0, self.video_view.duration - 3))
                return
            if k == Qt.Key_M:
                self._toggle_mute()
                return
            if k == Qt.Key_L:
                self.panel._cycle_loop()
                return
            if k == Qt.Key_V:
                self._toggle_sub_visible()
                return
            if k == Qt.Key_J:
                self.video_view.cycle_track("sub")
                return
            if k == Qt.Key_A:
                self.video_view.cycle_track("audio")
                return
            if k == Qt.Key_S:
                self._take_screenshot()
                return
            if k == Qt.Key_G:
                self._toggle_gif()
                return
            if k == Qt.Key_I:
                self._set_ab_point()
                return
            if k == Qt.Key_O:
                self._clear_ab_loop()
                return
            if k == Qt.Key_Period:
                self.video_view.frame_step()
                self._show_toast(t("viewer.next_frame"))
                return
            if k == Qt.Key_Comma:
                self.video_view.frame_step(back=True)
                self._show_toast(t("viewer.prev_frame"))
                return
            if k == Qt.Key_BracketLeft:
                self._nudge_speed(-1)
                return
            if k == Qt.Key_BracketRight:
                self._nudge_speed(1)
                return
            if k == Qt.Key_Backslash:
                self._set_speed(1.0)
                return
        else:
            if k == Qt.Key_Right or k == Qt.Key_Down:
                self.step(1)
                return
            if k == Qt.Key_Left or k == Qt.Key_Up:
                self.step(-1)
                return
            if k in (Qt.Key_Plus, Qt.Key_Equal):
                self.image_view.zoom_by_steps(2)
                return
            if k in (Qt.Key_Minus, Qt.Key_Underscore):
                self.image_view.zoom_by_steps(-2)
                return
            if k == Qt.Key_0:
                self.image_view.fit_to_window()
                return
            if k == Qt.Key_1:
                self.image_view.actual_size()
                return
            if k == Qt.Key_R:
                self.image_view.rotate(-90 if mods & Qt.ShiftModifier else 90)
                return

        if k == Qt.Key_Home:
            self.show_index(0)
            return
        if k == Qt.Key_End:
            self.show_index(len(self.items) - 1)
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------ lifecycle

    def _show_help(self) -> None:
        from .help_dialog import HelpDialog

        HelpDialog.show_for(self)

    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange:
            self._show_bars()
        super().changeEvent(e)

    def closeEvent(self, e):
        # 软件退出（shutdown 流程）且字幕模型在运行 → 询问是否关闭（弹窗）
        if self._shutdown and (self._live_on or self._live_paused):
            from PySide6.QtWidgets import QMessageBox

            from .config import settings as _settings

            if bool(_settings["live_caption_resident"]):
                box = QMessageBox(self)
                box.setWindowTitle(t("viewer.live_caption_quit_title"))
                box.setText(t("viewer.live_caption_quit_text"))
                box.setIcon(QMessageBox.Question)
                close_btn = box.addButton(
                    t("viewer.live_caption_quit_close"), QMessageBox.DestructiveRole)
                box.addButton(QMessageBox.Cancel)
                box.setDefaultButton(box.buttons()[0])
                box.exec()
                if box.clickedButton() is close_btn:
                    self._kill_live_proc()
        # 仅关闭播放界面：不弹窗、默认不杀模型（进程独立保活，重开秒出）
        self._stop_live_poll()
        self._live_on = False
        self._live_label.hide()
        self._remember_position()
        from .config import flush

        flush()
        if self._current_is_video():
            self.video_view.set_pause(True)
        self.video_view.stop()
        self.image_view.clear()
        self.previewer.set_media(None)
        super().closeEvent(e)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self._live_on:
            self._stop_live_caption()
        self._gif_timer.stop()
        self._gif_recording = False
        self._remember_position()
        self.previewer.stop()
        self.video_view.shutdown()
        self.close()


def _write_srt_file(path: Path, rows) -> None:
    """rows: [(start_s, end_s, orig, zh)] → 双语 SRT。"""

    def ts(x: float) -> str:
        h, r = divmod(int(x * 1000), 3600000)
        m, r = divmod(r, 60000)
        s, ms = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    parts = []
    for i, (st, en, orig, zh) in enumerate(rows, 1):
        parts.append(f"{i}\n{ts(st)} --> {ts(en)}\n{orig}")
        if zh:
            parts.append(zh)
        parts.append("")
    path.write_text("\n".join(parts), encoding="utf-8")
