"""批量工具面板：入口选择器 → 二级功能页（两个功能共用一份视频列表）。

结构（用户要求把两个功能拆开，而不是挤在同一页）：
    页 0  功能选择器：两张卡片（批量 SRT / 缩略图网格）
    页 1  功能页：该功能的说明 + 专属参数（网格页内嵌 ThumbGridOptionsWidget）
    视频列表放在 stack 下方、由两个功能共享——返回选择器再进另一个功能时
    列表不丢（同一批视频常常既要字幕又要预览图）。

复用既有链路：SRT 走 main_window._batch_generate_srt（引擎批量队列），
缩略图网格直接走 ThumbGridProgressDialog（参数已在页内，不再弹第三层窗口）。
原右键入口不受影响——本面板只是把两件事从"当前文件夹"解放出来。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import media, theme
from .i18n import t

PAGE_PICK = 0
PAGE_TOOL = 1

TOOL_SRT = "srt"
TOOL_GRID = "grid"


class _ToolCard(QPushButton):
    """选择器里的功能卡片：标题 + 一句说明，整块可点。"""

    def __init__(self, title: str, desc: str, parent=None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(74)
        self.setStyleSheet(
            f"QPushButton {{ background:{theme.BG_RAISED}; color:{theme.TEXT};"
            f" border:1px solid {theme.BORDER}; border-radius:8px;"
            f" text-align:left; padding:10px 14px; }}"
            f"QPushButton:hover {{ border-color:{theme.ACCENT}; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        lb_title = QLabel(title)
        lb_title.setStyleSheet(f"color:{theme.TEXT}; font-size:13px;")
        lay.addWidget(lb_title)
        lb_desc = QLabel(desc)
        lb_desc.setWordWrap(True)
        lb_desc.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:11px;")
        lay.addWidget(lb_desc)
        # 子标签不该吃掉父按钮的点击
        for w in (lb_title, lb_desc):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)


class ToolsDialog(QDialog):
    """批量工具：选功能 → 添加任意视频/文件夹 → 生成。"""

    _instance: "ToolsDialog | None" = None

    def __init__(self, main_window, parent=None) -> None:
        super().__init__(parent)
        self._main = main_window
        self._tool: str | None = None
        self.setWindowTitle(t("tools.title"))
        self.setMinimumSize(600, 480)
        self.setStyleSheet(
            f"QDialog {{ background:{theme.BG_BASE}; }}"
            f"QLabel {{ color:{theme.TEXT}; }}"
            f"QListWidget {{ background:{theme.BG_RAISED};"
            f" color:{theme.TEXT}; border:1px solid {theme.BORDER};"
            f" border-radius:6px; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(8)

        self.head = QLabel(t("tools.pick_hint"))
        self.head.setWordWrap(True)
        self.head.setStyleSheet(f"color:{theme.TEXT_DIM};")
        lay.addWidget(self.head)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_picker())     # PAGE_PICK
        self.stack.addWidget(self._build_tool_page())  # PAGE_TOOL
        lay.addWidget(self.stack)

        # ---- 共享的视频列表（两个功能共用）
        self.list_box = QWidget()
        lb = QVBoxLayout(self.list_box)
        lb.setContentsMargins(0, 0, 0, 0)
        lb.setSpacing(6)

        add_row = QHBoxLayout()
        btn_files = QPushButton(t("tools.add_videos"))
        btn_files.clicked.connect(self._add_videos)
        add_row.addWidget(btn_files)
        btn_folder = QPushButton(t("tools.add_folder"))
        btn_folder.clicked.connect(self._add_folder)
        add_row.addWidget(btn_folder)
        add_row.addStretch(1)
        self.count_label = QLabel(t("tools.count").format(n=0))
        add_row.addWidget(self.count_label)
        lb.addLayout(add_row)

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        self.list.setAlternatingRowColors(True)
        lb.addWidget(self.list, 1)

        self.empty_hint = QLabel(t("tools.empty_hint"))
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setStyleSheet(f"color:{theme.TEXT_FAINT};")
        lb.addWidget(self.empty_hint)

        rm_row = QHBoxLayout()
        btn_rm = QPushButton(t("tools.remove_selected"))
        btn_rm.clicked.connect(self._remove_selected)
        rm_row.addWidget(btn_rm)
        btn_clear = QPushButton(t("tools.clear"))
        btn_clear.clicked.connect(self._clear)
        rm_row.addWidget(btn_clear)
        rm_row.addStretch(1)
        lb.addLayout(rm_row)
        lay.addWidget(self.list_box, 1)

        # ---- 底栏：返回 + 主操作
        foot = QHBoxLayout()
        self.btn_back = QPushButton(t("tools.back"))
        self.btn_back.clicked.connect(self.show_picker)
        foot.addWidget(self.btn_back)
        foot.addStretch(1)
        self.btn_run = QPushButton(t("tools.gen_srt"))
        self.btn_run.clicked.connect(self._run)
        self.btn_run.setStyleSheet(
            f"QPushButton {{ background:{theme.ACCENT}; color:#fff;"
            f" border:none; border-radius:4px; padding:6px 18px; }}"
            f"QPushButton:disabled {{ background:{theme.BG_RAISED};"
            f" color:{theme.TEXT_FAINT}; }}"
        )
        foot.addWidget(self.btn_run)
        lay.addLayout(foot)

        self.show_picker()

    # ------------------------------------------------------------ 页面构建
    def _build_picker(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(8)
        title = QLabel(t("tools.pick_title"))
        title.setStyleSheet(f"color:{theme.TEXT}; font-size:14px;")
        lay.addWidget(title)
        self.card_srt = _ToolCard(t("tools.card_srt_title"),
                                  t("tools.card_srt_desc"))
        self.card_srt.clicked.connect(lambda: self.open_tool(TOOL_SRT))
        lay.addWidget(self.card_srt)
        self.card_grid = _ToolCard(t("tools.card_grid_title"),
                                   t("tools.card_grid_desc"))
        self.card_grid.clicked.connect(lambda: self.open_tool(TOOL_GRID))
        lay.addWidget(self.card_grid)
        lay.addStretch(1)
        return page

    def _build_tool_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(6)
        self.tool_hint = QLabel("")
        self.tool_hint.setWordWrap(True)
        self.tool_hint.setStyleSheet(f"color:{theme.TEXT_DIM};")
        lay.addWidget(self.tool_hint)

        # 缩略图网格的参数面板（延迟到首次进入该功能才建，省启动开销）
        self.grid_options = None
        self._grid_slot = QWidget()
        slot_lay = QVBoxLayout(self._grid_slot)
        slot_lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._grid_slot)
        return page

    def _ensure_grid_options(self):
        from .thumb_grid_options import ThumbGridOptionsWidget

        if self.grid_options is None:
            self.grid_options = ThumbGridOptionsWidget(self._videos(), self)
            self._grid_slot.layout().addWidget(self.grid_options)
        else:
            self.grid_options.setVideos(self._videos())
        return self.grid_options

    # ------------------------------------------------------------ 导航
    def show_picker(self) -> None:
        self._tool = None
        self.stack.setCurrentIndex(PAGE_PICK)
        self.head.setText(t("tools.pick_hint"))
        self.setWindowTitle(t("tools.title"))
        self.btn_back.setVisible(False)
        self.btn_run.setVisible(False)
        self.list_box.setVisible(False)
        self._grid_slot.setVisible(False)

    def open_tool(self, tool: str) -> None:
        self._tool = tool
        self.stack.setCurrentIndex(PAGE_TOOL)
        self.btn_back.setVisible(True)
        self.btn_run.setVisible(True)
        self.list_box.setVisible(True)
        if tool == TOOL_GRID:
            self.setWindowTitle(t("tools.card_grid_title"))
            self.head.setText(t("tools.card_grid_desc"))
            self.tool_hint.setText(t("tools.page_grid_hint"))
            self._ensure_grid_options()
            self._grid_slot.setVisible(True)
            self.btn_run.setText(t("tools.gen_grid"))
        else:
            self.setWindowTitle(t("tools.card_srt_title"))
            self.head.setText(t("tools.card_srt_desc"))
            self.tool_hint.setText(t("tools.page_srt_hint"))
            self._grid_slot.setVisible(False)
            self.btn_run.setText(t("tools.gen_srt"))
        self._refresh()

    # ------------------------------------------------------------ 列表管理
    def _videos(self) -> list[Path]:
        return [Path(self.list.item(i).data(Qt.UserRole))
                for i in range(self.list.count())]

    def _add_paths(self, paths: list) -> None:
        existing = {str(v) for v in self._videos()}
        added = 0
        for raw in paths:
            p = Path(raw)
            try:
                if p.is_dir():
                    # 文件夹：递归找视频（上限防呆：单文件夹最多 2000）
                    for f in sorted(p.rglob("*")):
                        if added >= 2000:
                            break
                        if f.is_file() and media.classify(f) is True \
                                and str(f) not in existing:
                            self.list.addItem(self._make_item(f))
                            existing.add(str(f))
                            added += 1
                elif p.is_file() and media.classify(p) is True \
                        and str(p) not in existing:
                    self.list.addItem(self._make_item(p))
                    existing.add(str(p))
                    added += 1
            except OSError:
                continue
        self._refresh()

    def _make_item(self, p: Path) -> QListWidgetItem:
        it = QListWidgetItem(p.name)
        it.setData(Qt.UserRole, str(p))
        it.setToolTip(str(p))
        return it

    def _add_videos(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, t("tools.add_videos"), "",
            "Videos (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.ts *.m2ts "
            "*.webm *.mpg *.mpeg);;All files (*.*)")
        if files:
            self._add_paths(files)

    def _add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, t("tools.add_folder"))
        if d:
            self._add_paths([d])

    def _remove_selected(self) -> None:
        for it in sorted(self.list.selectedItems(), key=lambda x: self.list.row(x)):
            self.list.takeItem(self.list.row(it))
        self._refresh()

    def _clear(self) -> None:
        self.list.clear()
        self._refresh()

    def _refresh(self) -> None:
        n = self.list.count()
        self.count_label.setText(t("tools.count").format(n=n))
        has = n > 0
        self.btn_run.setEnabled(has)
        self.empty_hint.setVisible(not has)
        if self.grid_options is not None and self._tool == TOOL_GRID:
            self.grid_options.setVideos(self._videos())

    # ------------------------------------------------------------ 执行
    def _run(self) -> None:
        if self._tool == TOOL_GRID:
            self._gen_grids()
        elif self._tool == TOOL_SRT:
            self._gen_srt()

    def _gen_grids(self) -> None:
        from .thumb_grid_progress import ThumbGridProgressDialog

        videos = self._videos()
        if not videos:
            return
        opts_widget = self._ensure_grid_options()
        if not opts_widget.outputDirText():
            # 判空看输入框原文：Path("") 会变成 '.'（看着合法），守卫会失效
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(self, t("thumbgrid.title"), t("thumbgrid.no_dir"))
            return
        out_dir = opts_widget.outputDir()
        opts_widget.persist()
        ThumbGridProgressDialog(
            videos, out_dir, opts_widget.cellWidth(), opts_widget.formatName(),
            opts_widget.quality(), options=opts_widget.gridOptions(),
            on_exists=opts_widget.onExists(), parent=self).show()

    def _gen_srt(self) -> None:
        """把列表丢进主窗口的 SRT 批量队列（复用引擎调度与进度窗）。"""
        videos = self._videos()
        if not videos:
            return
        items = [media.item_for_path(v) for v in videos]
        items = [i for i in items if i is not None and i.is_video]
        if not items:
            return
        # 批量互斥守卫在 _batch_generate_srt 内部（busy 时弹提示），
        # 这里直接借用同一入口
        self._main._batch_generate_srt(items)

    # ---- 单例复用 ----
    @classmethod
    def show_for(cls, main_window) -> "ToolsDialog":
        dlg = cls._instance
        if dlg is None:
            dlg = cls(main_window, main_window)
            cls._instance = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg
