"""缩略图网格的可复用参数控件。

三处宿主共用这里的控件，避免同一套 UI 写三遍后互相漂移：
- ThumbGridDialog（右键「生成缩略图网格」的模态对话框）
- ToolsDialog 的缩略图网格二级页面（内嵌，不再弹第三层窗口）
- SettingsDialog（只用 _ModeMultiCombo + _HelpDot，作为全局默认）

包含：
- `_HelpDot`：`?` 小圆点按钮，点击弹出带正文的说明气泡。原先「质量」
  这类长说明只挂在 spinbox 的 tooltip 上，而控件还藏在折叠的高级选项
  里——用户在标签上悬停什么也不出现，等于没实装。
- `_ModeMultiCombo`：勾选式多选下拉（收起时显示「已选 N 种」），每项
  带自己的说明 tooltip。
- `ThumbGridOptionsWidget`：保存位置 + 行列 + 抓帧方式 + 各模式参数 +
  高级折叠（宽度/格式/质量/冲突策略），产出 GridOptions。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme
from .config import settings
from .i18n import t
from .thumb_grid import (
    ALL_MODES,
    MODE_COVER,
    MODE_EVEN,
    MODE_EXACT,
    MODE_INTERVAL,
    MODE_RANDOM,
    MODE_RANGE,
    MODE_TRIM,
    GridOptions,
    parse_time,
    parse_time_list,
)

_MODE_LABEL = {
    MODE_EVEN: "thumbgrid.mode_even",
    MODE_TRIM: "thumbgrid.mode_trim",
    MODE_INTERVAL: "thumbgrid.mode_interval",
    MODE_RANGE: "thumbgrid.mode_range",
    MODE_RANDOM: "thumbgrid.mode_random",
    MODE_EXACT: "thumbgrid.mode_exact",
    MODE_COVER: "thumbgrid.mode_cover",
}
_MODE_TIP = {
    MODE_EVEN: "thumbgrid.mode_even_tip",
    MODE_TRIM: "thumbgrid.mode_trim_tip",
    MODE_INTERVAL: "thumbgrid.mode_interval_tip",
    MODE_RANGE: "thumbgrid.mode_range_tip",
    MODE_RANDOM: "thumbgrid.mode_random_tip",
    MODE_EXACT: "thumbgrid.mode_exact_tip",
    MODE_COVER: "thumbgrid.mode_cover_tip",
}


class _HelpBubble(QDialog):
    """无边框说明气泡：点空白/按 Esc 即关，不抢焦点流程。"""

    def __init__(self, text: str, title: str, parent=None) -> None:
        super().__init__(parent, Qt.Popup)
        self.setStyleSheet(
            f"QDialog {{ background:{theme.BG_PANEL};"
            f" border:1px solid {theme.BORDER}; border-radius:6px; }}"
            f"QLabel {{ color:{theme.TEXT}; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        head = QLabel(title)
        head.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:11px;")
        lay.addWidget(head)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setMaximumWidth(340)
        lay.addWidget(body)


class _HelpDot(QPushButton):
    """`?` 小圆点：可见的说明入口（tooltip 只在悬停时才有，容易被错过）。"""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__("?", parent)
        self._text = text
        self.setFixedSize(16, 16)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(text)          # 悬停也给，两条路都通
        self.setStyleSheet(
            f"QPushButton {{ background:{theme.BG_RAISED}; color:{theme.TEXT_DIM};"
            f" border:1px solid {theme.BORDER}; border-radius:8px;"
            f" font-size:10px; padding:0; }}"
            f"QPushButton:hover {{ color:{theme.TEXT}; border-color:{theme.ACCENT}; }}"
        )
        self.clicked.connect(self._popup)

    def helpText(self) -> str:
        return self._text

    def _popup(self) -> None:
        bubble = _HelpBubble(self._text, t("thumbgrid.help_title"), self)
        bubble.adjustSize()
        bubble.move(self.mapToGlobal(self.rect().bottomLeft()))
        bubble.show()


def labeled(label_text: str, widget: QWidget, help_text: str = "") -> QWidget:
    """标签 + 控件（+ 可选说明圆点）打包成一行，供网格布局单格放置。"""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lb = QLabel(label_text)
    if help_text:
        lb.setToolTip(help_text)       # 标签自己也要有 tooltip
    lay.addWidget(lb)
    lay.addWidget(widget, 1)
    if help_text:
        lay.addWidget(_HelpDot(help_text))
    return box


class _ModeMultiCombo(QComboBox):
    """勾选式多选下拉：收起时显示「已选 N 种」，展开是带说明的勾选列表。

    Qt 没有现成的多选 QComboBox。做法是给它一个可勾选的 item model，
    并拦截 item 的点击——默认行为是「选中即收起」，那样一次只能改一项。

    收起态的文字用 **paintEvent 自绘**，不能用 setEditable(True)+lineEdit：
    可编辑的 QComboBox 点击输入框根本不展开下拉（只有右侧箭头才会，而
    箭头又被 lineEdit 和样式表挤掉），结果是一项都勾不上——用户实测
    「无法修改抓帧方式」就是这个。自绘保留了非可编辑 combobox 的
    「点哪都能展开」。

    两个信号刻意分开：
    - changed：**用户**改了选择。宿主据此写回设置——预填默认值时不该
      触发它，否则一打开设置页就把默认值写脏（标记 dirty 并落盘）。
    - refreshed：选择集合发生任何变化（含程序预填）。宿主据此同步
      「只显示已勾选模式的参数行」这类纯展示逻辑。
    """

    changed = Signal()
    refreshed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        for mode in ALL_MODES:
            it = QStandardItem(t(_MODE_LABEL[mode]))
            it.setData(mode, Qt.UserRole)
            it.setToolTip(t(_MODE_TIP[mode]))
            it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            it.setCheckState(Qt.Unchecked)
            self._model.appendRow(it)
        self.setModel(self._model)
        self.view().viewport().installEventFilter(self)
        self._model.itemChanged.connect(self._on_item_changed)
        self.setToolTip(t("thumbgrid.modes_hint"))
        self._refresh_text()

    # 点击 item 时只切勾选状态，不关闭下拉（多选的关键）
    def eventFilter(self, obj, event):  # noqa: ANN001, N802
        if event.type() == QEvent.MouseButtonRelease:
            idx = self.view().indexAt(event.position().toPoint())
            if idx.isValid():
                item = self._model.itemFromIndex(idx)
                item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked
                                   else Qt.Checked)
                return True
        return super().eventFilter(obj, event)

    def _on_item_changed(self, _item) -> None:  # noqa: ANN001
        self._refresh_text()
        self.refreshed.emit()
        self.changed.emit()

    def displayText(self) -> str:  # noqa: N802
        """收起态显示的文字（自绘用；也让测试能直接读到）。"""
        n = len(self.selectedModes())
        return t("thumbgrid.modes_none") if n == 0 \
            else t("thumbgrid.modes_count").format(n=n)

    def paintEvent(self, event):  # noqa: ANN001, N802
        """自绘收起态：把 currentText 换成"已选 N 种"，并画出下拉箭头。

        model 里的 item 是各个模式，非可编辑 combobox 默认会显示
        currentIndex 对应的模式名——那不是我们要的；而改用可编辑模式又
        会让下拉点不开（见类文档）。所以只替换绘制用的文本。
        箭头也要自己画：全局 QSS 把 ::drop-down 宽度设成 0（原意是让
        icons.ArrowComboBox 自绘），不画的话控件看着就不像可以点开的下拉。
        """
        painter = QStylePainter(self)
        painter.setPen(self.palette().color(QPalette.Text))
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        opt.currentText = self.displayText()
        opt.currentIcon = QIcon()
        opt.iconSize = QSize()
        painter.drawComplexControl(QStyle.CC_ComboBox, opt)
        painter.drawControl(QStyle.CE_ComboBoxLabel, opt)

        font = painter.font()
        font.setFamily(icons.FAMILY)
        font.setPixelSize(9)
        painter.setFont(font)
        painter.setPen(self.palette().text().color())
        painter.drawText(self.rect().adjusted(0, 0, -8, 0),
                         Qt.AlignRight | Qt.AlignVCenter, icons.CHEVRON_DOWN)

    def _refresh_text(self) -> None:
        self.update()          # 触发 paintEvent 重绘收起态文字
        names = [t(_MODE_LABEL[m]) for m in self.selectedModes()]
        self.setToolTip(("、".join(names) + "\n\n" if names else "")
                        + t("thumbgrid.modes_hint"))

    def selectedModes(self) -> list[str]:
        out = []
        for row in range(self._model.rowCount()):
            it = self._model.item(row)
            if it.checkState() == Qt.Checked:
                out.append(str(it.data(Qt.UserRole)))
        return out

    def setSelectedModes(self, modes) -> None:  # noqa: N802
        wanted = {str(m) for m in (modes or [])}
        blocked = self._model.blockSignals(True)
        for row in range(self._model.rowCount()):
            it = self._model.item(row)
            it.setCheckState(Qt.Checked if it.data(Qt.UserRole) in wanted
                             else Qt.Unchecked)
        self._model.blockSignals(blocked)
        self._refresh_text()
        self.refreshed.emit()


class ThumbGridOptionsWidget(QWidget):
    """缩略图网格的完整参数面板（可嵌入对话框或工具页）。"""

    def __init__(self, videos: list[Path] | None = None, parent=None,
                 show_save_dir: bool = True) -> None:
        super().__init__(parent)
        self._videos = list(videos or [])
        self.setStyleSheet(
            f"QLabel {{ color:{theme.TEXT}; }}"
            f"QLineEdit, QSpinBox, QComboBox {{ background:{theme.BG_RAISED};"
            f" color:{theme.TEXT}; border:1px solid {theme.BORDER};"
            f" border-radius:4px; padding:5px 8px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # ---- 保存位置
        self.ed_dir = QLineEdit()
        self._dir_touched = False
        self._auto_dir = self._default_dir()
        self.ed_dir.textEdited.connect(self._mark_dir_touched)
        if show_save_dir:
            row_dir = QHBoxLayout()
            row_dir.addWidget(QLabel(t("thumbgrid.save_dir")))
            self.ed_dir.setText(self._auto_dir)
            row_dir.addWidget(self.ed_dir, 1)
            btn_browse = QPushButton(t("thumbgrid.browse"))
            btn_browse.clicked.connect(self._browse)
            row_dir.addWidget(btn_browse)
            lay.addLayout(row_dir)
        else:
            self.ed_dir.setText(self._auto_dir)
            self.ed_dir.hide()

        # ---- 行列（1 起，1×1 = 单张截图）
        grid = QGridLayout()
        grid.setSpacing(6)
        self.sp_cols = QSpinBox()
        self.sp_cols.setRange(1, 10)
        self.sp_cols.setValue(_int(settings["thumbgrid_cols"], 5, 1, 10))
        grid.addWidget(QLabel(t("thumbgrid.columns")), 0, 0)
        grid.addWidget(self.sp_cols, 0, 1)
        self.sp_rows = QSpinBox()
        self.sp_rows.setRange(1, 10)
        self.sp_rows.setValue(_int(settings["thumbgrid_rows"], 5, 1, 10))
        grid.addWidget(QLabel(t("thumbgrid.rows")), 0, 2)
        grid.addWidget(self.sp_rows, 0, 3)
        hint = QLabel(t("thumbgrid.grid_hint"))
        hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        grid.addWidget(hint, 0, 4)
        grid.setColumnStretch(5, 1)
        lay.addLayout(grid)

        # ---- 抓帧方式（多选）
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        mode_row.addWidget(QLabel(t("thumbgrid.modes")))
        self.cb_modes = _ModeMultiCombo()
        self.cb_modes.setSelectedModes(_modes_from_settings())
        self.cb_modes.refreshed.connect(self._sync_mode_params)
        mode_row.addWidget(self.cb_modes, 1)
        mode_row.addWidget(_HelpDot(t("thumbgrid.modes_hint")))
        lay.addLayout(mode_row)

        # ---- 每种模式的参数（只在勾选后显示）
        self.param_panel = QWidget()
        pg = QGridLayout(self.param_panel)
        pg.setContentsMargins(0, 0, 0, 0)
        pg.setSpacing(6)

        self.sp_trim_head = _pct_spin(settings["thumbgrid_trim_head"], 5.0)
        self.sp_trim_tail = _pct_spin(settings["thumbgrid_trim_tail"], 5.0)
        self.row_trim = QWidget()
        trim_lay = QHBoxLayout(self.row_trim)
        trim_lay.setContentsMargins(0, 0, 0, 0)
        trim_lay.setSpacing(6)
        trim_lay.addWidget(labeled(t("thumbgrid.trim_head"), self.sp_trim_head,
                                   t("thumbgrid.mode_trim_tip")))
        trim_lay.addWidget(labeled(t("thumbgrid.trim_tail"), self.sp_trim_tail))
        trim_lay.addStretch(1)
        pg.addWidget(self.row_trim, 0, 0, 1, 4)

        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(1, 3600)
        self.sp_interval.setSuffix(" s")
        self.sp_interval.setValue(_int(settings["thumbgrid_interval"], 30, 1, 3600))
        self.row_interval = labeled(t("thumbgrid.interval_secs"), self.sp_interval,
                                    t("thumbgrid.mode_interval_tip"))
        pg.addWidget(self.row_interval, 1, 0, 1, 2)

        self.ed_range_start = QLineEdit(str(settings["thumbgrid_range_start"] or ""))
        self.ed_range_start.setPlaceholderText(t("thumbgrid.time_placeholder"))
        self.ed_range_end = QLineEdit(str(settings["thumbgrid_range_end"] or ""))
        self.ed_range_end.setPlaceholderText(t("thumbgrid.range_end_hint"))
        self.row_range = QWidget()
        range_lay = QHBoxLayout(self.row_range)
        range_lay.setContentsMargins(0, 0, 0, 0)
        range_lay.setSpacing(6)
        range_lay.addWidget(labeled(t("thumbgrid.range_start"), self.ed_range_start,
                                    t("thumbgrid.mode_range_tip")))
        range_lay.addWidget(labeled(t("thumbgrid.range_end"), self.ed_range_end))
        pg.addWidget(self.row_range, 2, 0, 1, 4)

        self.sp_random = QSpinBox()
        self.sp_random.setRange(1, 10)
        self.sp_random.setSuffix(t("thumbgrid.random_count_suffix"))
        self.sp_random.setValue(_int(settings["thumbgrid_random_count"], 1, 1, 10))
        self.row_random = labeled(t("thumbgrid.random_count"), self.sp_random,
                                  t("thumbgrid.mode_random_tip"))
        pg.addWidget(self.row_random, 3, 0, 1, 2)

        self.ed_exact = QLineEdit(str(settings["thumbgrid_exact_times"] or ""))
        self.ed_exact.setPlaceholderText(t("thumbgrid.exact_times_hint"))
        self.row_exact = labeled(t("thumbgrid.exact_times"), self.ed_exact,
                                 t("thumbgrid.mode_exact_tip"))
        pg.addWidget(self.row_exact, 4, 0, 1, 4)
        pg.setColumnStretch(3, 1)
        lay.addWidget(self.param_panel)

        # ---- 高级（折叠）
        self.btn_advanced = QPushButton(t("thumbgrid.advanced"))
        self.btn_advanced.setCheckable(True)
        self.btn_advanced.clicked.connect(self._toggle_advanced)
        lay.addWidget(self.btn_advanced)

        self.adv_panel = QWidget()
        adv = QGridLayout(self.adv_panel)
        adv.setContentsMargins(0, 4, 0, 0)
        adv.setSpacing(6)
        self.sp_width = QSpinBox()
        self.sp_width.setRange(80, 640)
        self.sp_width.setSingleStep(20)
        self.sp_width.setSuffix(" px")
        self.sp_width.setValue(_int(settings["thumbgrid_width"], 160, 80, 640))
        adv.addWidget(labeled(t("thumbgrid.cell_width"), self.sp_width), 0, 0)
        self.cb_fmt = QComboBox()
        self.cb_fmt.addItem("JPG", "jpg")
        self.cb_fmt.addItem("PNG", "png")
        idx = self.cb_fmt.findData(str(settings["thumbgrid_format"] or "jpg"))
        self.cb_fmt.setCurrentIndex(max(0, idx))
        self.cb_fmt.currentIndexChanged.connect(self._sync_quality_enabled)
        adv.addWidget(labeled(t("thumbgrid.format"), self.cb_fmt), 0, 1)
        self.sp_quality = QSpinBox()
        self.sp_quality.setRange(40, 100)
        self.sp_quality.setValue(_int(settings["thumbgrid_quality"], 88, 40, 100))
        self.sp_quality.setToolTip(t("thumbgrid.quality_tip"))
        adv.addWidget(labeled(t("thumbgrid.quality"), self.sp_quality,
                              t("thumbgrid.quality_tip")), 0, 2)
        self.cb_exists = QComboBox()
        self.cb_exists.addItem(t("thumbgrid.exists_rename"), "rename")
        self.cb_exists.addItem(t("thumbgrid.exists_skip"), "skip")
        self.cb_exists.addItem(t("thumbgrid.exists_overwrite"), "overwrite")
        adv.addWidget(labeled(t("thumbgrid.on_exists"), self.cb_exists), 1, 0, 1, 2)
        adv.setColumnStretch(3, 1)
        self.adv_panel.setVisible(False)
        lay.addWidget(self.adv_panel)

        self._sync_mode_params()
        self._sync_quality_enabled()

    # ---- 内部
    def _default_dir(self) -> str:
        configured = str(settings["thumbgrid_save_dir"] or "").strip()
        if configured:
            return configured
        if self._videos:
            return str(self._videos[0].parent / "Thumbnails")
        return ""

    def setVideos(self, videos: list[Path]) -> None:  # noqa: N802
        """工具页里列表会变：仅在输入框还是"自动填的默认值"时跟着更新。

        无条件覆盖是错的：这个方法在列表每次变化时都会被调到，会把用户
        刚填的目录冲掉；而且"用户故意清空"也必须保留——否则判空守卫会
        被自动回填悄悄绕过，几百张图写进进程的工作目录。
        判据只比对输入框现值与上一次自动填入的值（不依赖 textEdited：
        程序化 setText 不触发它）。
        """
        auto = self.ed_dir.text().strip() == self._auto_dir
        self._videos = list(videos or [])
        if not auto or self._dir_touched:
            return
        self._auto_dir = self._default_dir()
        self.ed_dir.setText(self._auto_dir)

    def _mark_dir_touched(self, _text: str = "") -> None:
        """用户亲手改过保存位置：此后不再被列表变化自动覆盖。"""
        self._dir_touched = True

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, t("thumbgrid.save_dir"),
                                             self.ed_dir.text())
        if d:
            self._dir_touched = True
            self.ed_dir.setText(d)

    def _toggle_advanced(self, checked: bool) -> None:
        self.adv_panel.setVisible(checked)
        win = self.window()
        if win is not None:
            win.adjustSize()

    def _sync_quality_enabled(self) -> None:
        """PNG 无损：质量无意义 → 置灰（而不是把值改成 0 写回设置）。"""
        is_jpg = self.cb_fmt.currentData() == "jpg"
        self.sp_quality.setEnabled(is_jpg)

    def _sync_mode_params(self) -> None:
        """只显示已勾选模式的参数行，面板不被从不使用的输入框塞满。"""
        modes = set(self.cb_modes.selectedModes())
        self.row_trim.setVisible(MODE_TRIM in modes)
        self.row_interval.setVisible(MODE_INTERVAL in modes)
        self.row_range.setVisible(MODE_RANGE in modes)
        self.row_random.setVisible(MODE_RANDOM in modes)
        self.row_exact.setVisible(MODE_EXACT in modes)
        self.param_panel.setVisible(bool(
            modes & {MODE_TRIM, MODE_INTERVAL, MODE_RANGE, MODE_RANDOM, MODE_EXACT}))

    # ---- 对外
    def outputDirText(self) -> str:  # noqa: N802
        """输入框里的原文（判空必须看它）。

        不能拿 Path 判空：Path("") 是 WindowsPath('.')，str() 得到 "."——
        非空且看起来合法，于是"没填保存位置"的守卫直接失效，几百张图会
        静默写进进程的工作目录。
        """
        return self.ed_dir.text().strip()

    def outputDir(self) -> Path:  # noqa: N802
        return Path(self.outputDirText())

    def onExists(self) -> str:  # noqa: N802
        return str(self.cb_exists.currentData() or "rename")

    def formatName(self) -> str:  # noqa: N802
        return str(self.cb_fmt.currentData() or "jpg")

    def cellWidth(self) -> int:  # noqa: N802
        return int(self.sp_width.value())

    def quality(self) -> int:  # noqa: N802
        return int(self.sp_quality.value())

    def gridOptions(self) -> GridOptions:  # noqa: N802
        modes = self.cb_modes.selectedModes() or [MODE_EVEN]
        start = parse_time(self.ed_range_start.text()) or 0.0
        end = parse_time(self.ed_range_end.text()) or 0.0
        return GridOptions(
            rows=int(self.sp_rows.value()),
            cols=int(self.sp_cols.value()),
            modes=tuple(modes),
            trim_head_pct=float(self.sp_trim_head.value()),
            trim_tail_pct=float(self.sp_trim_tail.value()),
            interval_secs=float(self.sp_interval.value()),
            range_start=start,
            range_end=end,
            random_count=int(self.sp_random.value()),
            exact_times=tuple(parse_time_list(self.ed_exact.text())),
        )

    def persist(self) -> None:
        """把本次选择写回设置作为下次默认（与旧行为一致）。

        注意 quality 无论格式都存用户选的值：旧实现选 PNG 时写 0，
        越出 spinbox 的 40-100 范围，靠 `or 88` 才侥幸复位——用户调过的
        JPG 质量就这么被静默清掉了。
        """
        settings["thumbgrid_cols"] = int(self.sp_cols.value())
        settings["thumbgrid_rows"] = int(self.sp_rows.value())
        settings["thumbgrid_width"] = int(self.sp_width.value())
        settings["thumbgrid_format"] = self.formatName()
        settings["thumbgrid_quality"] = int(self.sp_quality.value())
        settings["thumbgrid_modes"] = list(self.cb_modes.selectedModes())
        settings["thumbgrid_trim_head"] = float(self.sp_trim_head.value())
        settings["thumbgrid_trim_tail"] = float(self.sp_trim_tail.value())
        settings["thumbgrid_interval"] = float(self.sp_interval.value())
        settings["thumbgrid_range_start"] = self.ed_range_start.text().strip()
        settings["thumbgrid_range_end"] = self.ed_range_end.text().strip()
        settings["thumbgrid_random_count"] = int(self.sp_random.value())
        settings["thumbgrid_exact_times"] = self.ed_exact.text().strip()


def _int(value, default: int, lo: int, hi: int) -> int:
    """把配置里的任意值收成 [lo, hi] 内的整数；无法解析或越界时给 default。

    必须做范围归一而不是 `int(x or default)`：旧版本选 PNG 时把
    thumbgrid_quality 写成 0（越出 40-100），拿它直接 setValue 会被 Qt
    夹到 40——用户看到的是"我的质量怎么变成 40 了"。越界一律回默认值。
    """
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    if lo <= n <= hi:
        return n
    return default


def _pct_spin(value, default: float) -> QSpinBox:
    sp = QSpinBox()
    sp.setRange(0, 45)
    sp.setSuffix(" %")
    sp.setValue(_int(value, int(default), 0, 45))
    return sp


def _modes_from_settings() -> list[str]:
    raw = settings["thumbgrid_modes"]
    if isinstance(raw, str):
        raw = [raw]
    modes = [m for m in (raw or []) if m in ALL_MODES]
    return modes or [MODE_EVEN]
