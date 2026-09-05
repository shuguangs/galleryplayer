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
from PySide6.QtGui import QPalette
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
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class FlowLayout(QLayout):
    """按钮空间不足时自动换行，避免窗口缩窄后控件被吞掉。"""

    def __init__(self, parent=None, margin=0, spacing=6, align_right=False):
        super().__init__(parent)
        self._items = []
        self._align_right = bool(align_right)
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
        """一行排开的宽度作为建议值：宽窗口下不无谓换行。"""
        margins = self.contentsMargins()
        width = 0
        height = 0
        for i, item in enumerate(self._items):
            hint = item.sizeHint()
            width += hint.width() + (self.spacing() if i else 0)
            height = max(height, hint.height())
        return QSize(width + margins.left() + margins.right(),
                     height + margins.top() + margins.bottom())

    def minimumSize(self):
        """最窄只需容纳最宽的一个控件——其余靠换行，不再顶宽整页。"""
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _lines(self, area_width):
        """把控件切成若干行，返回 [(行内 (item, 宽) 列表, 行宽, 行高), ...]。"""
        lines = []
        cur, cur_w, cur_h = [], 0, 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            width = min(hint.width(), area_width)
            add_w = width + (spacing if cur else 0)
            if cur and cur_w + add_w > area_width:
                lines.append((cur, cur_w, cur_h))
                cur, cur_w, cur_h = [], 0, 0
                add_w = width
            cur.append((item, width))
            cur_w += add_w
            cur_h = max(cur_h, hint.height())
        if cur:
            lines.append((cur, cur_w, cur_h))
        return lines

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        spacing = self.spacing()
        y = area.y()
        total = y
        lines = self._lines(max(1, area.width()))
        for index, (cells, line_w, line_h) in enumerate(lines):
            if index:
                y += spacing
            x = area.x()
            if self._align_right and line_w < area.width():
                x += area.width() - line_w  # 靠右：与旧 addStretch 行为一致
            for item, width in cells:
                if not test_only:
                    item.setGeometry(QRect(QPoint(x, y), QSize(width, line_h)))
                x += width + spacing
            y += line_h
            total = y
        return total - rect.y() + margins.bottom()


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


# 行首标签宽度：对齐用的"建议宽"，但最小宽必须小于它，否则每一行都
# 把整页最小宽顶高 150px，窄窗口下右侧控件被推出滚动视口（实测）。
_CAP_HINT_W = 150
_CAP_MIN_W = 84


class _CapLabel(QLabel):
    """建议宽 150（列对齐）+ 最小宽 84（可压缩）的行首标签。"""

    def sizeHint(self):  # noqa: N802
        hint = super().sizeHint()
        return QSize(max(hint.width(), _CAP_HINT_W), hint.height())

    def minimumSizeHint(self):  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), _CAP_MIN_W), hint.height())


