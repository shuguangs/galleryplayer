"""缩略图网格生成：参数配置弹窗（参数控件本体在 thumb_grid_options）。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from . import theme
from .i18n import t
from .thumb_grid import GridOptions
from .thumb_grid_options import ThumbGridOptionsWidget


class ThumbGridDialog(QDialog):
    """缩略图网格参数配置（模态）。

    保存位置（默认视频所在目录/Thumbnails）· 行列 1-10（1×1 = 单张截图）·
    抓帧方式多选 · 高级折叠（缩略图宽度/格式/质量/冲突策略）。
    设置页的全局默认值在此预填，本次选择又写回成新的默认。
    """
    # 用户点击立即生成后发射：([视频路径], 输出目录, 单格宽, 格式, 质量, 抓帧参数)
    startRequested = Signal(list, Path, int, str, int, object)

    def __init__(self, videos: list[Path], parent=None) -> None:
        super().__init__(parent)
        self._videos = list(videos)
        self.setWindowTitle(t("thumbgrid.title"))
        self.setMinimumWidth(560)
        self.setStyleSheet(
            f"QDialog {{ background:{theme.BG_BASE}; }}"
            f"QLabel {{ color:{theme.TEXT}; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(9)

        summary = QLabel(t("thumbgrid.summary").format(n=len(self._videos)))
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color:{theme.TEXT_DIM};")
        lay.addWidget(summary)

        self.options = ThumbGridOptionsWidget(self._videos, self)
        lay.addWidget(self.options)
        lay.addStretch(1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton(t("thumbgrid.cancel"))
        self.btn_cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_cancel)
        self.btn_ok = QPushButton(t("thumbgrid.generate"))
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self._start)
        self.btn_ok.setStyleSheet(
            f"QPushButton {{ background:{theme.ACCENT}; color:#fff;"
            f" border:none; border-radius:4px; padding:6px 18px; }}"
        )
        btns.addWidget(self.btn_ok)
        lay.addLayout(btns)

    # 兼容旧调用方（main_window / tools_dialog 读它取冲突策略）
    @property
    def cb_exists(self):
        return self.options.cb_exists

    def gridOptions(self) -> GridOptions:  # noqa: N802
        return self.options.gridOptions()

    def _start(self) -> None:
        if not self.options.outputDirText():
            # 判空看输入框原文：Path("") 会变成 '.'（看着合法），守卫会失效
            QMessageBox.warning(self, t("thumbgrid.title"), t("thumbgrid.no_dir"))
            return
        out_dir = self.options.outputDir()
        self.options.persist()
        self.startRequested.emit(
            self._videos, out_dir, self.options.cellWidth(),
            self.options.formatName(), self.options.quality(),
            self.options.gridOptions(),
        )
        self.accept()
