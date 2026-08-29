"""Translucent overlay bars that sit on top of the video/image surface."""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from . import icons
from .config import settings
from .i18n import t
from .media import format_duration
from .mpv_widget import Track
from .seekbar import SeekBar

SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0]


class _EqPanel(QWidget):
    """Sliders for mpv's picture controls, embedded in the 画面 button's menu."""

    changed = Signal(str, int)   # (property name, -100..100)
    reset = Signal()

    ROWS = [
        ("brightness", t("controls.eq_brightness")),
        ("contrast", t("controls.eq_contrast")),
        ("saturation", t("controls.eq_saturation")),
        ("gamma", t("controls.eq_gamma")),
        ("hue", t("controls.eq_hue")),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(5)
        self.sliders: dict[str, QSlider] = {}
        self.vals: dict[str, QLabel] = {}
        for name, label in self.ROWS:
            r = QHBoxLayout()
            r.setSpacing(8)
            cap = QLabel(label)
            cap.setFixedWidth(44)
            r.addWidget(cap)
            s = QSlider(Qt.Horizontal)
            s.setRange(-100, 100)
            s.setFixedWidth(170)
            s.setFocusPolicy(Qt.NoFocus)
            val = QLabel("0")
            val.setFixedWidth(34)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            s.valueChanged.connect(
                lambda v, n=name, lab=val: (lab.setText(str(v)), self.changed.emit(n, v))
            )
            r.addWidget(s)
            r.addWidget(val)
            lay.addLayout(r)
            self.sliders[name] = s
            self.vals[name] = val
        btn = QPushButton(t("controls.eq_reset"))
        btn.setFocusPolicy(Qt.NoFocus)
        btn.clicked.connect(self._do_reset)
        lay.addWidget(btn)

    def _do_reset(self) -> None:
        for name, s in self.sliders.items():
            s.blockSignals(True)
            s.setValue(0)
            s.blockSignals(False)
            self.vals[name].setText("0")
        self.reset.emit()


def _overlay_button(text: str, tip: str = "", width: int = 34, icon: bool = False) -> QToolButton:
    b = QToolButton()
    b.setObjectName("OverlayIcon" if icon else "OverlayBtn")
    b.setText(text)
    if tip:
        b.setToolTip(tip)
    b.setFixedHeight(30)
    if width:
        b.setFixedWidth(width)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)  # keep keyboard shortcuts going to the viewer
    return b


class StageButton(QWidget):
    """A round translucent button drawn straight onto the video surface.

    Custom-painted rather than a styled QToolButton because it has to sit on top of the
    mpv render surface at an arbitrary size and stay legible over any frame, which means
    a real circular backdrop rather than a rectangle with rounded corners.
    """

    clicked = Signal()

    def __init__(self, glyph: str, tip: str = "", parent=None, plain: bool = False) -> None:
        super().__init__(parent)
        self.glyph = glyph
        self._plain = plain  # no circular backdrop, just the glyph itself
        self._hover = False
        self._down = False
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setMouseTracking(True)

    def set_glyph(self, glyph: str) -> None:
        if glyph != self.glyph:
            self.glyph = glyph
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        d = min(self.width(), self.height())
        box = QRect((self.width() - d) // 2, (self.height() - d) // 2, d, d)
        if self._down:
            alpha, fg = 190, "#ffffff"
        elif self._hover:
            alpha, fg = 160, "#ffffff"
        else:
            alpha, fg = 105, "#eef0f3"
        if not self._plain:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawEllipse(box)
        f = QFont(icons.FAMILY)
        # A plain glyph has no circle to fill, so it can take up more of the box.
        f.setPixelSize(max(11, int(d * (0.60 if self._plain else 0.40))))
        p.setFont(f)
        p.setPen(QColor(fg))
        # The play triangle is visually left-heavy inside a circle; nudge it back.
        shift = int(d * 0.035) if self.glyph == icons.PLAY else 0
        p.drawText(box.adjusted(shift, 0, shift, 0), Qt.AlignCenter, self.glyph)

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = self._down = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._down = True
            self.update()
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._down:
            self._down = False
            self.update()
            if self.rect().contains(e.position().toPoint()):
                self.clicked.emit()
            e.accept()

    def mouseDoubleClickEvent(self, e):
        # Swallow it: a double click here is two taps of this button, not a request to
        # go fullscreen, which is what the stage underneath would have made of it.
        e.accept()


class TopBar(QWidget):
    close_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ViewerOverlay")
        self.setFixedHeight(52)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 6, 10, 6)
        lay.setSpacing(10)

        self.title = QLabel("")
        self.title.setObjectName("OverlayTitle")
        lay.addWidget(self.title, 1)

        self.info = QLabel("")
        self.info.setObjectName("OverlayDim")
        lay.addWidget(self.info)

        self.counter = QLabel("")
        self.counter.setObjectName("OverlayDim")
        lay.addWidget(self.counter)

        btn_close = _overlay_button(icons.CLOSE, t("controls.close"), icon=True)
        btn_close.clicked.connect(self.close_clicked)
        lay.addWidget(btn_close)

    def paintEvent(self, e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0.0, QColor(0, 0, 0, 190))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g)