class _FlexLabel(QLabel):
    """网格里的字段标签：可换行，最小宽封顶 84。

    普通 QLabel 即便开了 wordWrap，最小宽仍是"最长单词"宽（英文
    "Translation target" ≈ 132px）。几列这样的标签叠加就能把分组最小宽
    顶过窗口宽度，右侧下拉/按钮被推出滚动视口。
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)

    def minimumSizeHint(self):  # noqa: N802
        hint = super().minimumSizeHint()
        width = min(hint.width(), _CAP_MIN_W)
        return QSize(width, max(hint.height(), self.heightForWidth(width)))


def _flow_row(*widgets: QWidget, spacing: int = 8, align_right: bool = False) -> QWidget:
    """按钮组容器：窄窗口自动换行，且容器高度跟着换行增长。

    普通 QWidget 的 sizePolicy 不声明 heightForWidth，父布局按 sizeHint
    高度（单行）给高——换行后的第二行被切在容器外，按钮"消失"（实测）。
    """
    from PySide6.QtWidgets import QSizePolicy

    box = QWidget()
    lay = FlowLayout(box, spacing=spacing, align_right=align_right)
    for w in widgets:
        lay.addWidget(w)
    policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    policy.setHeightForWidth(True)
    box.setSizePolicy(policy)
    return box


class _WrapCheckBox(QCheckBox):
    """文字可换行的复选框。

    QCheckBox 不支持 wordWrap：它的最小宽 = 指示器 + 整句文字宽。本页有
    十来个整句说明式选项，英文界面下单个复选框最小宽可达 876px，直接把
    设置页内容最小宽顶到 984px——远超默认窗口 640，右侧按钮和下拉全被
    推出滚动视口（就是"按钮显示不全 / 改变窗口大小挡住按钮"）。

    这里自绘：指示器交给当前样式画（外观与原生一致），文字用 TextWordWrap
    自己排版，并向布局声明 heightForWidth，换行后行高自动增长。
    仍是真正的 QCheckBox——toggled/isChecked/setToolTip 等用法完全不变。
    """

    _MAX_MIN_TEXT_W = 160  # 最小宽里给文字留的上限

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        # 水平策略必须带 ShrinkFlag（Preferred/Ignored 才有），否则布局用
        # qSmartMinSize 取 max(minimumSizeHint, sizeHint) —— 自定义的小
        # 最小宽会被整句 sizeHint 盖掉（实测 MinimumExpanding 下英文仍是
        # 876px）。Preferred + heightForWidth 才真的允许压缩换行。
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _style_option(self):
        from PySide6.QtWidgets import QStyleOptionButton

        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        return opt

    def _decoration_width(self) -> int:
        """指示器 + 间距占宽（文字可用宽 = 控件宽 - 这个值）。"""
        from PySide6.QtWidgets import QStyle

        opt = self._style_option()
        style = self.style()
        return (style.pixelMetric(QStyle.PM_IndicatorWidth, opt, self)
                + style.pixelMetric(QStyle.PM_CheckBoxLabelSpacing, opt, self)
                + 2 * style.pixelMetric(QStyle.PM_FocusFrameHMargin, opt, self))

    def _indicator_height(self) -> int:
        from PySide6.QtWidgets import QStyle

        return self.style().pixelMetric(QStyle.PM_IndicatorHeight,
                                        self._style_option(), self)

    def _text_rect(self, width: int) -> QRect:
        if not self.text():
            return QRect()
        return self.fontMetrics().boundingRect(
            QRect(0, 0, max(1, width), 0), Qt.TextWordWrap, self.text())

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        text_h = self._text_rect(width - self._decoration_width()).height()
        # 下限用原生 sizeHint 高度：单行时与普通复选框一样高（含样式内边距），
        # 否则布局会把它压到 16px，比同页其它选项矮一截。
        return max(super().sizeHint().height(),
                   max(self._indicator_height(), text_h) + 2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """最小宽只保证指示器 + 一小段文字，其余交给换行。"""
        metrics = self.fontMetrics()
        words = [w for w in self.text().split() if w]
        longest = max((metrics.horizontalAdvance(w) for w in words), default=0)
        if longest <= 0 or longest > self._MAX_MIN_TEXT_W:
            # 中文无空格：整句算一个"词"，退回固定小宽度
            longest = min(metrics.horizontalAdvance("中文四字"),
                          self._MAX_MIN_TEXT_W)
        width = self._decoration_width() + min(longest, self._MAX_MIN_TEXT_W)
        return QSize(width, self.heightForWidth(width))

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QPainter
        from PySide6.QtWidgets import QStyle, QStyleOptionButton

        painter = QPainter(self)
        opt = self._style_option()
        # 只让样式画指示器：文字摘掉，避免样式按不换行裁剪
        indicator_opt = QStyleOptionButton(opt)
        indicator_opt.text = ""
        # 指示器与第一行文字对齐（多行时不要垂直居中到整块中间）
        first_line = max(self._indicator_height(), self.fontMetrics().height())
        indicator_opt.rect = QRect(0, 0, self.width(), first_line)
        self.style().drawControl(QStyle.CE_CheckBox, indicator_opt, painter, self)

        if not self.text():
            return
        left = self._decoration_width()
        painter.setPen(self.palette().color(
            QPalette.Normal if self.isEnabled() else QPalette.Disabled,
            QPalette.WindowText))
        painter.drawText(QRect(left, 0, max(1, self.width() - left), self.height()),
                         Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop,
                         self.text())


def _mini_field(label: str, widget: QWidget) -> QWidget:
    """紧凑的"小标签 + 小控件"组，作为 FlowLayout 的换行单元。"""
    from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    cap = QLabel(label)
    lay.addWidget(cap)
    widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    lay.addWidget(widget)
    return box


def _dir_row(label: str, edit: QLineEdit, *buttons: QPushButton) -> QWidget:
    """目录行：标签 + 竖排两行（输入框全宽 / 按钮横排靠右）。

    旧行结构（标签+输入+2 按钮挤一行 5 列网格）的最小宽 ~724px 超过
    窗口最小宽 520——按钮被滚动区裁掉完全点不到（实测复现）。竖排后
    groupbox 最小宽贴合窗口，窄窗口下输入框仍可用、按钮永不下岗。

    按钮行用 FlowLayout：窄窗口下按钮换行而不是被挤出视口。
    """
    from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 2, 0, 2)
    lay.setSpacing(8)
    cap = _CapLabel(label)
    cap.setWordWrap(True)
    cap.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    lay.addWidget(cap, 0, Qt.AlignTop)

    panel = QWidget()
    pl = QVBoxLayout(panel)
    pl.setContentsMargins(0, 0, 0, 0)
    pl.setSpacing(4)
    # 输入框最小宽不能跟着内容走（长路径会把整页顶宽）
    edit.setMinimumWidth(80)
    pl.addWidget(edit)
    for b in buttons:
        b.setFocusPolicy(Qt.NoFocus)
    pl.addWidget(_flow_row(*buttons, spacing=6, align_right=True))
    lay.addWidget(panel, 1)
    return wrap


def _row(label: str, widget: QWidget, hint: str = "") -> QWidget:
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 2, 0, 2)
    lay.setSpacing(8)
    cap = _CapLabel(label)
    cap.setWordWrap(True)
    lay.addWidget(cap)
    lay.addWidget(widget, 2 if not hint else 0)  # 无提示行：控件吃余量（拉伸自适应）
    if hint:
        h = QLabel(hint)
        h.setStyleSheet(f"color:{theme.TEXT_DIM};")
        # 不换行的提示 QLabel 最小宽 = 整段文字宽，是窄窗口超宽的隐形推手
        h.setWordWrap(True)
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
        # 设计下限；首次 show 时 _fit_minimum_width 会按内容真实最小宽抬高，
        # 保证"缩到最小"时所有按钮/下拉仍完整可见。
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
        # 横向滚动只作最后兜底：内容最小宽已能压到窗口最小宽以内
        # （见 _fit_minimum_width / _apply_text_wrap），正常不会出现。
        # 曾用 AlwaysOff——内容被静默裁掉，右侧按钮物理上点不到。
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            f"QScrollBar:vertical {{ background: {theme.BG_BASE}; width: 10px; margin: 0; }}"
            f"QScrollBar::handle:vertical {{ background: {theme.BG_HOVER};"
            f" border-radius: 5px; min-height: 30px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {theme.ACCENT_DIM}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}"
            f"QScrollBar:horizontal {{ background: {theme.BG_BASE}; height: 10px; margin: 0; }}"
            f"QScrollBar::handle:horizontal {{ background: {theme.BG_HOVER};"
            f" border-radius: 5px; min-width: 30px; }}"
            f"QScrollBar::handle:horizontal:hover {{ background: {theme.ACCENT_DIM}; }}"
            f"QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}"
            f"QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}"
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
        self.cb_resume = _WrapCheckBox(t("settings.resume_label"))
        self.cb_resume.setChecked(bool(settings["resume_enabled"]))
        self.cb_resume.toggled.connect(lambda v: self._set("resume_enabled", v))
        root.addWidget(self.cb_resume)

        self.cb_autoplay = _WrapCheckBox(t("settings.autoplay_label"))
        self.cb_autoplay.setChecked(bool(settings["autoplay_next"]))
        self.cb_autoplay.toggled.connect(lambda v: self._set("autoplay_next", v))
        root.addWidget(self.cb_autoplay)

        self.cb_native = _WrapCheckBox(t("settings.native_size_label"))
        self.cb_native.setChecked(bool(settings["open_native_size"]))
        self.cb_native.toggled.connect(lambda v: self._set("open_native_size", v))
        root.addWidget(self.cb_native)

        self.cb_scroll = _WrapCheckBox(t("settings.remember_scroll_label"))
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

        # ---- 字幕颜色 / 描边（正常字幕 + 实时字幕）：白色字幕在白色
        # 画面不可见（实测）——可改颜色；描边档位给反色描边（白字黑边）
        def _color_row(setting_key: str, outline_key: str, label: str,
                       on_change) -> QWidget:
            from PySide6.QtGui import QColor
            from PySide6.QtWidgets import QColorDialog

            box = QWidget()
            lay = QHBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)

            cur = str(settings[setting_key] or "#ffffff")

            def _pick() -> None:
                nonlocal cur
                c = QColorDialog.getColor(QColor(cur), self,
                                          t("settings.pick_sub_color"))
                if not c.isValid():
                    return
                cur = c.name()
                settings[setting_key] = cur
                btn.setStyleSheet(
                    f"QPushButton {{ background:{cur}; border:1px solid #888;"
                    f" border-radius:4px; min-width:36px; min-height:20px; }}")
                on_change()
                self._set(setting_key, cur)

            btn = QPushButton()
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(t("settings.pick_sub_color"))
            btn.setStyleSheet(
                f"QPushButton {{ background:{cur}; border:1px solid #888;"
                f" border-radius:4px; min-width:36px; min-height:20px; }}")
            btn.clicked.connect(_pick)
            lay.addWidget(btn)

            sp = QSpinBox()
            sp.setRange(0, 4)
            sp.setSpecialValueText(t("settings.outline_off"))
            sp.setValue(int(settings[outline_key] or 0))
            sp.setToolTip(t("settings.outline_hint"))
            sp.valueChanged.connect(lambda v: (self._set(outline_key, v),
                                               on_change()))
            lay.addWidget(sp)
            lay.addStretch(1)
            wrap = _row(label, box, t("settings.outline_label"))
            return wrap

        # 正常字幕（mpv）：改动立即作用于正在播放的视频
        def _apply_mpv_sub_style() -> None:
            for v in self._viewer_targets():
                v.video_view.apply_sub_color_style()

        root.addWidget(_color_row("sub_color", "sub_outline",
                                  t("settings.sub_color_label"), _apply_mpv_sub_style))

        # 实时字幕（覆盖层）
        def _apply_live_style() -> None:
            for v in self._viewer_targets():
                v.refresh_live_caption_style()

        root.addWidget(_color_row("live_caption_color", "live_caption_outline",
                                  t("settings.live_color_label"), _apply_live_style))

        self.cb_subvis = _WrapCheckBox(t("settings.sub_visible_label"))
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

        # ---- 截图保存路径（竖排两行：窄窗口按钮不裁剪）
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
        btn_browse = QPushButton(t("settings.browse_ellipsis"))
        btn_browse.clicked.connect(self._browse_shot_path)
        btn_open = QPushButton(t("settings.open"))
        btn_open.clicked.connect(self._open_shot_folder)
        btn_clear = QPushButton(t("settings.clear"))
        btn_clear.clicked.connect(lambda: (self.edit_shot_path.clear(),
                                           self._set("capture_path", "")))
        root.addWidget(_dir_row(t("settings.shot_path_label"),
                                self.edit_shot_path, btn_browse, btn_open, btn_clear))
        shot_hint = QLabel(t("settings.shot_path_hint"))
        shot_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        shot_hint.setWordWrap(True)
        root.addWidget(shot_hint)

        # ---- 压缩包解压缓存路径（竖排两行）
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
        btn_ab = QPushButton(t("settings.browse_ellipsis"))
        btn_ab.clicked.connect(self._browse_archive_path)
        btn_ac = QPushButton(t("settings.clear"))
        btn_ac.clicked.connect(lambda: (self.edit_archive_path.clear(),
                                        self._set("archive_cache", "")))
        root.addWidget(_dir_row(t("settings.archive_path_label"),
                                self.edit_archive_path, btn_ab, btn_ac))
        arch_hint = QLabel(t("settings.archive_path_hint"))
        arch_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        arch_hint.setWordWrap(True)
        root.addWidget(arch_hint)

        self.cb_archive_no_thumbs = _WrapCheckBox(t("settings.archive_no_thumbs_label"))
        self.cb_archive_no_thumbs.setChecked(bool(settings["archive_no_thumbs"]))
        self.cb_archive_no_thumbs.toggled.connect(lambda v: self._set("archive_no_thumbs", v))
        root.addWidget(self.cb_archive_no_thumbs)
        no_thumbs_hint = QLabel(t("settings.archive_no_thumbs_hint"))
        no_thumbs_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        root.addWidget(no_thumbs_hint)

        # ---- 缩略图网格（全局默认值：右键生成时的预填参数）
        from PySide6.QtWidgets import QGridLayout, QGroupBox

        tg = QGroupBox(t("settings.section_thumbgrid"))
        tg.setStyleSheet(
            f"QGroupBox {{ border:1px solid {theme.BORDER}; border-radius:6px;"
            f" margin-top:6px; padding-top:2px; }}"
            f"QGroupBox::title {{ subcontrol-origin:margin; left:10px;"
            f" padding:0 4px; color:{theme.TEXT_DIM}; }}"
        )
        tgl = QGridLayout(tg)
        tgl.setContentsMargins(8, 6, 8, 6)
        tgl.setSpacing(4)
        tgl.setColumnStretch(0, 0)
        tgl.setColumnStretch(1, 2)
        tgl.setColumnStretch(2, 1)
        tgl.setColumnStretch(3, 0)

        self.edit_thumbgrid_dir = QLineEdit()
        self.edit_thumbgrid_dir.setPlaceholderText(t("settings.thumbgrid_save_hint"))
        self.edit_thumbgrid_dir.setText(str(settings["thumbgrid_save_dir"] or "").strip())
        self.edit_thumbgrid_dir.editingFinished.connect(
            lambda: self._set("thumbgrid_save_dir",
                              self.edit_thumbgrid_dir.text().strip()))
        btn_tg = QPushButton(t("settings.browse_ellipsis"))
        btn_tg.setFocusPolicy(Qt.NoFocus)
        btn_tg.clicked.connect(self._browse_thumbgrid_dir)
        tg_dir_box = QHBoxLayout()
        tg_dir_box.setContentsMargins(0, 0, 0, 0)
        tg_dir_box.addWidget(self.edit_thumbgrid_dir, 1)
        tg_dir_box.addWidget(btn_tg)
        tg_dir_box.addWidget(QLabel())  # 占位对齐清除按钮
        tgl.addWidget(_FlexLabel(t("thumbgrid.save_dir")), 0, 0)
        tgl.addLayout(tg_dir_box, 0, 1, 1, 3)

        self.sp_tg_cols = QSpinBox()
        self.sp_tg_cols.setRange(1, 10)
        self.sp_tg_cols.setValue(int(settings["thumbgrid_cols"] or 5))
        self.sp_tg_cols.valueChanged.connect(
            lambda v: self._set("thumbgrid_cols", v))
        tgl.addWidget(_FlexLabel(t("thumbgrid.columns")), 1, 0)
        tgl.addWidget(self.sp_tg_cols, 1, 1)

        self.sp_tg_rows = QSpinBox()
        self.sp_tg_rows.setRange(1, 10)
        self.sp_tg_rows.setValue(int(settings["thumbgrid_rows"] or 5))
        self.sp_tg_rows.valueChanged.connect(
            lambda v: self._set("thumbgrid_rows", v))
        tgl.addWidget(_FlexLabel(t("thumbgrid.rows")), 1, 2)
        tgl.addWidget(self.sp_tg_rows, 1, 3)

        self.sp_tg_width = QSpinBox()
        self.sp_tg_width.setRange(80, 640)
        self.sp_tg_width.setSingleStep(20)
        self.sp_tg_width.setSuffix(" px")
        self.sp_tg_width.setValue(int(settings["thumbgrid_width"] or 160))
        self.sp_tg_width.valueChanged.connect(
            lambda v: self._set("thumbgrid_width", v))
        tgl.addWidget(_FlexLabel(t("thumbgrid.cell_width")), 2, 0)
        tgl.addWidget(self.sp_tg_width, 2, 1)

        self.cb_tg_fmt = QComboBox()
        self.cb_tg_fmt.addItem("JPG", "jpg")
        self.cb_tg_fmt.addItem("PNG", "png")
        _fi = self.cb_tg_fmt.findData(str(settings["thumbgrid_format"] or "jpg"))
        self.cb_tg_fmt.setCurrentIndex(max(0, _fi))
        self.cb_tg_fmt.currentIndexChanged.connect(
            lambda _i: self._set("thumbgrid_format", self.cb_tg_fmt.currentData()))
        tgl.addWidget(_FlexLabel(t("thumbgrid.format")), 2, 2)
        tgl.addWidget(self.cb_tg_fmt, 2, 3)

        self.sp_tg_quality = QSpinBox()
        self.sp_tg_quality.setRange(40, 100)
        self.sp_tg_quality.setValue(int(settings["thumbgrid_quality"] or 88))
        self.sp_tg_quality.setToolTip(t("thumbgrid.quality_tip"))
        self.sp_tg_quality.valueChanged.connect(
            lambda v: self._set("thumbgrid_quality", v))
        # 质量说明原先只挂在 spinbox 的 tooltip 上，标签是裸的——用户在
        # 标签上悬停什么也不出现，等于没实装。补一个可见的 `?` 说明入口。
        from .thumb_grid_options import _HelpDot as _TgHelpDot

        tg_quality_label = _FlexLabel(t("thumbgrid.quality"))
        tg_quality_label.setToolTip(t("thumbgrid.quality_tip"))
        tgl.addWidget(tg_quality_label, 3, 0)
        tg_q_box = QHBoxLayout()
        tg_q_box.setContentsMargins(0, 0, 0, 0)
        tg_q_box.setSpacing(4)
        tg_q_box.addWidget(self.sp_tg_quality, 1)
        tg_q_box.addWidget(_TgHelpDot(t("thumbgrid.quality_tip")))
        tgl.addLayout(tg_q_box, 3, 1)

        # 抓帧方式（多选）：与生成对话框共用同一个控件，这里存的是全局默认
        from .thumb_grid_options import _ModeMultiCombo, _modes_from_settings

        self.cb_tg_modes = _ModeMultiCombo()
        self.cb_tg_modes.setSelectedModes(_modes_from_settings())
        self.cb_tg_modes.changed.connect(
            lambda: self._set("thumbgrid_modes",
                              list(self.cb_tg_modes.selectedModes())))
        tg_modes_label = _FlexLabel(t("thumbgrid.modes"))
        tg_modes_label.setToolTip(t("thumbgrid.modes_hint"))
        tgl.addWidget(tg_modes_label, 3, 2)
        tg_m_box = QHBoxLayout()
        tg_m_box.setContentsMargins(0, 0, 0, 0)
        tg_m_box.setSpacing(4)
        tg_m_box.addWidget(self.cb_tg_modes, 1)
        tg_m_box.addWidget(_TgHelpDot(t("thumbgrid.modes_hint")))
        tgl.addLayout(tg_m_box, 3, 3)

        tg_modes_hint = QLabel(t("settings.thumbgrid_modes_hint"))
        tg_modes_hint.setWordWrap(True)
        tg_modes_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        tgl.addWidget(tg_modes_hint, 4, 0, 1, 4)

        tg_hint = QLabel(t("settings.thumbgrid_save_hint"))
        tg_hint.setWordWrap(True)
        tg_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        tgl.addWidget(tg_hint, 5, 0, 1, 4)
        root.addWidget(tg)

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
        # 列拉伸：窗口拉大时输入框/下拉跟随变宽（QGridLayout 默认列宽固定在
        # 初始 sizeHint——曾导致拉伸设置窗控件纹丝不动，实测复现）
        gg.setColumnStretch(0, 0)   # 标签列：固定
        gg.setColumnStretch(1, 2)   # 输入/下拉主列：吃掉余量
        gg.setColumnStretch(2, 1)
        gg.setColumnStretch(3, 0)
        gg.setColumnStretch(4, 0)

        # 引擎目录（竖排两行：输入框全宽 + 按钮行——单行 5 列结构最小宽
        # 724px 超出窗口最小宽，按钮被裁剪点不到）
        self.edit_subtitle_dir = QLineEdit()
        self.edit_subtitle_dir.setReadOnly(True)
        self.edit_subtitle_dir.setPlaceholderText(t("settings.subtitle_dir_placeholder"))
        self.edit_subtitle_dir.setToolTip(t("settings.subtitle_dir_hint"))
        self.edit_subtitle_dir.setText(str(settings["subtitle_pipeline_dir"] or "").strip())
        btn_sb = QPushButton(t("settings.browse_ellipsis"))
        btn_sb.clicked.connect(self._browse_subtitle_dir)
        btn_sc = QPushButton(t("settings.detect"))
        btn_sc.clicked.connect(self._detect_subtitle_dir)
        root.addWidget(_dir_row(t("settings.subtitle_dir_label"),
                                self.edit_subtitle_dir, btn_sb, btn_sc))
        self.sub_status = QLabel("")
        self.sub_status.setStyleSheet(f"color:{theme.TEXT_DIM};")
        root.addWidget(self.sub_status)
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
        # 这个下拉以前建好后忘了 addWidget——控件从未进入任何布局，
        # "识别语言"在设置页里完全看不到（settings 里只能手改 JSON）。
        # 放在识别引擎（第 2 行）之前的第 1 行。
        gg.addWidget(_CapLabel(t("settings.live_lang_label")), 1, 0)
        gg.addWidget(self.cb_live_lang, 1, 1, 1, 2)

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
        gg.addWidget(_FlexLabel(t("settings.srt_translate_label")), 14, 0)
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
        gg.addWidget(_FlexLabel(t("settings.translate_target_label")), 16, 0)
        gg.addWidget(self.cb_translate_target, 16, 1)

        self.cb_srt_format = QComboBox()
        for fmt in ("srt", "vtt", "ass"):
            self.cb_srt_format.addItem(fmt.upper(), fmt)
        idx = self.cb_srt_format.findData(str(settings["srt_export_format"]))
        self.cb_srt_format.setCurrentIndex(max(0, idx))
        self.cb_srt_format.currentIndexChanged.connect(
            lambda _i: self._set("srt_export_format", self.cb_srt_format.currentData()))
        gg.addWidget(_FlexLabel(t("settings.srt_format_label")), 16, 2)
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
        gg.addWidget(_FlexLabel(t("settings.idle_unload_label")), 17, 0)
        gg.addWidget(self.spin_idle_unload, 17, 1)

        # 内容场景提示词组：按片源类型微调翻译语气与术语策略。独占第 21 行
        # （旧 17 行 col2/3 与空闲释放并排——4 列一行是窄窗口超宽的主要来源）
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
        gg.addWidget(_FlexLabel(t("settings.translate_scenario_label")), 21, 0)
        gg.addWidget(self.cb_translate_scenario, 21, 1, 1, 4)

        # 实时字幕覆盖层：字号 + 覆盖范围（按播放区域百分比）
        # 三个值都必须回灌到已打开的播放器：字号走样式表，覆盖范围走
        # Viewer._relayout 的几何计算。旧版只写 settings 不通知 Viewer——
        # 用户改完字号"对实时字幕无效"，要重开播放器才生效（实测）。
        def _push_live_display() -> None:
            for v in self._viewer_targets():
                v.refresh_live_caption_style()

        self.spin_live_font = QSpinBox()
        self.spin_live_font.setRange(12, 96)
        self.spin_live_font.setValue(int(settings["live_caption_font_size"]))
        self.spin_live_font.valueChanged.connect(
            lambda v: (self._set("live_caption_font_size", int(v)),
                       _push_live_display()))
        self.spin_live_width = QSpinBox()
        self.spin_live_width.setRange(40, 100)
        self.spin_live_width.setSuffix("%")
        self.spin_live_width.setValue(int(settings["live_caption_width"]))
        self.spin_live_width.valueChanged.connect(
            lambda v: (self._set("live_caption_width", int(v)),
                       _push_live_display()))
        self.spin_live_height = QSpinBox()
        self.spin_live_height.setRange(8, 40)
        self.spin_live_height.setSuffix("%")
        self.spin_live_height.setValue(int(settings["live_caption_height"]))
        self.spin_live_height.valueChanged.connect(
            lambda v: (self._set("live_caption_height", int(v)),
                       _push_live_display()))
        for widget, key in (
            (self.spin_live_font, "live_caption_font_size"),
            (self.spin_live_width, "live_caption_width"),
            (self.spin_live_height, "live_caption_height"),
        ):
            widget.setToolTip(t("settings.live_caption_display_hint"))

        gg.addWidget(_FlexLabel(t("settings.live_asr_label")), 2, 0)
        gg.addWidget(self.cb_live_asr, 2, 1)
        gg.addWidget(_FlexLabel(t("settings.live_model_preset_label")), 13, 0)
        gg.addWidget(self.cb_live_preset, 13, 1)
        self.cb_hardware_model = _WrapCheckBox(t("settings.hardware_aware_model_label"))
        self.cb_hardware_model.setToolTip(t("settings.hardware_aware_model_hint"))
        self.cb_hardware_model.setChecked(bool(settings["hardware_aware_model"]))
        self.cb_hardware_model.toggled.connect(
            lambda value: self._set("hardware_aware_model", value)
        )
        gg.addWidget(self.cb_hardware_model, 13, 2, 1, 3)
        gg.addWidget(_FlexLabel(t("settings.live_translate_label")), 2, 2)
        gg.addWidget(self.cb_live_translate, 2, 3, 1, 2)
        # 所选引擎的资源需求 / 本机是否够（选择时即时更新）
        self.asr_hint = QLabel("")
        self.asr_hint.setWordWrap(True)
        self.asr_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        gg.addWidget(self.asr_hint, 18, 0, 1, 5)
        self._warn_model_resources(str(settings["live_asr_model"]))
        self._update_combo_resources()

        # 人声降噪：实时字幕（可选，默认关）与 SRT 生成（默认开）。
        # 独占行（19/20）——曾加在 4/5 行与"模型目录""来源/保存"撞格，
        # 控件叠放完全点不开（实测）。
        self.cb_live_denoise = _WrapCheckBox(t("settings.live_denoise_label"))
        self.cb_live_denoise.setChecked(bool(settings["live_caption_denoise"]))
        self.cb_live_denoise.setToolTip(t("settings.live_denoise_hint"))
        self.cb_live_denoise.toggled.connect(
            lambda v: self._set("live_caption_denoise", v))
        gg.addWidget(self.cb_live_denoise, 19, 0, 1, 5)
        self.cb_srt_denoise = _WrapCheckBox(t("settings.srt_denoise_label"))
        self.cb_srt_denoise.setChecked(bool(settings["srt_denoise"]))
        self.cb_srt_denoise.setToolTip(t("settings.srt_denoise_hint"))
        self.cb_srt_denoise.toggled.connect(
            lambda v: self._set("srt_denoise", v))
        gg.addWidget(self.cb_srt_denoise, 20, 0, 1, 5)

        # 模型目录（同引擎目录：竖排两行，窄窗口按钮不裁剪）
        self.edit_asr_dir = QLineEdit()
        self.edit_asr_dir.setReadOnly(True)
        self.edit_asr_dir.setPlaceholderText(t("settings.live_asr_dir_hint"))
        self.edit_asr_dir.setToolTip(t("settings.live_asr_dir_hint"))
        self.edit_asr_dir.setText(str(settings["live_asr_dir"] or ""))
        btn_ad = QPushButton(t("settings.browse_ellipsis"))
        btn_ad.clicked.connect(self._browse_asr_dir)
        btn_ac = QPushButton(t("settings.clear"))
        btn_ac.clicked.connect(lambda: (self.edit_asr_dir.clear(),
                                        self._set("live_asr_dir", "")))
        gg.addWidget(_dir_row(t("settings.live_asr_dir_label"),
                              self.edit_asr_dir, btn_ad, btn_ac), 4, 0, 1, 5)

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
        gg.addWidget(_FlexLabel(t("settings.live_source_label")), 5, 0)
        gg.addWidget(self.cb_live_source, 5, 1)
        # 保存位置挪到独立行 6（旧 5 行 col2/3 并排：4 列一行是窄窗口超宽来源）
        gg.addWidget(_FlexLabel(t("settings.subtitle_save_label")), 6, 0)
        gg.addWidget(self.cb_subtitle_save, 6, 1)

        self.cb_live_resident = _WrapCheckBox(t("settings.live_resident_label"))
        # 显存占用随实际引擎变化，不写死数字
        from .live_engine import model_label, vram_footprint_gb

        self.cb_live_resident.setToolTip(t("settings.live_resident_hint").format(
            model=model_label(), vram=f"{vram_footprint_gb():g}GB"))
        self.cb_live_resident.setChecked(bool(settings["live_caption_resident"]))
        self.cb_live_resident.toggled.connect(lambda v: self._set("live_caption_resident", v))
        gg.addWidget(self.cb_live_resident, 7, 0, 1, 5)

        self.cb_live_preload = _WrapCheckBox(t("settings.live_model_preload_label"))
        self.cb_live_preload.setToolTip(t("settings.live_model_preload_hint"))
        self.cb_live_preload.setChecked(bool(settings["live_model_preload"]))
        self.cb_live_preload.toggled.connect(lambda v: self._set("live_model_preload", v))
        gg.addWidget(self.cb_live_preload, 9, 0, 1, 6)
        preload_hint = QLabel(t("settings.live_model_preload_hint"))
        preload_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        preload_hint.setWordWrap(True)
        gg.addWidget(preload_hint, 10, 0, 1, 6)
        # 三个数值一行 6 列（标签+spin ×3）最小宽 389px，是本组第二大的顶宽
        # 来源；改成可换行的 3 组小控件，窄窗口自动折成多行、不再挤出视口。
        gg.addWidget(
            _flow_row(
                _mini_field(t("settings.live_caption_display_label"), self.spin_live_font),
                _mini_field(t("settings.live_caption_width_label"), self.spin_live_width),
                _mini_field(t("settings.live_caption_height_label"), self.spin_live_height),
                spacing=10,
            ),
            8, 0, 1, 6,
        )
        display_hint = QLabel(t("settings.live_caption_display_hint"))
        display_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        display_hint.setWordWrap(True)
        # 旧版和 preload_hint 同占 (10,0)——两个 QLabel 直接叠在一起，
        # 后加的把前一个盖掉（实测第 10 行只看得到一段文字）。独占 22 行。
        gg.addWidget(display_hint, 22, 0, 1, 6)

        self.slider_bilingual = QSlider(Qt.Horizontal)
        self.slider_bilingual.setRange(0, 100)
        self.slider_bilingual.setValue(int(float(settings["caption_bilingual_ratio"]) * 100))
        self.slider_bilingual.setToolTip(t("settings.caption_bilingual_hint"))
        self.slider_bilingual.valueChanged.connect(
            lambda v: self._set("caption_bilingual_ratio", v / 100.0)
        )
        gg.addWidget(_FlexLabel(t("settings.caption_bilingual_label")), 11, 0)
        gg.addWidget(self.slider_bilingual, 11, 1, 1, 4)

        self.edit_glossary = QLineEdit()
        self.edit_glossary.setPlaceholderText(t("settings.caption_glossary_hint"))
        self.edit_glossary.setText(self._format_glossary(settings["caption_glossary"]))
        self.edit_glossary.editingFinished.connect(self._save_glossary)
        gg.addWidget(_FlexLabel(t("settings.caption_glossary_label")), 12, 0)
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
        # 三个下拉竖排（一行一个）：旧 6 列单行结构最小宽 ~724px 超出窗口
        # 最小宽 520，右侧镜像下拉在窄窗口被裁掉（实测）
        ig.setColumnStretch(0, 0)
        ig.setColumnStretch(1, 1)
        ig.addWidget(_FlexLabel(t("settings.install_model_label")), 0, 0)
        ig.addWidget(self.install_model, 0, 1)
        ig.addWidget(_FlexLabel(t("settings.install_translate_label")), 1, 0)
        ig.addWidget(self.install_translate, 1, 1)
        ig.addWidget(_FlexLabel(t("settings.install_mirror_label")), 2, 0)
        ig.addWidget(self.install_mirror, 2, 1)

        self.install_hint = QLabel("")
        self.install_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        self.install_hint.setWordWrap(True)
        ig.addWidget(self.install_hint, 3, 0, 1, 2)

        self.btn_install = QPushButton(t("settings.install_start"))
        self.btn_install.setFocusPolicy(Qt.NoFocus)
        self.btn_install.clicked.connect(self._start_install)
        self.btn_install_llama = QPushButton(t("settings.install_llama"))
        self.btn_install_llama.setFocusPolicy(Qt.NoFocus)
        self.btn_install_llama.setToolTip(t("settings.install_llama_hint"))
        self.btn_install_llama.clicked.connect(self._start_install_llama)
        self.install_status = QLabel("")
        self.install_status.setWordWrap(True)
        self.install_status.setStyleSheet(f"color:{theme.TEXT_DIM};")
        # 按钮行：窄窗口自动换行（旧版并排放在两个网格格子里，窄窗口下
        # 第二个按钮被推出滚动视口点不到）。不设 maximumWidth——英文
        # 标题比中文长得多，限宽会把文字截断（实测 "Install SRT LLM
        # translate" 需要 314px，旧上限 160px）。
        ig.addWidget(_flow_row(self.btn_install, self.btn_install_llama, spacing=6),
                     4, 0, 1, 2)
        ig.addWidget(self.install_status, 5, 0, 1, 2)

        self.install_log = QPlainTextEdit()
        self.install_log.setReadOnly(True)
        self.install_log.setMaximumHeight(96)
        self.install_log.setStyleSheet(
            f"QPlainTextEdit {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:4px;"
            f" font-family:Consolas; font-size:11px; }}"
        )
        ig.addWidget(self.install_log, 6, 0, 1, 2)
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
        btn_diag_refresh = QPushButton(t("settings.live_diagnostics_refresh"))
        btn_diag_refresh.setFocusPolicy(Qt.NoFocus)
        btn_diag_refresh.clicked.connect(self._refresh_live_diagnostics)
        btn_diag_restart = QPushButton(t("settings.live_diagnostics_restart"))
        btn_diag_restart.setFocusPolicy(Qt.NoFocus)
        btn_diag_restart.clicked.connect(self._restart_live_engine)
        dg.addWidget(_flow_row(btn_diag_refresh, btn_diag_restart, spacing=6),
                     3, 0, 1, 4)
        root.addWidget(diag_grp)
        self._refresh_live_diagnostics()
        # ---- 文件关联
        root.addWidget(_section(t("settings.section_assoc")))
        self.btn_assoc = QPushButton(t("settings.assoc_register"))
        self.btn_assoc.setFocusPolicy(Qt.NoFocus)
        self.btn_assoc.clicked.connect(self._register_assoc)
        self.btn_unassoc = QPushButton(t("settings.assoc_unregister"))
        self.btn_unassoc.setFocusPolicy(Qt.NoFocus)
        self.btn_unassoc.clicked.connect(self._unregister_assoc)
        assoc_box = _flow_row(self.btn_assoc, self.btn_unassoc, spacing=8)
        root.addWidget(assoc_box)
        self.assoc_hint = QLabel("")
        self.assoc_hint.setWordWrap(True)
        self.assoc_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        root.addWidget(self.assoc_hint)
        self._refresh_assoc_hint()

        # ---- 诊断日志导出（启动日志：排查"打开软件白屏/未响应"用）
        btn_export_log = QPushButton(t("settings.export_log"))
        btn_export_log.setFocusPolicy(Qt.NoFocus)
        btn_export_log.setToolTip(t("settings.export_log_hint"))
        btn_export_log.clicked.connect(self._export_diagnostic_log)
        export_hint = QLabel(t("settings.export_log_hint"))
        export_hint.setWordWrap(True)
        export_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        root.addWidget(_flow_row(btn_export_log, spacing=8))
        root.addWidget(export_hint)

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

        # 全部控件构建完成：应用下拉弹性策略（曾放在安装组后面——
        # 之后创建的诊断/关联等组的下拉漏掉，且 widget 进滚动区前的
        # 策略在部分布局路径下不生效，实测安装下拉恒 160px）
        self._apply_text_wrap()
        self._apply_flex_policies()

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
        from .config import find_subtitle_source_dir

        # 没装过引擎时查安装目标所在盘（exe 旁/手动指定），不是程序目录
        target = (find_subtitle_pipeline_dir()
                  or find_subtitle_source_dir() or APP_DIR)
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

    def _browse_thumbgrid_dir(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        start = self.edit_thumbgrid_dir.text() or str(APP_DIR)
        d = QFileDialog.getExistingDirectory(
            self, t("thumbgrid.save_dir"), start)
        if d:
            self.edit_thumbgrid_dir.setText(d)
            self._set("thumbgrid_save_dir", d)

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
        # 控件 polish 之后才有可信的 minimumSizeHint（构造期偏大近一倍），
        # 所以最小宽在这里定；语言切换后重开也会重新贴合。
        self._fit_minimum_width()

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
        from .config import find_subtitle_pipeline_dir, find_subtitle_source_dir

        if getattr(self, "_install_proc", None) is not None \
                and self._install_proc.state() != QProcess.NotRunning:
            return
        # engine dir: existing engine (if any) else live-subtitle 源目录
        # （exe 旁/手动指定，不要求 venv——安装恰恰发生在 venv 之前；
        # 旧代码的 __file__ 相对回退在打包版指向 _internal/，永远找不到）
        pipe = find_subtitle_pipeline_dir() or find_subtitle_source_dir()
        if pipe is None:
            self.install_status.setText(t("settings.install_no_script"))
            return
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

        from .config import (find_subtitle_pipeline_dir,
                             find_subtitle_source_dir)

        if getattr(self, "_install_proc", None) is not None                 and self._install_proc.state() != QProcess.NotRunning:
            return
        pipe = find_subtitle_pipeline_dir() or find_subtitle_source_dir()
        if pipe is None:
            self.install_status.setText(t("settings.install_no_script"))
            return
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

    def _export_diagnostic_log(self) -> None:
        """把诊断日志打包成 zip 放到播放器根文件夹。

        日志平时一直在自动写（userdata/logs/startup_*.log，每次启动一份），
        白屏/未响应发生时文件已经在盘上——这里只是把最近 5 份启动日志 +
        引擎日志 + 环境信息打成 zip，落在播放器根目录，方便直接发回去。
        """
        import platform
        import zipfile
        from datetime import datetime
        from PySide6.QtWidgets import QMessageBox

        from . import startup_log
        from .config import find_subtitle_pipeline_dir
        from .runtime import USERDATA_DIR

        zip_path = APP_DIR / f"播放器诊断日志_{datetime.now():%Y%m%d_%H%M%S}.zip"
        candidates: list = startup_log.recent_logs(5)
        candidates.append(USERDATA_DIR / "last_playlist.json")
        pipe = find_subtitle_pipeline_dir()
        if pipe is not None:
            candidates += [
                pipe / "live-caption.log",
                pipe / "live-caption.err",
                pipe / "live-caption.state",
            ]
        written = 0
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for src in candidates:
                    if src.is_file():
                        try:
                            zf.write(src, src.name)
                            written += 1
                        except OSError:
                            pass
                zf.writestr("环境信息.txt",
                            f"platform: {platform.platform()}\n"
                            f"python: {platform.python_version()}\n"
                            f"frozen: {bool(getattr(sys, 'frozen', False))}\n"
                            f"exe: {sys.executable}\n"
                            f"导出时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                            f"包含文件数: {written}\n")
            QMessageBox.information(
                self, t("settings.export_log"),
                t("settings.export_log_done").format(path=zip_path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, t("settings.export_log"),
                t("settings.export_log_failed").format(err=exc))

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

    def _fit_minimum_width(self) -> None:
        """窗口最小宽跟随内容真实最小宽，永不让控件被挤出可视区。

        布局能压缩到什么程度有硬下限（按钮文字、SpinBox 数字都要完整显示）。
        与其把窗口最小宽写死 520、让内容溢出后靠水平滚动"藏"住右侧按钮，
        不如让窗口最小宽 = 内容最小宽 + 边距：用户最多只能缩到"刚好全部
        可见"，再拖也不会遮住按钮。宽度足够时布局照常自适应。

        必须在首次 show 之后调用：控件 polish（样式表生效）前 minimumSizeHint
        偏大得多（实测英文 1130 vs 508），构造期取值会把最小宽定成两倍。
        """
        scroll = self._scroll_area
        content = scroll.widget()
        margins = self.layout().contentsMargins()
        chrome = margins.left() + margins.right() + 2 * scroll.frameWidth()
        # 竖向滚动条常驻（内容一定超高），它的宽度也要算进去
        bar = scroll.verticalScrollBar().sizeHint().width()
        needed = content.minimumSizeHint().width() + chrome + bar
        # 520 是原有的设计下限（再窄纯属难用）；内容需要更宽时以内容为准。
        needed = max(520, needed)
        if needed == self.minimumWidth():
            return
        self.setMinimumWidth(needed)
        if self.width() < needed:
            self.resize(needed, self.height())

    def _apply_text_wrap(self) -> None:
        """所有说明性 QLabel 一律允许换行。

        不换行的 QLabel 最小宽 = 整句像素宽。本页有几十条整句说明，
        英文界面下单条可达 900px，把内容区最小宽顶到 914px——默认 640
        窗口里右侧按钮/下拉直接被推出滚动视口。行首标签（_CapLabel）和
        分节标题另有自己的宽度策略，跳过。
        """
        for lab in self.findChildren(QLabel):
            if isinstance(lab, _CapLabel) or lab.wordWrap() or not lab.text():
                continue
            lab.setWordWrap(True)

    def _viewer_targets(self) -> list:
        """当前打开的 Viewer 实例（字幕样式/字号实时生效用）。

        主窗口持有 viewer（懒创建）；设置对话框刻意不带 parent（独立窗口），
        所以从 QApplication 的顶层窗口里找主窗口再取它的 viewer。

        返回的是 Viewer 而不是 Viewer.video_view：实时字幕覆盖层挂在 Viewer
        上，mpv 字幕在 video_view 上，两边都要能拿到（旧版只返回 video_view，
        于是 `v._apply_live_caption_style()` 必然 AttributeError，实时字幕的
        颜色/描边/字号在设置页里改完毫无反应）。
        """
        from PySide6.QtWidgets import QApplication

        targets = []
        for w in QApplication.topLevelWidgets():
            viewer = getattr(w, "viewer", None)
            if viewer is not None and hasattr(viewer, "refresh_live_caption_style"):
                targets.append(viewer)
        return targets

    def _apply_flex_policies(self) -> None:
        """下拉框弹性策略：Expanding（拉大窗口时吃满列宽）+ 可压缩最小宽。

        QComboBox 默认水平 sizePolicy 是 Preferred——列拉伸设了它也不主动
        占满格子（实测 920 宽窗口安装下拉恒 160px，选项文字显示不全）。

        最小宽不能设 200：一行两个下拉的行最小宽 ≈ 标签+200+标签+200
        ≈ 550px，直接把整页内容最小宽顶到 750px 以上——默认 640 窗口右侧
        控件和按钮全被推出滚动视口（实测复现，用户反馈的"按钮显示不全"）。
        改为 AdjustToMinimumContentsLength + 最小宽 108：窄窗口文字自动省略
        号截断（有 tooltip 兜底），宽窗口仍由 Expanding 吃满列宽。
        """
        from PySide6.QtWidgets import QComboBox, QSizePolicy

        for c in self.findChildren(QComboBox):
            c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            c.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            c.setMinimumContentsLength(6)
            c.setMinimumWidth(108)
            if not c.toolTip():
                # 无专属说明的下拉：tooltip 显示当前选项全文（窄窗口被省略号
                # 截断时仍可读），并随选择变化更新。已有说明的不覆盖。
                c.setToolTip(c.currentText())
                c.currentTextChanged.connect(c.setToolTip)

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
