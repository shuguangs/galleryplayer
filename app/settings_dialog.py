"""Central settings window: gathers the toggles that were previously JSON-only.

Everything here writes straight into `settings` and is flushed to disk when the
dialog closes. A few options (default decode mode, subtitle size, default volume)
are read when a video is first opened, so they take effect from the next video /
next launch rather than mid-playback -- the footer says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class FlowLayout(QLayout):
    """按钮空间不足时自动换行，避免窗口缩窄后控件被吞掉。"""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        x, y = area.x(), area.y()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if line_height and next_x - spacing > area.right() + 1:
                x = area.x()
                y += line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()

from . import assoc, theme
from .config import (
    ASR_MODEL_SPECS,
    PRESET_MODELS,
    TRANSLATE_MODEL_SPECS,
    flush,
    settings,
)
from .i18n import LANGUAGES, current_language, set_language, t
from .runtime import APP_DIR
from .scenarios import load_scenarios


def _asr_model_label(key: str) -> str:
    """下拉项文字：模型名 + 磁盘/显存需求，选择时即可看到资源代价。"""
    spec = ASR_MODEL_SPECS.get(key)
    if spec is None:
        return key
    return (f"{spec['label']}（{spec['size_gb']:g}GB 磁盘 / "
            f"{spec['vram_gb']:g}GB 显存）")

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
        f" border-bottom:1px solid {theme.BORDER}; padding:6px 0 2px 0; margin-top:4px;"
    )
    return lab


def _row(label: str, widget: QWidget, hint: str = "") -> QWidget:
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 2, 0, 2)
    lay.setSpacing(8)
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
        # parent 一律不认：QDialog 带父窗口在 Windows 上是 owned window，
        # 主窗口最小化时会被一起最小化。独立 Qt.Window 才能各自最小化。
        super().__init__(None)
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle(t("settings.title"))
        self.setMinimumWidth(520)
        self.resize(640, 480)
        self.setStyleSheet(
            f"QDialog {{ background:{theme.BG_BASE}; }}"
            f"QLabel {{ color:{theme.TEXT}; }}"
            f"QCheckBox {{ color:{theme.TEXT}; }}"
            f"QComboBox {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:6px;"
            f" padding:4px 26px 4px 9px; }}"
            f"QComboBox:hover {{ background:{theme.BG_HOVER}; }}"
            f"QComboBox QAbstractItemView {{ background:{theme.BG_PANEL};"
            f" color:{theme.TEXT}; border:1px solid {theme.BORDER};"
            f" selection-background-color:{theme.BG_SELECT};"
            f" selection-color:{theme.TEXT}; outline:0; }}"
        )
        root_outer = QVBoxLayout(self)
        root_outer.setContentsMargins(14, 10, 14, 10)
        root_outer.setSpacing(7)

        # All option groups live in this viewport; the Done button stays visible.
        scroll = QScrollArea(self)
        self._scroll_area = scroll  # 滚轮转发目标（见 eventFilter）
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            f"QScrollBar:vertical {{ background: {theme.BG_BASE}; width: 10px; margin: 0; }}"
            f"QScrollBar::handle:vertical {{ background: {theme.BG_HOVER};"
            f" border-radius: 5px; min-height: 30px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {theme.ACCENT_DIM}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}"
        )
        content = QWidget()
        content.setObjectName("SettingsContent")
        content.setStyleSheet("QWidget#SettingsContent { background: transparent; }")
        root_outer.addWidget(scroll, 1)

        root = QVBoxLayout(content)
        root.setContentsMargins(6, 2, 8, 2)
        root.setSpacing(1)

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

        # ---- 字幕引擎 + 实时字幕（分组网格）
        from PySide6.QtWidgets import QGridLayout, QGroupBox

        grp = QGroupBox(t("settings.section_subtitles"))
        grp.setStyleSheet(
            f"QGroupBox {{ border:1px solid {theme.BORDER}; border-radius:6px;"
            f" margin-top:6px; padding-top:2px; }}"
            f"QGroupBox::title {{ subcontrol-origin:margin; left:10px;"
            f" padding:0 4px; color:{theme.TEXT_DIM}; }}"
        )
        gg = QGridLayout(grp)
        gg.setContentsMargins(8, 6, 8, 6)
        gg.setSpacing(4)

        # 引擎目录
        self.edit_subtitle_dir = QLineEdit()
        self.edit_subtitle_dir.setReadOnly(True)
        self.edit_subtitle_dir.setPlaceholderText(t("settings.subtitle_dir_placeholder"))
        self.edit_subtitle_dir.setToolTip(t("settings.subtitle_dir_hint"))
        self.edit_subtitle_dir.setText(str(settings["subtitle_pipeline_dir"] or "").strip())
        btn_sb = QPushButton(t("settings.browse_ellipsis"))
        btn_sb.setFocusPolicy(Qt.NoFocus)
        btn_sb.clicked.connect(self._browse_subtitle_dir)
        btn_sc = QPushButton(t("settings.detect"))
        btn_sc.setFocusPolicy(Qt.NoFocus)
        btn_sc.clicked.connect(self._detect_subtitle_dir)
        gg.addWidget(QLabel(t("settings.subtitle_dir_label")), 0, 0)
        gg.addWidget(self.edit_subtitle_dir, 0, 1, 1, 2)
        gg.addWidget(btn_sb, 0, 3)
        gg.addWidget(btn_sc, 0, 4)
        self.sub_status = QLabel("")
        self.sub_status.setStyleSheet(f"color:{theme.TEXT_DIM};")
        gg.addWidget(self.sub_status, 1, 0, 1, 5)
        self._refresh_subtitle_status()

        # 识别引擎（下拉）：Qwen3-ASR / SenseVoice / whisper 各档位
        self.cb_live_asr = QComboBox()
        for m in ("qwen", "sensevoice", "large-v3", "medium", "small", "base", "tiny"):
            self.cb_live_asr.addItem(_asr_model_label(m), m)
        idx = self.cb_live_asr.findData(settings["live_asr_model"])
        self.cb_live_asr.setCurrentIndex(max(0, idx))
        self.cb_live_asr.currentIndexChanged.connect(
            self._on_live_asr_model_changed)
        self.cb_live_preset = QComboBox()
        for value, key in (
            ("fast", "settings.live_model_preset_fast"),
            ("balanced", "settings.live_model_preset_balanced"),
            ("accurate", "settings.live_model_preset_accurate"),
            ("custom", "settings.live_model_preset_custom"),
        ):
            self.cb_live_preset.addItem(t(key), value)
        idx = self.cb_live_preset.findData(str(settings["live_model_preset"]))
        self.cb_live_preset.setCurrentIndex(max(0, idx))
        self.cb_live_preset.setToolTip(t("settings.live_model_preset_hint"))
        self.cb_live_preset.currentIndexChanged.connect(self._on_live_preset_changed)
        if str(settings["live_model_preset"]) != "custom":
            mapped = PRESET_MODELS[str(settings["live_model_preset"])]
            idx = self.cb_live_asr.findData(mapped)
            if idx >= 0:
                self.cb_live_asr.blockSignals(True)
                self.cb_live_asr.setCurrentIndex(idx)
                self.cb_live_asr.blockSignals(False)
            settings["live_asr_model"] = mapped
        self.cb_live_translate = QComboBox()
        for m, spec in TRANSLATE_MODEL_SPECS.items():
            suffix = "" if m == "none" else f"　{spec['size_gb']:g}GB"
            self.cb_live_translate.addItem(f"{spec['label']}{suffix}", m)
        idx = self.cb_live_translate.findData(settings["live_ollama_model"])
        if idx < 0:  # 用户配了清单外的模型名：保留它，不要静默改掉
            self.cb_live_translate.addItem(str(settings["live_ollama_model"]),
                                           settings["live_ollama_model"])
            idx = self.cb_live_translate.count() - 1
        self.cb_live_translate.setCurrentIndex(idx)
        self.cb_live_translate.setToolTip(t("settings.live_translate_hint"))
        self.cb_live_translate.currentIndexChanged.connect(
            lambda _i: (self._set("live_ollama_model", self.cb_live_translate.currentData()),
                        self._update_combo_resources()))
        # 识别 + 翻译两个选择的合计资源占用（随任一下拉变化即时更新）
        self.combo_res_label = QLabel("")
        self.combo_res_label.setWordWrap(True)
        self.combo_res_label.setStyleSheet(f"color:{theme.TEXT_DIM};")
        gg.addWidget(self.combo_res_label, 3, 0, 1, 5)
        self.cb_live_asr.currentIndexChanged.connect(
            lambda _i: self._update_combo_resources())
        self.cb_live_lang = QComboBox()
        for lang in ("auto", "zh", "yue", "en", "ja", "ko", "fr", "de", "es"):
            self.cb_live_lang.addItem(f"{t('settings.live_lang_' + lang)}（{lang}）", lang)
        idx = self.cb_live_lang.findData(settings["live_caption_lang"])
        self.cb_live_lang.setCurrentIndex(max(0, idx))
        self.cb_live_lang.currentIndexChanged.connect(
            lambda _i: self._set("live_caption_lang", self.cb_live_lang.currentData()))

        # 生成 SRT 的翻译模型：与实时字幕分开；可选用 llama.cpp 大模型（仅 SRT 时启动）
        self.cb_srt_translate = QComboBox()
        self.cb_srt_translate.addItem(t("settings.srt_follow_live"), "live")
        for m, spec in TRANSLATE_MODEL_SPECS.items():
            if m == "none":
                continue
            self.cb_srt_translate.addItem(f"{spec['label']}　{spec['size_gb']:g}GB", m)
        self.cb_srt_translate.addItem(
            t("settings.srt_hymt2_label"), "hy-mt2-30b")
        idx = self.cb_srt_translate.findData(settings["srt_translate_model"])
        if idx < 0:
            idx = 0
        self.cb_srt_translate.setCurrentIndex(idx)
        self.cb_srt_translate.setToolTip(t("settings.srt_translate_hint"))
        self.cb_srt_translate.currentIndexChanged.connect(
            lambda _i: self._set("srt_translate_model", self.cb_srt_translate.currentData()))
        gg.addWidget(QLabel(t("settings.srt_translate_label")), 14, 0)
        gg.addWidget(self.cb_srt_translate, 14, 1, 1, 2)
        srt_hint = QLabel(t("settings.srt_translate_hint"))
        srt_hint.setWordWrap(True)
        srt_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        gg.addWidget(srt_hint, 15, 0, 1, 5)

        # 翻译目标语言 + SRT 导出格式（同一行两个下拉）
        self.cb_translate_target = QComboBox()
        for code, key in (("zh", "zh"), ("zh-Hant", "zh-Hant"), ("en", "en")):
            self.cb_translate_target.addItem(t("settings.translate_target_" + key), code)
        idx = self.cb_translate_target.findData(str(settings["live_translate_target"]))
        self.cb_translate_target.setCurrentIndex(max(0, idx))
        self.cb_translate_target.setToolTip(t("settings.translate_target_hint"))
        self.cb_translate_target.currentIndexChanged.connect(
            lambda _i: self._set("live_translate_target",
                                 self.cb_translate_target.currentData()))
        gg.addWidget(QLabel(t("settings.translate_target_label")), 16, 0)
        gg.addWidget(self.cb_translate_target, 16, 1)

        self.cb_srt_format = QComboBox()
        for fmt in ("srt", "vtt", "ass"):
            self.cb_srt_format.addItem(fmt.upper(), fmt)
        idx = self.cb_srt_format.findData(str(settings["srt_export_format"]))
        self.cb_srt_format.setCurrentIndex(max(0, idx))
        self.cb_srt_format.currentIndexChanged.connect(
            lambda _i: self._set("srt_export_format", self.cb_srt_format.currentData()))
        gg.addWidget(QLabel(t("settings.srt_format_label")), 16, 2)
        gg.addWidget(self.cb_srt_format, 16, 3)

        # 空闲自动释放显存（引擎侧 N 分钟无任务即卸载模型，下次任务自动重载）
        self.spin_idle_unload = QSpinBox()
        self.spin_idle_unload.setRange(0, 240)
        self.spin_idle_unload.setSuffix(" min")
        self.spin_idle_unload.setSpecialValueText(t("settings.idle_unload_off"))
        self.spin_idle_unload.setValue(int(settings["live_caption_idle_unload"]) // 60)
        self.spin_idle_unload.setToolTip(t("settings.idle_unload_hint"))
        self.spin_idle_unload.valueChanged.connect(
            lambda v: self._set("live_caption_idle_unload", int(v) * 60))
        gg.addWidget(QLabel(t("settings.idle_unload_label")), 17, 0)
        gg.addWidget(self.spin_idle_unload, 17, 1)

        # 内容场景提示词组：按片源类型微调翻译语气与术语策略。放第 17 行的空
        # 单元格（col 2/3）——这张表其余行只有 0-4 五列、跨行说明都是 colspan=5，
        # 另开第 6 列会把分组最小宽度再拉宽近 200px，而新控件正好落在默认窗宽
        # 之外（要横向滚动才看得到）
        self.cb_translate_scenario = QComboBox()
        for scenario in load_scenarios():
            labels = scenario["label"]
            label = labels.get(current_language()) or labels.get("zh") or scenario["key"]
            self.cb_translate_scenario.addItem(label, scenario["key"])
        idx = self.cb_translate_scenario.findData(str(settings["translate_scenario"]))
        self.cb_translate_scenario.setCurrentIndex(max(0, idx))
        self.cb_translate_scenario.setToolTip(t("settings.translate_scenario_hint"))
        self.cb_translate_scenario.currentIndexChanged.connect(
            lambda _i: self._set("translate_scenario",
                                 self.cb_translate_scenario.currentData()))
        gg.addWidget(QLabel(t("settings.translate_scenario_label")), 17, 2)
        gg.addWidget(self.cb_translate_scenario, 17, 3)

        # 实时字幕覆盖层：字号 + 覆盖范围（按播放区域百分比）
        self.spin_live_font = QSpinBox()
        self.spin_live_font.setRange(12, 96)
        self.spin_live_font.setValue(int(settings["live_caption_font_size"]))
        self.spin_live_font.valueChanged.connect(
            lambda v: self._set("live_caption_font_size", int(v)))
        self.spin_live_width = QSpinBox()
        self.spin_live_width.setRange(40, 100)
        self.spin_live_width.setSuffix("%")
        self.spin_live_width.setValue(int(settings["live_caption_width"]))
        self.spin_live_width.valueChanged.connect(
            lambda v: self._set("live_caption_width", int(v)))
        self.spin_live_height = QSpinBox()
        self.spin_live_height.setRange(8, 40)
        self.spin_live_height.setSuffix("%")
        self.spin_live_height.setValue(int(settings["live_caption_height"]))
        self.spin_live_height.valueChanged.connect(
            lambda v: self._set("live_caption_height", int(v)))
        for widget, key in (
            (self.spin_live_font, "live_caption_font_size"),
            (self.spin_live_width, "live_caption_width"),
            (self.spin_live_height, "live_caption_height"),
        ):
            widget.setToolTip(t("settings.live_caption_display_hint"))

        gg.addWidget(QLabel(t("settings.live_asr_label")), 2, 0)
        gg.addWidget(self.cb_live_asr, 2, 1)
        gg.addWidget(QLabel(t("settings.live_model_preset_label")), 13, 0)
        gg.addWidget(self.cb_live_preset, 13, 1)
        self.cb_hardware_model = QCheckBox(t("settings.hardware_aware_model_label"))
        self.cb_hardware_model.setToolTip(t("settings.hardware_aware_model_hint"))
        self.cb_hardware_model.setChecked(bool(settings["hardware_aware_model"]))
        self.cb_hardware_model.toggled.connect(
            lambda value: self._set("hardware_aware_model", value)
        )
        gg.addWidget(self.cb_hardware_model, 13, 2, 1, 3)
        gg.addWidget(QLabel(t("settings.live_translate_label")), 2, 2)
        gg.addWidget(self.cb_live_translate, 2, 3, 1, 2)
        # 所选引擎的资源需求 / 本机是否够（选择时即时更新）
        self.asr_hint = QLabel("")
        self.asr_hint.setWordWrap(True)
        self.asr_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        gg.addWidget(self.asr_hint, 18, 0, 1, 5)
        self._warn_model_resources(str(settings["live_asr_model"]))
        self._update_combo_resources()

        # 模型目录（读取本地模型文件夹）
        self.edit_asr_dir = QLineEdit()
        self.edit_asr_dir.setReadOnly(True)
        self.edit_asr_dir.setPlaceholderText(t("settings.live_asr_dir_hint"))
        self.edit_asr_dir.setToolTip(t("settings.live_asr_dir_hint"))
        self.edit_asr_dir.setText(str(settings["live_asr_dir"] or ""))
        btn_ad = QPushButton(t("settings.browse_ellipsis"))
        btn_ad.setFocusPolicy(Qt.NoFocus)
        btn_ad.clicked.connect(self._browse_asr_dir)
        btn_ac = QPushButton(t("settings.clear"))
        btn_ac.setFocusPolicy(Qt.NoFocus)
        btn_ac.clicked.connect(lambda: (self.edit_asr_dir.clear(),
                                        self._set("live_asr_dir", "")))
        gg.addWidget(QLabel(t("settings.live_asr_dir_label")), 4, 0)
        gg.addWidget(self.edit_asr_dir, 4, 1, 1, 2)
        gg.addWidget(btn_ad, 4, 3)
        gg.addWidget(btn_ac, 4, 4)

        # 来源 + 保存位置（下拉）
        self.cb_live_source = QComboBox()
        self.cb_live_source.addItem(t("settings.live_source_audio"), "audio")
        self.cb_live_source.addItem(t("settings.live_source_loopback"), "loopback")
        self.cb_live_source.setToolTip(t("settings.live_source_hint"))
        idx = self.cb_live_source.findData(settings["live_caption_source"])
        self.cb_live_source.setCurrentIndex(max(0, idx))
        self.cb_live_source.currentIndexChanged.connect(
            lambda _i: self._set("live_caption_source", self.cb_live_source.currentData()))
        self.cb_subtitle_save = QComboBox()
        self.cb_subtitle_save.addItem(t("settings.subtitle_save_media"), "media")
        self.cb_subtitle_save.addItem(t("settings.subtitle_save_player"), "player")
        self.cb_subtitle_save.setToolTip(t("settings.subtitle_save_hint"))
        idx = self.cb_subtitle_save.findData(settings["subtitle_save_dir"])
        self.cb_subtitle_save.setCurrentIndex(max(0, idx))
        self.cb_subtitle_save.currentIndexChanged.connect(
            lambda _i: self._set("subtitle_save_dir", self.cb_subtitle_save.currentData()))
        gg.addWidget(QLabel(t("settings.live_source_label")), 5, 0)
        gg.addWidget(self.cb_live_source, 5, 1)
        gg.addWidget(QLabel(t("settings.subtitle_save_label")), 5, 2)
        gg.addWidget(self.cb_subtitle_save, 5, 3, 1, 2)

        self.cb_live_resident = QCheckBox(t("settings.live_resident_label"))
        # 显存占用随实际引擎变化，不写死数字
        from .live_engine import model_label, vram_footprint_gb

        self.cb_live_resident.setToolTip(t("settings.live_resident_hint").format(
            model=model_label(), vram=f"{vram_footprint_gb():g}GB"))
        self.cb_live_resident.setChecked(bool(settings["live_caption_resident"]))
        self.cb_live_resident.toggled.connect(lambda v: self._set("live_caption_resident", v))
        gg.addWidget(self.cb_live_resident, 6, 0, 1, 5)

        self.cb_live_preload = QCheckBox(t("settings.live_model_preload_label"))
        self.cb_live_preload.setToolTip(t("settings.live_model_preload_hint"))
        self.cb_live_preload.setChecked(bool(settings["live_model_preload"]))
        self.cb_live_preload.toggled.connect(lambda v: self._set("live_model_preload", v))
        gg.addWidget(self.cb_live_preload, 9, 0, 1, 5)
        preload_hint = QLabel(t("settings.live_model_preload_hint"))
        preload_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        preload_hint.setWordWrap(True)
        gg.addWidget(preload_hint, 10, 0, 1, 5)
        gg.addWidget(QLabel(t("settings.live_caption_display_label")), 7, 0)
        gg.addWidget(self.spin_live_font, 7, 1)
        gg.addWidget(QLabel(t("settings.live_caption_width_label")), 7, 2)
        gg.addWidget(self.spin_live_width, 7, 3)
        gg.addWidget(QLabel(t("settings.live_caption_height_label")), 8, 2)
        gg.addWidget(self.spin_live_height, 8, 3)
        display_hint = QLabel(t("settings.live_caption_display_hint"))
        display_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        display_hint.setWordWrap(True)
        gg.addWidget(display_hint, 8, 0, 1, 2)

        self.slider_bilingual = QSlider(Qt.Horizontal)
        self.slider_bilingual.setRange(0, 100)
        self.slider_bilingual.setValue(int(float(settings["caption_bilingual_ratio"]) * 100))
        self.slider_bilingual.setToolTip(t("settings.caption_bilingual_hint"))
        self.slider_bilingual.valueChanged.connect(
            lambda v: self._set("caption_bilingual_ratio", v / 100.0)
        )
        gg.addWidget(QLabel(t("settings.caption_bilingual_label")), 11, 0)
        gg.addWidget(self.slider_bilingual, 11, 1, 1, 4)

        self.edit_glossary = QLineEdit()
        self.edit_glossary.setPlaceholderText(t("settings.caption_glossary_hint"))
        self.edit_glossary.setText(self._format_glossary(settings["caption_glossary"]))
        self.edit_glossary.editingFinished.connect(self._save_glossary)
        gg.addWidget(QLabel(t("settings.caption_glossary_label")), 12, 0)
        gg.addWidget(self.edit_glossary, 12, 1, 1, 4)
        root.addWidget(grp)

        # ---- 一键安装（分组）
        inst_grp = QGroupBox(t("settings.install_group"))
        inst_grp.setStyleSheet(grp.styleSheet())
        ig = QGridLayout(inst_grp)
        ig.setContentsMargins(8, 6, 8, 6)
        ig.setSpacing(4)
        self.install_model = QComboBox()
        for m in ("qwen", "sensevoice", "large-v3", "medium", "small"):
            self.install_model.addItem(_asr_model_label(m), m)
        self.install_model.setCurrentIndex(0)  # 默认 Qwen3-ASR（实测最准）
        self.install_model.currentTextChanged.connect(self._update_install_hint)
        self.install_translate = QComboBox()
        for m, spec in TRANSLATE_MODEL_SPECS.items():
            suffix = "" if m == "none" else f"　{spec['size_gb']:g}GB"
            self.install_translate.addItem(f"{spec['label']}{suffix}", m)
        self.install_translate.setCurrentIndex(1)  # 默认 qwen3:8b
        self.install_translate.currentTextChanged.connect(self._update_install_hint)
        self.install_mirror = QComboBox()
        self.install_mirror.addItem("huggingface.co", "huggingface")
        self.install_mirror.addItem("hf-mirror.com（国内快）", "hf-mirror")
        self.install_mirror.setToolTip(t("settings.install_mirror_hint"))
        ig.addWidget(QLabel(t("settings.install_model_label")), 0, 0)
        ig.addWidget(self.install_model, 0, 1)
        ig.addWidget(QLabel(t("settings.install_translate_label")), 0, 2)
        ig.addWidget(self.install_translate, 0, 3)
        ig.addWidget(QLabel(t("settings.install_mirror_label")), 0, 4)
        ig.addWidget(self.install_mirror, 0, 5)

        self.install_hint = QLabel("")
        self.install_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        self.install_hint.setWordWrap(True)
        ig.addWidget(self.install_hint, 1, 0, 1, 6)

        self.btn_install = QPushButton(t("settings.install_start"))
        self.btn_install.setFocusPolicy(Qt.NoFocus)
        self.btn_install.clicked.connect(self._start_install)
        self.btn_install_llama = QPushButton(t("settings.install_llama"))
        self.btn_install_llama.setFocusPolicy(Qt.NoFocus)
        self.btn_install_llama.setToolTip(t("settings.install_llama_hint"))
        self.btn_install_llama.clicked.connect(self._start_install_llama)
        self.install_status = QLabel("")
        self.install_status.setStyleSheet(f"color:{theme.TEXT_DIM};")
        ig.addWidget(self.btn_install, 2, 0)
        ig.addWidget(self.btn_install_llama, 2, 2)
        ig.addWidget(self.install_status, 2, 3, 1, 3)

        self.install_log = QPlainTextEdit()
        self.install_log.setReadOnly(True)
        self.install_log.setMaximumHeight(96)
        self.install_log.setStyleSheet(
            f"QPlainTextEdit {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:4px;"
            f" font-family:Consolas; font-size:11px; }}"
        )
        ig.addWidget(self.install_log, 3, 0, 1, 6)
        root.addWidget(inst_grp)
        self._update_install_hint()

        diag_grp = QGroupBox(t("settings.live_diagnostics_group"))
        diag_grp.setStyleSheet(grp.styleSheet())
        dg = QGridLayout(diag_grp)
        dg.setContentsMargins(8, 6, 8, 6)
        dg.setSpacing(4)
        self.live_diag_status = QLabel("")
        self.live_diag_status.setWordWrap(True)
        self.live_diag_status.setStyleSheet(f"color:{theme.TEXT_DIM};")
        dg.addWidget(self.live_diag_status, 0, 0, 1, 4)
        self.live_diag_task = QLabel("")
        self.live_diag_task.setWordWrap(True)
        self.live_diag_task.setStyleSheet(f"color:{theme.TEXT_DIM};")
        dg.addWidget(self.live_diag_task, 1, 0, 1, 4)
        self.live_diag_log = QPlainTextEdit()
        self.live_diag_log.setReadOnly(True)
        self.live_diag_log.setMaximumHeight(110)
        self.live_diag_log.setStyleSheet(
            f"QPlainTextEdit {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:4px;"
            f" font-family:Consolas; font-size:11px; }}"
        )
        dg.addWidget(self.live_diag_log, 2, 0, 1, 4)
        diag_buttons = QWidget()
        diag_buttons_layout = FlowLayout(diag_buttons, spacing=6)
        btn_diag_refresh = QPushButton(t("settings.live_diagnostics_refresh"))
        btn_diag_refresh.setFocusPolicy(Qt.NoFocus)
        btn_diag_refresh.clicked.connect(self._refresh_live_diagnostics)
        diag_buttons_layout.addWidget(btn_diag_refresh)
        btn_diag_restart = QPushButton(t("settings.live_diagnostics_restart"))
        btn_diag_restart.setFocusPolicy(Qt.NoFocus)
        btn_diag_restart.clicked.connect(self._restart_live_engine)
        diag_buttons_layout.addWidget(btn_diag_restart)
        dg.addWidget(diag_buttons, 3, 0, 1, 4)
        root.addWidget(diag_grp)
        self._refresh_live_diagnostics()
        # ---- 文件关联
        root.addWidget(_section(t("settings.section_assoc")))
        assoc_box = QWidget()
        ab = FlowLayout(assoc_box, spacing=8)
        self.btn_assoc = QPushButton(t("settings.assoc_register"))
        self.btn_assoc.setFocusPolicy(Qt.NoFocus)
        self.btn_assoc.clicked.connect(self._register_assoc)
        ab.addWidget(self.btn_assoc)
        self.btn_unassoc = QPushButton(t("settings.assoc_unregister"))
        self.btn_unassoc.setFocusPolicy(Qt.NoFocus)
        self.btn_unassoc.clicked.connect(self._unregister_assoc)
        ab.addWidget(self.btn_unassoc)
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
        footer.setContentsMargins(0, 0, 2, 0)
        footer.addStretch(1)
        btn_close = QPushButton(t("settings.done"))
        btn_close.setFocusPolicy(Qt.NoFocus)
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        root.addStretch(1)
        scroll.setWidget(content)
        root_outer.addLayout(footer)

        # 悬停在下拉框/滑动条上滚轮会误改设置并卡住界面滚动——统一禁用，
        # 滚轮事件转发给设置页滚动区。安装动作放 showEvent 里反复执行，
        # 确保运行期动态创建的控件（新引擎选项等）也全部覆盖
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication

        self._wheel_types = (QEvent.Wheel,)
        self._qapp = QApplication
        self._install_wheel_filter()

        screen = self.screen()
        if screen is not None:
            max_h = screen.availableGeometry().height() - 80
            if self.height() > max_h:
                self.resize(self.width(), max(360, max_h))

    # ------------------------------------------------------------- helpers

    def _set(self, key: str, value) -> None:
        settings[key] = value

    @staticmethod
    def _format_glossary(glossary: dict) -> str:
        return "; ".join(f"{key}={value}" for key, value in glossary.items())

    def _save_glossary(self) -> None:
        glossary: dict[str, str] = {}
        for part in self.edit_glossary.text().split(";"):
            if "=" not in part:
                continue
            source, target = part.split("=", 1)
            source, target = source.strip(), target.strip()
            if source and target:
                glossary[source] = target
        self._set("caption_glossary", glossary)

    def _on_live_preset_changed(self, _index: int) -> None:
        preset = self.cb_live_preset.currentData()
        self._set("live_model_preset", preset)
        if preset == "custom":
            return
        model = PRESET_MODELS[preset]
        idx = self.cb_live_asr.findData(model)
        if idx >= 0:
            self.cb_live_asr.blockSignals(True)
            self.cb_live_asr.setCurrentIndex(idx)
            self.cb_live_asr.blockSignals(False)
        self._set("live_asr_model", model)
        self._warn_model_resources(model)

    def _on_live_asr_model_changed(self, _index: int) -> None:
        model = self.cb_live_asr.currentData()
        self._set("live_asr_model", model)
        preset = {v: k for k, v in PRESET_MODELS.items()}.get(str(model), "custom")
        idx = self.cb_live_preset.findData(preset)
        if idx >= 0:
            self.cb_live_preset.blockSignals(True)
            self.cb_live_preset.setCurrentIndex(idx)
            self.cb_live_preset.blockSignals(False)
        self._set("live_model_preset", preset)
        self._warn_model_resources(model)

    def _update_combo_resources(self) -> None:
        """当前「识别 + 翻译」两个选择的合计磁盘/显存，并对本机显存给出判断。"""
        from . import live_engine

        asr = str(self.cb_live_asr.currentData() or "")
        tr = str(self.cb_live_translate.currentData() or "none")
        spec = ASR_MODEL_SPECS.get(asr)
        # hy-mt2-30b 是 SRT 专用（llama.cpp），不参与实时字幕合计
        tr_spec = TRANSLATE_MODEL_SPECS.get(tr) if tr != "hy-mt2-30b" else None
        if spec is None or not hasattr(self, "combo_res_label"):
            return
        disk = spec["size_gb"] + (tr_spec["size_gb"] if tr_spec else 0.0)
        vram = spec["vram_gb"] + (tr_spec["vram_gb"] if tr_spec else 0.0)
        hardware = live_engine.hardware_snapshot()
        vram_gb = hardware.get("vram_mb", 0) / 1024.0
        parts = [t("settings.combo_res").format(
            disk=f"{disk:g}", vram=f"{vram:g}")]
        if vram_gb <= 0:
            parts.append(t("settings.combo_res_cpu"))
        elif vram_gb + 0.4 < vram:
            parts.append(t("settings.combo_res_low").format(
                have=f"{vram_gb:.1f}", need=f"{vram:g}"))
        else:
            parts.append(t("settings.combo_res_ok").format(have=f"{vram_gb:.1f}"))
        self.combo_res_label.setText(" ".join(parts))

    def _warn_model_resources(self, model: str) -> None:
        """所选引擎的资源需求 + 本机是否够（显存/磁盘不足时给出明确提示）。"""
        from . import live_engine

        spec = ASR_MODEL_SPECS.get(str(model))
        if spec is None or not hasattr(self, "asr_hint"):
            return
        hardware = live_engine.hardware_snapshot()
        vram_gb = hardware.get("vram_mb", 0) / 1024.0
        parts = [t("settings.model_need").format(
            name=spec["label"], disk=f"{spec['size_gb']:g}", vram=f"{spec['vram_gb']:g}")]
        if not live_engine.model_installed(str(model)):
            parts.append(t("settings.model_not_installed"))
        if vram_gb <= 0:
            parts.append(t("settings.model_need_cpu"))
        elif vram_gb + 0.4 < spec["vram_gb"]:
            parts.append(t("settings.model_need_low_vram").format(
                have=f"{vram_gb:.1f}", need=f"{spec['vram_gb']:g}"))
        free_gb = self._pipeline_free_gb()
        if free_gb is not None and free_gb < spec["size_gb"] * 1.3:
            parts.append(t("settings.model_need_low_disk").format(
                free=f"{free_gb:.1f}", need=f"{spec['size_gb']:g}"))
        self.asr_hint.setText(" ".join(parts))

    @staticmethod
    def _pipeline_free_gb() -> float | None:
        """引擎目录所在盘剩余空间（GB）；拿不到返回 None。"""
        import shutil as _shutil

        from .config import find_subtitle_pipeline_dir

        target = find_subtitle_pipeline_dir() or APP_DIR
        try:
            return _shutil.disk_usage(str(target)).free / (1024 ** 3)
        except Exception:
            return None

    def _refresh_live_diagnostics(self) -> None:
        from . import live_engine

        state = live_engine.state()
        hardware = live_engine.hardware_snapshot()
        running = live_engine.alive()
        matched = live_engine.matches()
        gpu = hardware.get("gpu") or "未检测到独立 GPU"
        vram = hardware.get("vram_mb", 0)
        used = hardware.get("used_mb", 0)
        configured_model = str(settings["live_asr_model"])
        effective_model = live_engine.effective_model()
        recommended = live_engine.recommended_model()
        self.live_diag_status.setText(
            f"引擎：{'运行中' if running else '未运行'}　"
            f"配置：{'匹配' if matched else '不匹配'}　"
            f"运行模型：{state.get('model', '-')}　"
            f"配置：{configured_model} / 实际：{effective_model} / 推荐：{recommended}　"
            f"GPU：{gpu}（{vram} MB，已用 {used} MB）"
        )
        job = live_engine.control_job()
        self.live_diag_task.setText(
            f"当前任务：{job.get('mode', '-')}　"
            f"媒体：{Path(job.get('media', '-')).name if job.get('media') else '-'}　"
            f"起点：{job.get('seek', 0)} 秒"
        )
        paths = live_engine.paths()
        if paths is None:
            self.live_diag_log.clear()
            return
        try:
            lines = [
                line for line in paths[0].read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines() if line.strip()
            ]
            self.live_diag_log.setPlainText("\n".join(lines[-12:]))
        except Exception:
            self.live_diag_log.clear()

    def _restart_live_engine(self) -> None:
        from . import live_engine

        live_engine.kill()
        live_engine.start_preload()
        self._refresh_live_diagnostics()

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

    def _browse_asr_dir(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        start = self.edit_asr_dir.text() or str(APP_DIR)
        d = QFileDialog.getExistingDirectory(self, t("settings.pick_subtitle_dir"), start)
        if d:
            self.edit_asr_dir.setText(d)
            self._set("live_asr_dir", d)

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

    _WHEEL_CLASSES = None  # 延迟解析，避免模块顶部额外导入

    def _install_wheel_filter(self) -> None:
        """禁止设置控件响应滚轮；两个只读上下文窗口保持原生滚动。"""
        from PySide6.QtWidgets import (
            QAbstractSlider,
            QAbstractButton,
            QComboBox,
            QDoubleSpinBox,
            QSpinBox,
        )

        if SettingsDialog._WHEEL_CLASSES is None:
            SettingsDialog._WHEEL_CLASSES = (
                QAbstractButton, QComboBox, QSlider, QSpinBox,
                QDoubleSpinBox, QAbstractSlider,
            )
        seen = set()
        for cls in SettingsDialog._WHEEL_CLASSES:
            for widget in self.findChildren(cls):
                if widget in seen:
                    continue
                seen.add(widget)
                widget.setFocusPolicy(Qt.StrongFocus)
                widget.installEventFilter(self)  # 同一过滤器重复安装是幂等的

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self._install_wheel_filter()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Wheel and SettingsDialog._WHEEL_CLASSES \
                and isinstance(watched, SettingsDialog._WHEEL_CLASSES):
            # 控件自身不响应滚轮，但设置页仍应正常滚动。
            scroll = getattr(self, "_scroll_area", None)
            if scroll is not None:
                delta = event.angleDelta().y() or event.angleDelta().x()
                bar = scroll.verticalScrollBar()
                bar.setValue(bar.value() - int(delta))
            return True
        return super().eventFilter(watched, event)

    _INSTALL_MODEL_HINTS = {
        "qwen": "Qwen3-ASR-1.7B（推荐）：模型 4.7GB，建议 6GB 以上显存；52 种语言 + 22 种中文方言，"
                "自带语种识别。实测中/英零错误、日语错误率最低，数字与专有名词也最稳。"
                "无独显时可跑 CPU 但很慢。",
        "sensevoice": "SenseVoice-small：模型 0.9GB，2GB 显存即可（CPU 也能实时）；"
                      "中文/粤语/日语/韩语快而准，英语明显较弱。适合低配机器或纯中文内容。",
        "large-v3": "Whisper large-v3：模型 5.8GB，建议 8GB 以上显存；99 种语言覆盖最广，"
                    "加载约 40 秒，中文数字偶有错误。",
        "medium": "Whisper medium：模型 1.5GB，4GB 显存；通用均衡款，速度约 8 倍实时。",
        "small": "Whisper small：模型 0.5GB，2GB 显存即可（无独显也能用）；识别较粗，个别词会错。",
    }
    _INSTALL_TRANSLATE_HINTS = {
        "none": "不安装翻译模型：只出原文字幕，最省空间和时间。",
        "qwen3:8b": "qwen3:8b 5.2GB（推荐）：实测口语化最自然，习语意译到位"
                    "（Cut me some slack → 行行行，给我点面子吧）；已关思考模式，每句约 0.3s。",
        "translategemma:4b": "translategemma:4b 3.3GB：谷歌翻译专精，日译最稳、体积最小，"
                             "英语习语略偏直译。显存紧张时选它。",
        "qwen2.5:7b": "qwen2.5:7b 3.8GB：旧默认，通用可靠，个别习语仍会直译。",
        "qwen2.5:3b": "qwen2.5:3b 2.1GB：最省资源，日常对话够用，专业术语一般。",
        "aya-expanse:8b": "aya-expanse:8b 5.1GB（Cohere）：多语种覆盖好，但中文偏直译。",
    }

    def _update_install_hint(self) -> None:
        """安装项提示：模型/翻译说明 + 本机显存磁盘是否够 + 总下载量。"""
        from . import live_engine

        model = str(self.install_model.currentData() or "")
        translate = str(self.install_translate.currentData() or "none")
        parts = [self._INSTALL_MODEL_HINTS.get(model, ""),
                 self._INSTALL_TRANSLATE_HINTS.get(translate, "")]

        spec = ASR_MODEL_SPECS.get(model)
        tr_spec = TRANSLATE_MODEL_SPECS.get(translate, {"size_gb": 0.0, "vram_gb": 0.0})
        total_gb = (spec["size_gb"] if spec else 0.0) + tr_spec["size_gb"]
        # 首次安装还要装 venv 依赖（torch+CUDA 运行库约 4GB）
        deps_gb = 4.0 if model in ("qwen", "sensevoice") else 1.5
        parts.append(f"本次需下载约 {total_gb + deps_gb:.1f}GB"
                     f"（模型 {total_gb:.1f}GB + 运行环境 {deps_gb:g}GB），"
                     f"已装过的部分自动跳过。")

        hardware = live_engine.hardware_snapshot()
        vram_gb = hardware.get("vram_mb", 0) / 1024.0
        if spec is not None:
            need_both = spec["vram_gb"] + tr_spec["vram_gb"]
            if vram_gb <= 0:
                parts.append("未检测到 NVIDIA 显卡：将以 CPU 模式安装运行（慢，建议选 SenseVoice/small）。")
            elif vram_gb + 0.4 < spec["vram_gb"]:
                parts.append(f"⚠ 本机显存 {vram_gb:.1f}GB 低于建议 {spec['vram_gb']:g}GB，"
                             f"可能加载失败或极慢。")
            elif tr_spec["vram_gb"] > 0 and vram_gb < need_both:
                parts.append(f"提示：识别 + 翻译同时常驻约需 {need_both:g}GB 显存，"
                             f"本机 {vram_gb:.1f}GB——翻译模型会部分转到内存运行（略慢，可用）。")
        free_gb = self._pipeline_free_gb()
        if free_gb is not None and free_gb < total_gb + deps_gb:
            parts.append(f"⚠ 引擎所在盘仅剩 {free_gb:.1f}GB，不足本次下载量。")
        self.install_hint.setText(" · ".join(p for p in parts if p))

    @staticmethod
    def _installer_python():
        """install_engine.py 需要真 Python 解释器来搭建引擎 venv。

        源码运行时就是 sys.executable；打包版里 sys.executable 是播放器
        自己（拿它启动等于再开一个播放器，安装永远不执行），改从 PATH 找。
        返回 (程序, 参数前缀) 或 None。
        """
        import os
        import shutil
        import subprocess
        import sys

        if not getattr(sys, "frozen", False):
            return sys.executable, []
        cands = [c for c in (shutil.which("python"), shutil.which("python3"),
                             shutil.which("py")) if c]
        # Win10 1903+ 默认在 WindowsApps 放 python.exe 别名 stub：没装 Python 的
        # 机器上 which 同样命中它，启动只会弹 Microsoft Store 再秒退（界面只显示
        # "安装失败"，新加的"未找到 Python"指引反而永远走不到）。真解释器优先，
        # stub 排到最后，并逐个用 -c 验一次是不是真能跑
        cands.sort(key=lambda c: "\\windowsapps\\" in c.lower())
        for cand in cands:
            if not os.path.basename(cand).lower().startswith(("python", "py")):
                continue
            prefix = ["-3"] if cand.lower().endswith("py.exe") else []
            try:
                done = subprocess.run(
                    [cand, *prefix, "-c", "import sys"],
                    capture_output=True, timeout=6,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:  # noqa: BLE001 - 超时/无法执行都算不可用
                continue
            if done.returncode == 0:
                return cand, prefix
        return None, []

    def _start_install(self) -> None:
        from PySide6.QtCore import QProcess

        from . import live_engine
        from .config import find_subtitle_pipeline_dir

        if getattr(self, "_install_proc", None) is not None \
                and self._install_proc.state() != QProcess.NotRunning:
            return
        # engine dir: existing engine (if any) else default next to the player
        pipe = find_subtitle_pipeline_dir()
        if pipe is None:
            pipe = Path(__file__).resolve().parent.parent / "live-subtitle"
        script = pipe / "install_engine.py"
        if not script.is_file():
            self.install_status.setText(t("settings.install_no_script"))
            return

        proc = QProcess(self)
        self._install_proc = proc
        self.install_log.clear()
        self.btn_install.setEnabled(False)
        self.install_status.setText(t("settings.install_running"))

        def on_stdout() -> None:
            self.install_log.appendPlainText(
                bytes(proc.readAllStandardOutput()).decode("utf-8", "replace").strip()
            )

        def on_stderr() -> None:
            self.install_log.appendPlainText(
                bytes(proc.readAllStandardError()).decode("utf-8", "replace").strip()
            )

        def on_finished(code: int, _s) -> None:
            self.btn_install.setEnabled(True)
            ok = code == 0
            self.install_status.setText(
                t("settings.install_done") if ok else t("settings.install_failed")
            )
            self.install_status.setStyleSheet("color:#5dc0f0;" if ok else "color:#e0653f;")
            if ok:
                # 装完必须作废 effective_model 缓存：它按设置项缓存、不含文件系统
                # 状态，不清会一直返回安装前的回退档位（刚装好的 qwen 用不上，
                # 引擎按 whisper 起直接 MODEL_ERROR）
                live_engine.invalidate_model_cache()
            self._refresh_subtitle_status()

        proc.readyReadStandardOutput.connect(on_stdout)
        proc.readyReadStandardError.connect(on_stderr)
        proc.finished.connect(on_finished)

        exe, prefix = self._installer_python()
        if exe is None:
            self.install_status.setText(
                t("settings.install_no_python"))
            self.install_status.setStyleSheet("color:#e0653f;")
            self.btn_install.setEnabled(True)  # 进程没起来，on_finished 不会来
            return
        args = [*prefix, str(script), "--dir", str(pipe),
                "--model", str(self.install_model.currentData() or "qwen"),
                "--mirror", self.install_mirror.currentData() or "huggingface",
                "--translate", str(self.install_translate.currentData() or "none")]
        proc.start(exe, args)

    def _start_install_llama(self) -> None:
        """一键安装 HY-MT2-30B SRT 翻译（llama.cpp + 11.6GB 模型，幂等）。"""
        from PySide6.QtCore import QProcess

        from .config import find_subtitle_pipeline_dir

        if getattr(self, "_install_proc", None) is not None                 and self._install_proc.state() != QProcess.NotRunning:
            return
        pipe = find_subtitle_pipeline_dir()
        if pipe is None:
            pipe = Path(__file__).resolve().parent.parent / "live-subtitle"
        script = pipe / "install_engine.py"
        if not script.is_file():
            self.install_status.setText(t("settings.install_no_script"))
            return
        proc = QProcess(self)
        self._install_proc = proc
        self.install_log.clear()
        self.btn_install.setEnabled(False)
        self.btn_install_llama.setEnabled(False)
        self.install_status.setText(t("settings.install_running"))

        def on_stdout() -> None:
            self.install_log.appendPlainText(
                bytes(proc.readAllStandardOutput()).decode("utf-8", "replace").strip()
            )

        def on_stderr() -> None:
            self.install_log.appendPlainText(
                bytes(proc.readAllStandardError()).decode("utf-8", "replace").strip()
            )

        def on_finished(code: int, _s) -> None:
            self.btn_install.setEnabled(True)
            self.btn_install_llama.setEnabled(True)
            ok = code == 0
            self.install_status.setText(
                t("settings.install_done") if ok else t("settings.install_failed")
            )
            self.install_status.setStyleSheet("color:#5dc0f0;" if ok else "color:#e0653f;")
            self._refresh_subtitle_status()

        proc.readyReadStandardOutput.connect(on_stdout)
        proc.readyReadStandardError.connect(on_stderr)
        proc.finished.connect(on_finished)
        exe, prefix = self._installer_python()
        if exe is None:
            self.install_status.setText(t("settings.install_no_python"))
            self.install_status.setStyleSheet("color:#e0653f;")
            # 进程没起来 → on_finished 永不触发，按钮会永久灰掉（文案却写着
            # "装好后再点一次安装"，实际必须关掉设置窗重开）
            self.btn_install.setEnabled(True)
            self.btn_install_llama.setEnabled(True)
            return
        proc.start(exe, [*prefix, str(script), "--dir", str(pipe), "--llamacpp-only"])

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
            # 不把 parent 传给 QDialog：Windows 会把有父的顶层窗口作为
            # owned window，最小化主窗口时被一起带下去。无 parent 的
            # Qt.Window 独立参与任务栏，两边可各自最小化。parent 参数
            # 仍保留（兼容调用方），只是不再作为窗口属主。
            dlg = cls()
            cls._instance = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg
