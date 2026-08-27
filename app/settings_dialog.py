"""Central settings window: gathers the toggles that were previously JSON-only.

Everything here writes straight into `settings` and is flushed to disk when the
dialog closes. A few options (default decode mode, subtitle size, default volume)
are read when a video is first opened, so they take effect from the next video /
next launch rather than mid-playback -- the footer says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import assoc, theme
from .config import flush, settings
from .i18n import LANGUAGES, current_language, set_language, t
from .runtime import APP_DIR

_HWDEC_CHOICES = [
    ("auto-safe", "settings.hwdec_auto_safe"),
    ("auto", "settings.hwdec_auto"),
    ("no", "settings.hwdec_no"),
]


def _section(title: str) -> QLabel:
    lab = QLabel(title)
    lab.setObjectName("SettingsSection")
    lab.setStyleSheet(
        f"color:{theme.ACCENT}; font-size:14px; font-weight:bold;"
        f" border-bottom:1px solid {theme.BORDER}; padding:10px 0 4px 0; margin-top:6px;"
    )
    return lab


def _row(label: str, widget: QWidget, hint: str = "") -> QWidget:
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 3, 0, 3)
    lay.setSpacing(10)
    cap = QLabel(label)
    cap.setMinimumWidth(150)
    lay.addWidget(cap)
    lay.addWidget(widget)
    if hint:
        h = QLabel(hint)
        h.setStyleSheet(f"color:{theme.TEXT_DIM};")
        lay.addWidget(h, 1)
    else:
        lay.addStretch(1)
    return wrap


class SettingsDialog(QDialog):
    _instance: "SettingsDialog | None" = None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("settings.title"))
        self.setMinimumWidth(520)
        self.setStyleSheet(
            f"QDialog {{ background:{theme.BG_BASE}; }}"
            f"QLabel {{ color:{theme.TEXT}; }}"
            f"QCheckBox {{ color:{theme.TEXT}; }}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(2)

        # ---- 界面语言
        root.addWidget(_section(t("settings.section_lang")))
        self.combo_lang = QComboBox()
        for code, label in LANGUAGES:
            self.combo_lang.addItem(label, code)
        idx = self.combo_lang.findData(current_language())
        self.combo_lang.setCurrentIndex(max(0, idx))
        self.combo_lang.currentIndexChanged.connect(self._on_lang_changed)
        root.addWidget(_row(t("settings.lang_label"), self.combo_lang))
        lang_hint = QLabel(t("settings.lang_restart_hint"))
        lang_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        root.addWidget(lang_hint)

        # ---- 播放
        root.addWidget(_section(t("settings.section_play")))
        self.cb_resume = QCheckBox(t("settings.resume_label"))
        self.cb_resume.setChecked(bool(settings["resume_enabled"]))
        self.cb_resume.toggled.connect(lambda v: self._set("resume_enabled", v))
        root.addWidget(self.cb_resume)

        self.cb_autoplay = QCheckBox(t("settings.autoplay_label"))
        self.cb_autoplay.setChecked(bool(settings["autoplay_next"]))
        self.cb_autoplay.toggled.connect(lambda v: self._set("autoplay_next", v))
        root.addWidget(self.cb_autoplay)

        self.cb_native = QCheckBox(t("settings.native_size_label"))
        self.cb_native.setChecked(bool(settings["open_native_size"]))
        self.cb_native.toggled.connect(lambda v: self._set("open_native_size", v))
        root.addWidget(self.cb_native)

        self.cb_scroll = QCheckBox(t("settings.remember_scroll_label"))
        self.cb_scroll.setChecked(bool(settings["remember_scroll"]))
        self.cb_scroll.toggled.connect(lambda v: self._set("remember_scroll", v))
        root.addWidget(self.cb_scroll)

        self.combo_hwdec = QComboBox()
        for value, key in _HWDEC_CHOICES:
            self.combo_hwdec.addItem(t(key), value)
        cur = self.combo_hwdec.findData(str(settings["hwdec"]))
        self.combo_hwdec.setCurrentIndex(max(0, cur))
        self.combo_hwdec.currentIndexChanged.connect(
            lambda _=0: self._set("hwdec", self.combo_hwdec.currentData())
        )
        root.addWidget(_row(t("settings.hwdec_label"), self.combo_hwdec, t("settings.next_video_hint")))

        # ---- 音量 / 字幕
        root.addWidget(_section(t("settings.section_volume")))
        self.sl_volume = QSlider(Qt.Horizontal)
        self.sl_volume.setRange(0, 130)
        self.sl_volume.setFixedWidth(180)
        self.sl_volume.setValue(int(settings["volume"]))
        self.lab_volume = QLabel(t("settings.volume_pct").format(v=int(settings['volume'])))
        self.sl_volume.valueChanged.connect(self._on_volume)
        vol_box = QWidget()
        vb = QHBoxLayout(vol_box)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.addWidget(self.sl_volume)
        vb.addWidget(self.lab_volume)
        vb.addStretch(1)
        root.addWidget(_row(t("settings.volume_label"), vol_box, t("settings.next_launch_hint")))

        self.sp_subsize = QSpinBox()
        self.sp_subsize.setRange(16, 96)
        self.sp_subsize.setValue(int(settings["sub_font_size"]))
        self.sp_subsize.valueChanged.connect(lambda v: self._set("sub_font_size", v))
        root.addWidget(_row(t("settings.subsize_label"), self.sp_subsize, t("settings.next_video_hint")))

        self.cb_subvis = QCheckBox(t("settings.sub_visible_label"))
        self.cb_subvis.setChecked(bool(settings["sub_visible"]))
        self.cb_subvis.toggled.connect(lambda v: self._set("sub_visible", v))
        root.addWidget(self.cb_subvis)

        # ---- 截图 / GIF
        root.addWidget(_section(t("settings.section_capture")))
        self.sp_fps = QSpinBox()
        self.sp_fps.setRange(2, 30)
        self.sp_fps.setValue(int(settings["gif_fps"]))
        self.sp_fps.valueChanged.connect(lambda v: self._set("gif_fps", v))
        root.addWidget(_row(t("settings.gif_fps_label"), self.sp_fps, t("settings.fps_unit")))

        self.sp_secs = QSpinBox()
        self.sp_secs.setRange(1, 120)
        self.sp_secs.setValue(int(settings["gif_max_seconds"]))
        self.sp_secs.valueChanged.connect(lambda v: self._set("gif_max_seconds", v))
        root.addWidget(_row(t("settings.gif_secs_label"), self.sp_secs, t("settings.secs_hint")))

        self.sp_width = QSpinBox()
        self.sp_width.setRange(120, 1920)
        self.sp_width.setSingleStep(40)
        self.sp_width.setValue(int(settings["gif_max_width"]))
        self.sp_width.valueChanged.connect(lambda v: self._set("gif_max_width", v))
        root.addWidget(_row(t("settings.gif_width_label"), self.sp_width, t("settings.pixels_unit")))

        # ---- 截图保存路径
        shot_box = QWidget()
        sb = QHBoxLayout(shot_box)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(6)
        self.edit_shot_path = QLineEdit()
        self.edit_shot_path.setReadOnly(True)
        self.edit_shot_path.setPlaceholderText(t("settings.shot_path_placeholder"))
        self.edit_shot_path.setStyleSheet(
            f"QLineEdit {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:4px; padding:3px 6px; }}"
        )
        custom = str(settings["capture_path"] or "").strip()
        if custom:
            self.edit_shot_path.setText(custom)
        sb.addWidget(self.edit_shot_path, 1)
        btn_browse = QPushButton(t("settings.browse_ellipsis"))
        btn_browse.setFocusPolicy(Qt.NoFocus)
        btn_browse.clicked.connect(self._browse_shot_path)
        sb.addWidget(btn_browse)
        btn_open = QPushButton(t("settings.open"))
        btn_open.setFocusPolicy(Qt.NoFocus)
        btn_open.clicked.connect(self._open_shot_folder)
        sb.addWidget(btn_open)
        btn_clear = QPushButton(t("settings.clear"))
        btn_clear.setFocusPolicy(Qt.NoFocus)
        btn_clear.clicked.connect(lambda: (self.edit_shot_path.clear(), self._set("capture_path", "")))
        sb.addWidget(btn_clear)
        root.addWidget(_row(t("settings.shot_path_label"), shot_box, t("settings.shot_path_hint")))

        # ---- 压缩包解压缓存路径
        arch_box = QWidget()
        ab = QHBoxLayout(arch_box)
        ab.setContentsMargins(0, 0, 0, 0)
        ab.setSpacing(6)
        self.edit_archive_path = QLineEdit()
        self.edit_archive_path.setReadOnly(True)
        self.edit_archive_path.setPlaceholderText(t("settings.archive_path_placeholder"))
        self.edit_archive_path.setStyleSheet(
            f"QLineEdit {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:4px; padding:3px 6px; }}"
        )
        custom_arch = str(settings["archive_cache"] or "").strip()
        if custom_arch:
            self.edit_archive_path.setText(custom_arch)
        ab.addWidget(self.edit_archive_path, 1)
        btn_ab = QPushButton(t("settings.browse_ellipsis"))
        btn_ab.setFocusPolicy(Qt.NoFocus)
        btn_ab.clicked.connect(self._browse_archive_path)
        ab.addWidget(btn_ab)
        btn_ac = QPushButton(t("settings.clear"))
        btn_ac.setFocusPolicy(Qt.NoFocus)
        btn_ac.clicked.connect(lambda: (self.edit_archive_path.clear(), self._set("archive_cache", "")))
        ab.addWidget(btn_ac)
        root.addWidget(_row(t("settings.archive_path_label"), arch_box, t("settings.archive_path_hint")))

        self.cb_archive_no_thumbs = QCheckBox(t("settings.archive_no_thumbs_label"))
        self.cb_archive_no_thumbs.setChecked(bool(settings["archive_no_thumbs"]))
        self.cb_archive_no_thumbs.toggled.connect(lambda v: self._set("archive_no_thumbs", v))
        root.addWidget(self.cb_archive_no_thumbs)
        no_thumbs_hint = QLabel(t("settings.archive_no_thumbs_hint"))
        no_thumbs_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        root.addWidget(no_thumbs_hint)

        # ---- 字幕引擎（live-subtitle）
        root.addWidget(_section(t("settings.section_subtitles")))
        sub_engine_box = QWidget()
        se = QHBoxLayout(sub_engine_box)
        se.setContentsMargins(0, 0, 0, 0)
        se.setSpacing(6)
        self.edit_subtitle_dir = QLineEdit()
        self.edit_subtitle_dir.setReadOnly(True)
        self.edit_subtitle_dir.setPlaceholderText(t("settings.subtitle_dir_placeholder"))
        self.edit_subtitle_dir.setStyleSheet(
            f"QLineEdit {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:4px; padding:3px 6px; }}"
        )
        custom_dir = str(settings["subtitle_pipeline_dir"] or "").strip()
        self.edit_subtitle_dir.setText(custom_dir)
        se.addWidget(self.edit_subtitle_dir, 1)
        btn_sb = QPushButton(t("settings.browse_ellipsis"))
        btn_sb.setFocusPolicy(Qt.NoFocus)
        btn_sb.clicked.connect(self._browse_subtitle_dir)
        se.addWidget(btn_sb)
        btn_sc = QPushButton(t("settings.detect"))
        btn_sc.setFocusPolicy(Qt.NoFocus)
        btn_sc.clicked.connect(self._detect_subtitle_dir)
        se.addWidget(btn_sc)
        root.addWidget(_row(t("settings.subtitle_dir_label"), sub_engine_box))
        sub_status_hint = QLabel(t("settings.subtitle_dir_hint"))
        sub_status_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        sub_status_hint.setWordWrap(True)
        root.addWidget(sub_status_hint)
        self.sub_status = QLabel("")
        self.sub_status.setStyleSheet(f"color:{theme.TEXT_DIM};")
        root.addWidget(self.sub_status)
        self._refresh_subtitle_status()

        # ---- 文件关联
        root.addWidget(_section(t("settings.section_assoc")))
        assoc_box = QWidget()
        ab = QHBoxLayout(assoc_box)
        ab.setContentsMargins(0, 0, 0, 0)
        ab.setSpacing(8)
        self.btn_assoc = QPushButton(t("settings.assoc_register"))
        self.btn_assoc.setFocusPolicy(Qt.NoFocus)
        self.btn_assoc.clicked.connect(self._register_assoc)
        ab.addWidget(self.btn_assoc)
        self.btn_unassoc = QPushButton(t("settings.assoc_unregister"))
        self.btn_unassoc.setFocusPolicy(Qt.NoFocus)
        self.btn_unassoc.clicked.connect(self._unregister_assoc)
        ab.addWidget(self.btn_unassoc)
        ab.addStretch(1)
        root.addWidget(assoc_box)
        self.assoc_hint = QLabel("")
        self.assoc_hint.setWordWrap(True)
        self.assoc_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        root.addWidget(self.assoc_hint)
        self._refresh_assoc_hint()

        # ---- footer
        root.addSpacing(8)
        note = QLabel(t("settings.footer_note"))
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:12px;")
        root.addWidget(note)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_close = QPushButton(t("settings.done"))
        btn_close.setFocusPolicy(Qt.NoFocus)
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        root.addLayout(footer)

    # ------------------------------------------------------------- helpers

    def _set(self, key: str, value) -> None:
        settings[key] = value

    def _on_lang_changed(self, _index: int) -> None:
        code = self.combo_lang.currentData()
        if not code or str(code) == current_language():
            return
        set_language(str(code))
        box = QMessageBox(self)
        box.setWindowTitle(t("settings.section_lang"))
        box.setText(t("settings.lang_restart_confirm"))
        box.setIcon(QMessageBox.Question)
        now = box.addButton(t("settings.restart_now"), QMessageBox.AcceptRole)
        box.addButton(t("settings.restart_later"), QMessageBox.RejectRole)
        box.setDefaultButton(now)
        box.exec()
        if box.clickedButton() is now:
            flush()
            self._restart_app()

    @staticmethod
    def _restart_app() -> None:
        """Restart the whole app so the new language takes effect everywhere."""
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication

        if getattr(sys, "frozen", False):  # packaged exe: just re-launch it
            QProcess.startDetached(sys.executable, [])
        else:  # source tree: relaunch via the entry script
            entry = str(Path(__file__).resolve().parents[1] / "main.py")
            QProcess.startDetached(sys.executable, [entry])
        QApplication.quit()

    def _on_volume(self, v: int) -> None:
        self.lab_volume.setText(t("settings.volume_pct").format(v=v))
        settings["volume"] = int(v)

    def _browse_shot_path(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        start = self.edit_shot_path.text() or str(APP_DIR)
        d = QFileDialog.getExistingDirectory(self, t("settings.pick_shot_dir"), start)
        if d:
            self.edit_shot_path.setText(d)
            self._set("capture_path", d)

    def _browse_subtitle_dir(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        start = self.edit_subtitle_dir.text() or str(APP_DIR)
        d = QFileDialog.getExistingDirectory(self, t("settings.pick_subtitle_dir"), start)
        if d:
            self.edit_subtitle_dir.setText(d)
            self._set("subtitle_pipeline_dir", d)
            self._refresh_subtitle_status()

    def _detect_subtitle_dir(self) -> None:
        from .config import find_subtitle_pipeline_dir

        found = find_subtitle_pipeline_dir()
        if found is not None:
            self.edit_subtitle_dir.setText(str(found))
            self._set("subtitle_pipeline_dir", str(found))
        self._refresh_subtitle_status()

    def _refresh_subtitle_status(self) -> None:
        from .config import find_subtitle_pipeline_dir

        found = find_subtitle_pipeline_dir()
        if found is not None:
            self.sub_status.setText(t("settings.subtitle_dir_ready").format(dir=str(found)))
            self.sub_status.setStyleSheet("color:#5dc0f0;")
        else:
            self.sub_status.setText(t("settings.subtitle_dir_missing"))
            self.sub_status.setStyleSheet("color:#e0653f;")

    def _browse_archive_path(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        start = self.edit_archive_path.text() or str(APP_DIR)
        d = QFileDialog.getExistingDirectory(self, t("settings.pick_archive_dir"), start)
        if d:
            self.edit_archive_path.setText(d)
            self._set("archive_cache", d)

    def _open_shot_folder(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        custom = self.edit_shot_path.text().strip()
        if custom:
            folder = Path(custom)
        else:
            folder = APP_DIR / "截图"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _register_assoc(self) -> None:
        try:
            assoc.register()
            self.assoc_hint.setText(t("settings.assoc_registered_hint"))
        except Exception as exc:  # pragma: no cover - registry edge cases
            self.assoc_hint.setText(t("settings.assoc_register_failed").format(err=exc))

    def _unregister_assoc(self) -> None:
        try:
            assoc.unregister()
            self.assoc_hint.setText(t("settings.assoc_unregistered_hint"))
        except Exception as exc:  # pragma: no cover
            self.assoc_hint.setText(t("settings.assoc_unregister_failed").format(err=exc))

    def _refresh_assoc_hint(self) -> None:
        if not assoc.is_supported():
            self.btn_assoc.setEnabled(False)
            self.btn_unassoc.setEnabled(False)
            self.assoc_hint.setText(t("settings.assoc_windows_only"))
            return
        if assoc.is_registered():
            self.assoc_hint.setText(t("settings.assoc_status_registered"))
        else:
            self.assoc_hint.setText(t("settings.assoc_status_unregistered"))

    def closeEvent(self, e):  # noqa: ANN001
        flush()
        super().closeEvent(e)

    def done(self, r):  # noqa: ANN001 - covers both accept() and Esc
        flush()
        super().done(r)

    @classmethod
    def show_for(cls, parent=None) -> "SettingsDialog":
        dlg = cls._instance
        if dlg is None:
            dlg = cls(parent)
            cls._instance = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg