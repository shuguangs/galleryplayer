"""Central settings window: gathers the toggles that were previously JSON-only.

Everything here writes straight into `settings` and is flushed to disk when the
dialog closes. A few options (default decode mode, subtitle size, default volume)
are read when a video is first opened, so they take effect from the next video /
next launch rather than mid-playback -- the footer says so.
"""
from __future__ import annotations

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
from .runtime import APP_DIR

_HWDEC_CHOICES = [
    ("auto-safe", "自动硬解（默认，最稳）"),
    ("auto", "强制硬解（更省电，个别文件可能黑屏）"),
    ("no", "纯软解（最兼容，吃 CPU）"),
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
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        self.setStyleSheet(
            f"QDialog {{ background:{theme.BG_BASE}; }}"
            f"QLabel {{ color:{theme.TEXT}; }}"
            f"QCheckBox {{ color:{theme.TEXT}; }}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(2)

        # ---- 播放
        root.addWidget(_section("播放"))
        self.cb_resume = QCheckBox("断点续播（关掉后每次都从头播放）")
        self.cb_resume.setChecked(bool(settings["resume_enabled"]))
        self.cb_resume.toggled.connect(lambda v: self._set("resume_enabled", v))
        root.addWidget(self.cb_resume)

        self.cb_autoplay = QCheckBox("一个视频播完自动接着下一个")
        self.cb_autoplay.setChecked(bool(settings["autoplay_next"]))
        self.cb_autoplay.toggled.connect(lambda v: self._set("autoplay_next", v))
        root.addWidget(self.cb_autoplay)

        self.cb_native = QCheckBox("打开视频时按原始分辨率调整窗口（而不是最大化）")
        self.cb_native.setChecked(bool(settings["open_native_size"]))
        self.cb_native.toggled.connect(lambda v: self._set("open_native_size", v))
        root.addWidget(self.cb_native)

        self.cb_scroll = QCheckBox("切换回之前的文件夹时恢复滚动位置")
        self.cb_scroll.setChecked(bool(settings["remember_scroll"]))
        self.cb_scroll.toggled.connect(lambda v: self._set("remember_scroll", v))
        root.addWidget(self.cb_scroll)

        self.combo_hwdec = QComboBox()
        for value, label in _HWDEC_CHOICES:
            self.combo_hwdec.addItem(label, value)
        cur = self.combo_hwdec.findData(str(settings["hwdec"]))
        self.combo_hwdec.setCurrentIndex(max(0, cur))
        self.combo_hwdec.currentIndexChanged.connect(
            lambda _=0: self._set("hwdec", self.combo_hwdec.currentData())
        )
        root.addWidget(_row("默认解码模式", self.combo_hwdec, "下个视频生效"))

        # ---- 音量 / 字幕
        root.addWidget(_section("音量 · 字幕"))
        self.sl_volume = QSlider(Qt.Horizontal)
        self.sl_volume.setRange(0, 130)
        self.sl_volume.setFixedWidth(180)
        self.sl_volume.setValue(int(settings["volume"]))
        self.lab_volume = QLabel(f"{int(settings['volume'])}%")
        self.sl_volume.valueChanged.connect(self._on_volume)
        vol_box = QWidget()
        vb = QHBoxLayout(vol_box)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.addWidget(self.sl_volume)
        vb.addWidget(self.lab_volume)
        vb.addStretch(1)
        root.addWidget(_row("默认音量", vol_box, "下次启动生效"))

        self.sp_subsize = QSpinBox()
        self.sp_subsize.setRange(16, 96)
        self.sp_subsize.setValue(int(settings["sub_font_size"]))
        self.sp_subsize.valueChanged.connect(lambda v: self._set("sub_font_size", v))
        root.addWidget(_row("默认字幕字号", self.sp_subsize, "下个视频生效"))

        self.cb_subvis = QCheckBox("默认显示字幕")
        self.cb_subvis.setChecked(bool(settings["sub_visible"]))
        self.cb_subvis.toggled.connect(lambda v: self._set("sub_visible", v))
        root.addWidget(self.cb_subvis)

        # ---- 截图 / GIF
        root.addWidget(_section("截图 · GIF 录制"))
        self.sp_fps = QSpinBox()
        self.sp_fps.setRange(2, 30)
        self.sp_fps.setValue(int(settings["gif_fps"]))
        self.sp_fps.valueChanged.connect(lambda v: self._set("gif_fps", v))
        root.addWidget(_row("GIF 帧率", self.sp_fps, "帧/秒"))

        self.sp_secs = QSpinBox()
        self.sp_secs.setRange(1, 120)
        self.sp_secs.setValue(int(settings["gif_max_seconds"]))
        self.sp_secs.valueChanged.connect(lambda v: self._set("gif_max_seconds", v))
        root.addWidget(_row("GIF 最长时长", self.sp_secs, "秒（到时自动结束）"))

        self.sp_width = QSpinBox()
        self.sp_width.setRange(120, 1920)
        self.sp_width.setSingleStep(40)
        self.sp_width.setValue(int(settings["gif_max_width"]))
        self.sp_width.valueChanged.connect(lambda v: self._set("gif_max_width", v))
        root.addWidget(_row("GIF 最大宽度", self.sp_width, "像素"))

        # ---- 截图保存路径
        shot_box = QWidget()
        sb = QHBoxLayout(shot_box)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(6)
        self.edit_shot_path = QLineEdit()
        self.edit_shot_path.setReadOnly(True)
        self.edit_shot_path.setPlaceholderText("自动（先试视频所在文件夹，不行则存到程序旁的「截图」目录）")
        self.edit_shot_path.setStyleSheet(
            f"QLineEdit {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:4px; padding:3px 6px; }}"
        )
        custom = str(settings["capture_path"] or "").strip()
        if custom:
            self.edit_shot_path.setText(custom)
        sb.addWidget(self.edit_shot_path, 1)
        btn_browse = QPushButton("浏览…")
        btn_browse.setFocusPolicy(Qt.NoFocus)
        btn_browse.clicked.connect(self._browse_shot_path)
        sb.addWidget(btn_browse)
        btn_open = QPushButton("打开")
        btn_open.setFocusPolicy(Qt.NoFocus)
        btn_open.clicked.connect(self._open_shot_folder)
        sb.addWidget(btn_open)
        btn_clear = QPushButton("清除")
        btn_clear.setFocusPolicy(Qt.NoFocus)
        btn_clear.clicked.connect(lambda: (self.edit_shot_path.clear(), self._set("capture_path", "")))
        sb.addWidget(btn_clear)
        root.addWidget(_row("截图保存到", shot_box, "留空=自动选择"))

        # ---- 文件关联
        root.addWidget(_section("文件关联（Windows）"))
        assoc_box = QWidget()
        ab = QHBoxLayout(assoc_box)
        ab.setContentsMargins(0, 0, 0, 0)
        ab.setSpacing(8)
        self.btn_assoc = QPushButton("注册到「打开方式」")
        self.btn_assoc.setFocusPolicy(Qt.NoFocus)
        self.btn_assoc.clicked.connect(self._register_assoc)
        ab.addWidget(self.btn_assoc)
        self.btn_unassoc = QPushButton("取消关联")
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
        note = QLabel("提示：解码模式 / 字幕字号 / 默认音量在下个视频或下次启动时生效，其它即时生效。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:12px;")
        root.addWidget(note)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_close = QPushButton("完成")
        btn_close.setFocusPolicy(Qt.NoFocus)
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        root.addLayout(footer)

    # ------------------------------------------------------------- helpers

    def _set(self, key: str, value) -> None:
        settings[key] = value

    def _on_volume(self, v: int) -> None:
        self.lab_volume.setText(f"{v}%")
        settings["volume"] = int(v)

    def _browse_shot_path(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        start = self.edit_shot_path.text() or str(APP_DIR)
        d = QFileDialog.getExistingDirectory(self, "选择截图保存目录", start)
        if d:
            self.edit_shot_path.setText(d)
            self._set("capture_path", d)

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
            self.assoc_hint.setText(
                "已注册。现在右键任意视频/图片 →「打开方式」即可看到「媒体播放器」，"
                "选中并勾选「始终使用」就能设为默认。"
            )
        except Exception as exc:  # pragma: no cover - registry edge cases
            self.assoc_hint.setText(f"注册失败：{exc}")

    def _unregister_assoc(self) -> None:
        try:
            assoc.unregister()
            self.assoc_hint.setText("已取消关联。")
        except Exception as exc:  # pragma: no cover
            self.assoc_hint.setText(f"取消失败：{exc}")

    def _refresh_assoc_hint(self) -> None:
        if not assoc.is_supported():
            self.btn_assoc.setEnabled(False)
            self.btn_unassoc.setEnabled(False)
            self.assoc_hint.setText("文件关联仅在 Windows 上可用。")
            return
        if assoc.is_registered():
            self.assoc_hint.setText("状态：已注册到「打开方式」。")
        else:
            self.assoc_hint.setText(
                "把本程序加入右键「打开方式」列表（仅当前用户，无需管理员）。"
            )

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