class ControlBar(QWidget):
    """Bottom bar. Video controls hide themselves when an image is displayed."""

    play_pause = Signal()
    prev_media = Signal()
    next_media = Signal()
    seek_requested = Signal(float)
    scrub_started = Signal()
    scrub_finished = Signal()
    speed_selected = Signal(float)
    hwdec_selected = Signal(str)
    screenshot_requested = Signal()
    gif_toggle_requested = Signal()
    video_eq_changed = Signal(str, int)
    video_eq_reset = Signal()
    volume_selected = Signal(int)
    mute_toggled = Signal()
    loop_cycle_requested = Signal()
    panel_toggled = Signal()
    fullscreen_toggled = Signal()
    sub_track_selected = Signal(object)
    audio_track_selected = Signal(object)
    sub_file_requested = Signal(str)
    sub_font_step = Signal(int)
    sub_delay_step = Signal(float)
    sub_visibility_toggled = Signal()
    zoom_step = Signal(int)
    zoom_fit = Signal()
    zoom_actual = Signal()
    rotate_requested = Signal(int)

    def __init__(self, previewer, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ViewerOverlay")
        self._tracks: list[Track] = []
        self._current_sid = None
        self._current_aid = None
        self._sub_visible = True
        self._speed = 1.0
        self._hwdec_mode = str(settings["hwdec"])
        self._is_video = True

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 4, 14, 12)
        root.setSpacing(3)

        self.seek = SeekBar(previewer)
        self.seek.seek_requested.connect(self.seek_requested)
        self.seek.scrub_started.connect(self.scrub_started)
        self.seek.scrub_finished.connect(self.scrub_finished)
        root.addWidget(self.seek)

        row = QHBoxLayout()
        row.setSpacing(6)
        root.addLayout(row)

        self.btn_play = _overlay_button(icons.PLAY, t("controls.play_pause"), icon=True)
        self.btn_play.clicked.connect(self.play_pause)
        row.addWidget(self.btn_play)

        self.btn_prev = _overlay_button(icons.PREVIOUS, t("controls.previous"), icon=True)
        self.btn_prev.clicked.connect(self.prev_media)
        row.addWidget(self.btn_prev)

        self.btn_next = _overlay_button(icons.NEXT, t("controls.next"), icon=True)
        self.btn_next.clicked.connect(self.next_media)
        row.addWidget(self.btn_next)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("OverlayMono")
        self.time_label.setMinimumWidth(120)
        row.addWidget(self.time_label)

        row.addStretch(1)

        # ---- image-only controls
        self.img_widgets: list[QWidget] = []
        for glyph, tip, sig in (
            (icons.ZOOM_OUT, t("controls.zoom_out"), lambda: self.zoom_step.emit(-2)),
            (icons.ZOOM_IN, t("controls.zoom_in"), lambda: self.zoom_step.emit(2)),
        ):
            b = _overlay_button(glyph, tip, icon=True)
            b.clicked.connect(sig)
            row.addWidget(b)
            self.img_widgets.append(b)

        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("OverlayMono")
        self.zoom_label.setMinimumWidth(52)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        row.addWidget(self.zoom_label)
        self.img_widgets.append(self.zoom_label)

        b = _overlay_button(icons.FIT_PAGE, t("controls.fit_actual"), icon=True)
        b.clicked.connect(self.zoom_fit)
        row.addWidget(b)
        self.img_widgets.append(b)

        b = _overlay_button(icons.ROTATE, t("controls.rotate"), icon=True)
        b.clicked.connect(lambda: self.rotate_requested.emit(90))
        row.addWidget(b)
        self.img_widgets.append(b)

        # ---- video-only controls
        self.vid_widgets: list[QWidget] = []

        self.btn_speed = _overlay_button("1.0×", t("controls.speed_tip"), 56)
        self.btn_speed.setPopupMode(QToolButton.InstantPopup)
        self.btn_speed.setMenu(self._build_speed_menu())
        self.btn_speed.menu().setTitle(t("controls.menu_speed"))
        row.addWidget(self.btn_speed)
        self.vid_widgets.append(self.btn_speed)

        self.btn_hwdec = _overlay_button(t("controls.hwdec_btn"), t("controls.hwdec_tip"), 44)
        self.btn_hwdec.setPopupMode(QToolButton.InstantPopup)
        self.btn_hwdec.setMenu(self._build_hwdec_menu())
        self.btn_hwdec.menu().setTitle(t("controls.menu_hwdec"))
        row.addWidget(self.btn_hwdec)
        self.vid_widgets.append(self.btn_hwdec)

        self.btn_sub = _overlay_button(t("controls.sub_btn"), t("controls.sub_tip"), 44)
        self.btn_sub.setPopupMode(QToolButton.InstantPopup)
        self.btn_sub.setMenu(QMenu(self))
        self.btn_sub.menu().setTitle(t("controls.menu_sub"))
        self.btn_sub.menu().aboutToShow.connect(self._fill_sub_menu)
        row.addWidget(self.btn_sub)
        self.vid_widgets.append(self.btn_sub)

        self.btn_audio = _overlay_button(t("controls.audio_btn"), t("controls.audio_tip"), 44)
        self.btn_audio.setPopupMode(QToolButton.InstantPopup)
        self.btn_audio.setMenu(QMenu(self))
        self.btn_audio.menu().setTitle(t("controls.menu_audio"))
        self.btn_audio.menu().aboutToShow.connect(self._fill_audio_menu)
        row.addWidget(self.btn_audio)
        self.vid_widgets.append(self.btn_audio)

        self.btn_shot = _overlay_button(t("controls.screenshot_btn"), t("controls.screenshot_tip"), 44)
        self.btn_shot.clicked.connect(self.screenshot_requested)
        row.addWidget(self.btn_shot)
        self.vid_widgets.append(self.btn_shot)

        self.btn_gif = _overlay_button("GIF", t("controls.gif_tip"), 44)
        self.btn_gif.setCheckable(True)
        self.btn_gif.clicked.connect(self.gif_toggle_requested)
        row.addWidget(self.btn_gif)
        self.vid_widgets.append(self.btn_gif)

        self.btn_eq = _overlay_button(t("controls.eq_btn"), t("controls.eq_tip"), 44)
        self.btn_eq.setPopupMode(QToolButton.InstantPopup)
        self.btn_eq.setMenu(self._build_eq_menu())
        self.btn_eq.menu().setTitle(t("controls.menu_eq"))
        row.addWidget(self.btn_eq)
        self.vid_widgets.append(self.btn_eq)

        self.btn_loop = _overlay_button(icons.REPEAT_ALL, t("controls.loop_tip"), icon=True)
        self.btn_loop.clicked.connect(self.loop_cycle_requested)
        row.addWidget(self.btn_loop)
        self.vid_widgets.append(self.btn_loop)

        self.btn_mute = _overlay_button(icons.VOLUME, t("controls.mute_tip"), icon=True)
        self.btn_mute.clicked.connect(self.mute_toggled)
        row.addWidget(self.btn_mute)
        self.vid_widgets.append(self.btn_mute)

        self.vol = QSlider(Qt.Horizontal)
        self.vol.setObjectName("OverlaySlider")
        self.vol.setRange(0, 130)
        self.vol.setFixedWidth(96)
        self.vol.setToolTip(t("controls.volume_tip"))
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.vol.valueChanged.connect(self.volume_selected)
        row.addWidget(self.vol)
        self.vid_widgets.append(self.vol)

        self.btn_panel = _overlay_button(icons.PLAYLIST, t("controls.panel_tip"), icon=True)
        self.btn_panel.setCheckable(True)
        self.btn_panel.clicked.connect(self.panel_toggled)
        row.addWidget(self.btn_panel)

        self.btn_full = _overlay_button(icons.FULLSCREEN, t("controls.fullscreen_tip"), icon=True)
        self.btn_full.clicked.connect(self.fullscreen_toggled)
        row.addWidget(self.btn_full)

        # ---- responsive "more" overflow: on narrow windows the less-used
        #      video buttons fold into this menu, from the least important first.
        self._row = row
        self._more_btn = _overlay_button(chr(0xE712), t("controls.more"), icon=True)
        self._more_btn.setPopupMode(QToolButton.InstantPopup)
        self._more_menu = QMenu(self)
        self._more_menu.aboutToShow.connect(self._rebuild_more_menu)
        self._more_btn.setMenu(self._more_menu)
        self._more_btn.hide()
        row.addWidget(self._more_btn)

        # (widget, menu label) — fold order: earlier = folded away first.
        self._flex: list[tuple[QToolButton, str]] = [
            (self.btn_eq, t("controls.menu_eq")),
            (self.btn_gif, t("controls.menu_gif")),
            (self.btn_shot, t("controls.menu_shot")),
            (self.btn_speed, t("controls.menu_speed")),
            (self.btn_hwdec, t("controls.menu_hwdec")),
            (self.btn_sub, t("controls.menu_sub")),
            (self.btn_audio, t("controls.menu_audio")),
            (self.btn_loop, t("controls.menu_loop")),
            (self.btn_mute, t("controls.menu_mute")),
            (self.btn_panel, t("controls.menu_panel")),
            (self.btn_full, t("controls.menu_full")),
        ]
        self._update_flex()

    # ------------------------------------------------------------- menus

    def _build_speed_menu(self) -> QMenu:
        menu = QMenu(self)
        self._speed_group = QActionGroup(menu)
        self._speed_group.setExclusive(True)
        self._speed_actions: dict[float, QAction] = {}
        for s in SPEEDS:
            act = QAction(f"{s:g}×", menu)
            act.setCheckable(True)
            act.triggered.connect(lambda _c=False, v=s: self.speed_selected.emit(v))
            self._speed_group.addAction(act)
            menu.addAction(act)
            self._speed_actions[s] = act
        return menu

    # mpv hwdec modes we expose; the rest of mpv's options ('auto-copy', 'vdpau', ...)
    # are niche enough that a user who needs them can edit config.json by hand.
    HWDEC_MODES = [
        ("auto-safe", t("controls.hwdec_auto_safe"), t("controls.hwdec_auto_safe_tip")),
        ("auto", t("controls.hwdec_auto"), t("controls.hwdec_auto_tip")),
        ("no", t("controls.hwdec_no"), t("controls.hwdec_no_tip")),
    ]

    def _build_hwdec_menu(self) -> QMenu:
        menu = QMenu(self)
        self._hwdec_group = QActionGroup(menu)
        self._hwdec_group.setExclusive(True)
        self._hwdec_actions: dict[str, QAction] = {}
        for mode, label, tip in self.HWDEC_MODES:
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setToolTip(tip)
            act.triggered.connect(lambda _c=False, m=mode: self.hwdec_selected.emit(m))
            self._hwdec_group.addAction(act)
            menu.addAction(act)
            self._hwdec_actions[mode] = act
        return menu

    def _build_eq_menu(self) -> QMenu:
        menu = QMenu(self)
        self._eq_panel = _EqPanel()
        self._eq_panel.changed.connect(self.video_eq_changed)
        self._eq_panel.reset.connect(self.video_eq_reset)
        action = QWidgetAction(menu)
        action.setDefaultWidget(self._eq_panel)
        menu.addAction(action)
        return menu

    def set_hwdec_mode(self, mode: str) -> None:
        """Reflect the active hwdec mode on the button label + menu check."""
        self._hwdec_mode = mode
        settings["hwdec"] = mode
        # Button label: short tag that fits in the 44px overlay button.
        label = {
            "auto-safe": t("controls.hwdec_short_auto_safe"),
            "auto": t("controls.hwdec_short_auto"),
            "no": t("controls.hwdec_short_no"),
        }.get(mode, t("controls.hwdec_short_auto_safe"))
        self.btn_hwdec.setText(label)
        for m, act in self._hwdec_actions.items():
            act.setChecked(m == mode)

    def set_gif_recording(self, on: bool) -> None:
        """Show the GIF button as armed (red REC) while a recording is running."""
        self.btn_gif.setChecked(on)
        self.btn_gif.setText("● REC" if on else "GIF")

    def _fill_sub_menu(self) -> None:
        menu = self.btn_sub.menu()
        menu.clear()
        act = QAction(t("controls.sub_visible"), menu)
        act.setCheckable(True)
        act.setChecked(self._sub_visible)
        act.triggered.connect(self.sub_visibility_toggled)
        menu.addAction(act)
        menu.addSeparator()

        subs = [t for t in self._tracks if t.kind == "sub"]
        none_act = QAction(t("controls.none"), menu)
        none_act.setCheckable(True)
        none_act.setChecked(self._current_sid in (None, False, "no"))
        none_act.triggered.connect(lambda: self.sub_track_selected.emit("no"))
        menu.addAction(none_act)
        for t in subs:
            a = QAction(t.label(), menu)
            a.setCheckable(True)
            a.setChecked(t.id == self._current_sid)
            a.triggered.connect(lambda _c=False, tid=t.id: self.sub_track_selected.emit(tid))
            menu.addAction(a)
        if not subs:
            placeholder = QAction(t("controls.no_embedded_subs"), menu)
            placeholder.setEnabled(False)
            menu.addAction(placeholder)

        menu.addSeparator()
        a = QAction(t("controls.load_external_sub"), menu)
        a.triggered.connect(self._pick_sub_file)
        menu.addAction(a)
        menu.addSeparator()
        for label, step in ((t("controls.sub_font_up"), 4), (t("controls.sub_font_down"), -4)):
            a = QAction(label, menu)
            a.triggered.connect(lambda _c=False, s=step: self.sub_font_step.emit(s))
            menu.addAction(a)
        for label, delta in (
            (t("controls.sub_delay_plus"), 0.1),
            (t("controls.sub_delay_minus"), -0.1),
            (t("controls.sub_delay_reset"), 0.0),
        ):
            a = QAction(label, menu)
            a.triggered.connect(lambda _c=False, d=delta: self.sub_delay_step.emit(d))
            menu.addAction(a)

    def _fill_audio_menu(self) -> None:
        menu = self.btn_audio.menu()
        menu.clear()
        auds = [t for t in self._tracks if t.kind == "audio"]
        if not auds:
            a = QAction(t("controls.no_audio_tracks"), menu)
            a.setEnabled(False)
            menu.addAction(a)
            return
        for t in auds:
            a = QAction(f"{t.label()}  ·  {t.codec}", menu)
            a.setCheckable(True)
            a.setChecked(t.id == self._current_aid)
            a.triggered.connect(lambda _c=False, tid=t.id: self.audio_track_selected.emit(tid))
            menu.addAction(a)

    def _pick_sub_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, t("controls.pick_sub_title"), "", t("controls.sub_file_filter")
        )
        if path:
            self.sub_file_requested.emit(path)

    # -------------------------------------------------------------- state

    def mousePressEvent(self, e) -> None:
        # Clicks on the control bar's blank area must never reach the stage
        # underneath (which would pause the video / toggle fullscreen).
        e.accept()

    def mouseReleaseEvent(self, e) -> None:
        e.accept()

    def mouseDoubleClickEvent(self, e) -> None:
        e.accept()

    # -------------------------------------------- responsive overflow

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._update_flex()

    def _row_width(self) -> int:
        total = 0
        n = 0
        for i in range(self._row.count()):
            item = self._row.itemAt(i)
            w = item.widget()
            # isHidden() rather than isVisible(): the latter is False whenever the
            # parent chain is not mapped (offscreen tests, hidden overlay), which
            # would make every button invisible and the row "empty".
            if w is not None and not w.isHidden():
                total += w.sizeHint().width()
                n += 1
        return total + self._row.spacing() * max(0, n - 1)

    def _update_flex(self) -> None:
        """Fold the least-used video buttons into the "more" menu when narrow."""
        if not hasattr(self, "_flex"):
            return
        # start fully expanded
        for w, _ in self._flex:
            w.show()
        self._more_btn.hide()
        # reserve room for the more button itself (icon ~30px) + margins
        avail = self.width() - 28 - 34
        if self._row_width() <= avail:
            return
        for w, _ in self._flex:
            if self._row_width() <= avail:
                break
            w.hide()
        hidden = [w for w, _ in self._flex if w.isHidden()]
        self._more_btn.setVisible(bool(hidden))

    def _rebuild_more_menu(self) -> None:
        self._more_menu.clear()
        for w, label in self._flex:
            if not w.isHidden():
                continue
            if w.menu() is not None:
                # buttons that open their own menu become submenus here,
                # keeping their dynamic fill logic (subs / audio tracks, ...)
                self._more_menu.addMenu(w.menu())
            else:
                act = self._more_menu.addAction(label)
                act.triggered.connect(lambda _=False, ww=w: self._flex_trigger(ww))

    def _flex_trigger(self, w: QToolButton) -> None:
        w.click()

    def set_media_kind(self, is_video: bool) -> None:
        self._is_video = is_video
        for w in self.vid_widgets:
            w.setVisible(is_video)
        for w in self.img_widgets:
            w.setVisible(not is_video)
        self.btn_play.setVisible(is_video)
        self.seek.setVisible(is_video)
        self.seek.set_active(is_video)
        self.time_label.setVisible(is_video)
        self._update_flex()

    def set_playing(self, playing: bool) -> None:
        self.btn_play.setText(icons.PAUSE if playing else icons.PLAY)

    def set_position(self, pos: float, dur: float) -> None:
        self.seek.set_position(pos)
        self.time_label.setText(f"{format_duration(pos)} / {format_duration(dur)}")

    def set_duration(self, dur: float) -> None:
        self.seek.set_duration(dur)

    def set_cache_end(self, t: float) -> None:
        self.seek.set_cache_end(t)

    def set_caption_ranges(self, ranges: list[tuple[float, float]]) -> None:
        self.seek.set_caption_ranges(ranges)

    def set_ab_range(self, ab_range: tuple[float, float] | None) -> None:
        self.seek.set_ab_range(ab_range)

    def set_speed(self, speed: float) -> None:
        self._speed = speed
        self.btn_speed.setText(f"{speed:g}×")
        for s, act in self._speed_actions.items():
            act.setChecked(abs(s - speed) < 1e-6)

    def set_volume(self, vol: int, muted: bool) -> None:
        self.vol.blockSignals(True)
        self.vol.setValue(int(vol))
        self.vol.blockSignals(False)
        self.btn_mute.setText(
            icons.MUTE if muted or vol == 0 else (icons.VOLUME_LOW if vol < 55 else icons.VOLUME)
        )

    def set_tracks(self, tracks: list[Track], sid, aid) -> None:
        self._tracks = tracks
        self._current_sid = sid
        self._current_aid = aid
        subs = sum(1 for t in tracks if t.kind == "sub")
        auds = sum(1 for t in tracks if t.kind == "audio")
        self.btn_sub.setText(t("controls.sub_btn") if subs <= 1 else t("controls.sub_count").format(count=subs))
        self.btn_audio.setText(t("controls.audio_btn") if auds <= 1 else t("controls.audio_count").format(count=auds))
        self.btn_sub.setEnabled(True)
        self.btn_audio.setEnabled(auds > 0)

    def set_sub_visible(self, visible: bool) -> None:
        self._sub_visible = visible

    def set_zoom(self, scale: float) -> None:
        self.zoom_label.setText(f"{scale * 100:.0f}%")

    def set_loop_mode(self, mode: str) -> None:
        glyphs = {
            "off": icons.REPEAT_ALL,
            "list": icons.REPEAT_ALL,
            "one": icons.REPEAT_ONE,
            "shuffle": icons.SHUFFLE,
        }
        labels = {
            "off": t("controls.loop_off"),
            "list": t("controls.loop_list"),
            "one": t("controls.loop_one"),
            "shuffle": t("controls.loop_shuffle"),
        }
        self.btn_loop.setText(glyphs.get(mode, icons.REPEAT_ALL))
        self.btn_loop.setToolTip(t("controls.loop_tooltip").format(mode=labels.get(mode, t("controls.loop_off"))))
        self.btn_loop.setCheckable(True)
        self.btn_loop.setChecked(mode != "off")

    def set_panel_open(self, on: bool) -> None:
        self.btn_panel.setChecked(on)

    def set_fullscreen(self, on: bool) -> None:
        self.btn_full.setText(icons.FULLSCREEN_EXIT if on else icons.FULLSCREEN)
        self.btn_full.setToolTip(t("controls.exit_fullscreen_tip") if on else t("controls.fullscreen_tip"))

    def set_navigation(self, has_prev: bool, has_next: bool) -> None:
        self.btn_prev.setEnabled(has_prev)
        self.btn_next.setEnabled(has_next)

    def paintEvent(self, e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0.0, QColor(0, 0, 0, 0))
        g.setColorAt(0.35, QColor(0, 0, 0, 170))
        g.setColorAt(1.0, QColor(0, 0, 0, 225))
        p.fillRect(self.rect(), g)
