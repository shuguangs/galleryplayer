"""缩略图网格生成进度对话框：后台线程顺序处理，UI 永不卡死。"""
from __future__ import annotations

import shutil
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from . import theme
from .i18n import t
from .thumb_grid import (
    MODE_EVEN,
    DecodeFailed,
    GridOptions,
    NoPlans,
    generate_sheets,
)
from .thumbs import MpvGrabber


class _GenSignals(QObject):
    progress = Signal(int, int, str)     # done, total, 当前文件名
    log = Signal(str, bool)              # 行文本, 是否错误
    finished_all = Signal(int, int)      # 成功数, 失败数


class ThumbGridProgressDialog(QDialog):
    """批量生成进度：单线程队列（防 GPU 过载），UI 异步。

    一个视频可能产出多张图（勾了多种抓帧方式 / 随机要了多份）：进度按
    「视频」计，每张图各写一行日志。冲突策略由参数面板选定（跳过/覆盖/
    自动重命名）；生成前做磁盘空间预检（<100MB 警告仍可继续）；取消时
    优雅终止当前 grabber 并停止队列。
    """

    def __init__(self, videos: list[Path], out_dir: Path, width: int, fmt: str,
                 quality: int, options: GridOptions | None = None,
                 on_exists: str = "rename", parent=None) -> None:
        super().__init__(parent)
        self._videos = list(videos)
        self._out_dir = out_dir
        self._width, self._fmt, self._quality = width, fmt, quality
        self._opts = options or GridOptions(modes=(MODE_EVEN,))
        self._on_exists = on_exists
        self._cancel = threading.Event()

        self.setWindowTitle(t("thumbgrid.progress_title"))
        self.setMinimumSize(560, 360)
        self.setStyleSheet(
            f"QDialog {{ background:{theme.BG_BASE}; }}"
            f"QLabel {{ color:{theme.TEXT}; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 10)
        self.status = QLabel(t("thumbgrid.preparing"))
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, max(1, len(self._videos)))
        self.bar.setValue(0)
        lay.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet(
            f"QPlainTextEdit {{ background:{theme.BG_RAISED};"
            f" color:{theme.TEXT}; border:1px solid {theme.BORDER};"
            f" border-radius:4px; font-family:Consolas; font-size:11px; }}"
        )
        lay.addWidget(self.log, 1)
        btn_row = QHBoxLayout()
        self.btn_open_dir = QPushButton(t("thumbgrid.open_dir"))
        # 一开始就可见：大批量时用户可能生成第一张就想看结果
        self.btn_open_dir.clicked.connect(self._open_out_dir)
        btn_row.addWidget(self.btn_open_dir)
        btn_row.addStretch(1)
        self.btn_cancel = QPushButton(t("thumbgrid.cancel"))
        self.btn_cancel.clicked.connect(self._cancel_all)
        btn_row.addWidget(self.btn_cancel)
        lay.addLayout(btn_row)

        self._sig = _GenSignals()
        self._sig.progress.connect(self._on_progress)
        self._sig.log.connect(self._on_log)
        self._sig.finished_all.connect(self._on_finished)

        threading.Thread(target=self._worker, daemon=True,
                         name="thumbgrid-gen").start()

    # ---- UI 回调（GUI 线程）----
    def _on_progress(self, done: int, total: int, current: str) -> None:
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        self.status.setText(t("thumbgrid.progress_status").format(
            done=done, total=total, name=current))

    def _on_log(self, line: str, is_error: bool) -> None:
        if is_error:
            self.log.appendHtml(
                f'<span style="color:#e0653f">{line}</span>')
        else:
            self.log.appendPlainText(line)

    def _on_finished(self, ok: int, fail: int) -> None:
        self.status.setText(t("thumbgrid.done_status").format(ok=ok, fail=fail))
        self.btn_cancel.setText(t("thumbgrid.close"))
        self.btn_cancel.clicked.disconnect(self._cancel_all)
        self.btn_cancel.clicked.connect(self.accept)

    def _open_out_dir(self) -> None:
        """在资源管理器中打开输出目录（不存在则先创建）。"""
        try:
            self._out_dir.mkdir(parents=True, exist_ok=True)
            from . import fileops

            fileops.open_folder(self._out_dir)
        except Exception:
            pass

    def _cancel_all(self) -> None:
        self._cancel.set()
        self.status.setText(t("thumbgrid.cancelling"))
        self.btn_cancel.setEnabled(False)

    # ---- 工作线程 ----
    def _worker(self) -> None:
        grabber = MpvGrabber(tag="thumbgrid")
        ok = fail = 0
        total = len(self._videos)
        try:
            # 磁盘空间预检（<100MB 警告继续——PIL 每张约几 MB）
            try:
                usage = shutil.disk_usage(str(self._out_dir))
                if usage.free < 100 * 1024 * 1024:
                    self._sig.log.emit(
                        t("thumbgrid.low_disk").format(
                            mb=usage.free // 1024 // 1024), True)
            except Exception:
                pass
            for i, video in enumerate(self._videos):
                if self._cancel.is_set():
                    break
                self._sig.progress.emit(i, total, video.name)
                try:
                    if not video.is_file():
                        self._sig.log.emit(
                            t("thumbgrid.err_missing").format(name=video.name), True)
                        fail += 1
                        continue
                    made = self._process_one(grabber, video)
                    if made <= 0:
                        fail += 1
                    else:
                        ok += 1
                except Exception as exc:  # noqa: BLE001
                    fail += 1
                    self._sig.log.emit(
                        t("thumbgrid.err_line").format(name=video.name,
                                                       err=str(exc)[:100]), True)
                    grabber._reset()  # 损坏文件：重置实例再战下一个
                    traceback.print_exc()
            self._sig.progress.emit(ok + fail, total, "")
        finally:
            grabber.close()
            self._sig.finished_all.emit(ok, fail)

    def _process_one(self, grabber: MpvGrabber, video: Path) -> int:
        """一个视频：按计划产出 1..N 张图，返回成功保存的张数。

        抓帧与拼接的实际工作在 thumb_grid.generate_sheets（与 UI 无关，
        可单测）；这里只把结果翻译成进度与日志。
        """
        try:
            made = generate_sheets(
                grabber, video, self._out_dir, self._opts,
                cell_width=self._width, fmt=self._fmt, quality=self._quality,
                on_exists=self._on_exists,
                should_cancel=self._cancel.is_set)
        except DecodeFailed:
            self._sig.log.emit(
                t("thumbgrid.err_decode").format(name=video.name), True)
            return 0
        except NoPlans:
            # 参数不成立（时间段填反了、精确点一个都没填…）：明说，
            # 不要让用户以为是解码失败
            self._sig.log.emit(
                t("thumbgrid.plan_none").format(name=video.name), True)
            return 0
        for plan, out in made:
            self._sig.log.emit(
                t("thumbgrid.ok_line_mode").format(
                    name=video.name, mode=plan.label, out=out.name), False)
        return len(made)
