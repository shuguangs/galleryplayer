"""启动日志系统 + 导出的回归测试。"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class StartupLogTests(unittest.TestCase):
    def setUp(self):
        import importlib
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="startup-log-"))
        from app import runtime, startup_log

        importlib.reload(runtime)  # 换 USERDATA_DIR 需重载（模块级常量）
        runtime.USERDATA_DIR = self.tmp
        startup_log._PATH = None
        self.runtime = runtime
        self.slog = startup_log

    def tearDown(self):
        import importlib

        self.slog._PATH = None
        importlib.reload(self.runtime)  # 还原真实路径

    def test_begin_creates_dated_log_in_logs_dir(self):
        self.slog.begin()
        logs = list((self.tmp / "logs").glob("startup_*.log"))
        self.assertEqual(1, len(logs), "begin() 必须在 logs/ 下建独立日志文件")
        content = logs[0].read_text(encoding="utf-8")
        self.assertIn("process", content)
        self.assertIn("frozen=", content)

    def test_stage_appends_immediately(self):
        self.slog.begin()
        self.slog.stage("my-stage", "打点内容")
        logs = list((self.tmp / "logs").glob("startup_*.log"))
        content = logs[0].read_text(encoding="utf-8")
        self.assertIn("my-stage", content)
        self.assertIn("打点内容", content)

    def test_stall_detector_records_blocking(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        self.slog.begin()
        self.slog.attach(app)

        def block_then_quit():
            import time

            time.sleep(1.4)               # GUI 线程阻塞（QTimer 冻结）
            QTimer.singleShot(0, app.quit)  # 回事件循环让心跳 tick 记录 gap

        QTimer.singleShot(50, block_then_quit)
        app.exec()
        logs = list((self.tmp / "logs").glob("startup_*.log"))
        content = logs[0].read_text(encoding="utf-8")
        self.assertIn("gui-stall", content, "卡顿检测必须记录 GUI 线程阻塞")

    def test_prune_keeps_only_recent(self):
        self.slog.begin()
        logs_dir = self.tmp / "logs"
        for i in range(25):
            (logs_dir / f"startup_20260101_0000{i:02d}.log").write_text("x")
        self.slog._prune_old_logs()
        remaining = list(logs_dir.glob("startup_*.log"))
        self.assertLessEqual(len(remaining), self.slog._KEEP,
                             "旧日志必须被清理，否则无限累积")

    def test_recent_logs_newest_first(self):
        self.slog.begin()
        logs_dir = self.tmp / "logs"
        (logs_dir / "startup_20260101_000001.log").write_text("old")
        (logs_dir / "startup_20260101_000002.log").write_text("new")
        recent = self.slog.recent_logs(5)
        self.assertEqual("startup_20260101_000002.log", recent[0].name)

    def test_write_failure_never_raises(self):
        self.slog.begin()
        with mock.patch.object(Path, "write_text", side_effect=OSError):
            self.slog.stage("boom", "写不进去也不许抛")  # 不抛即通过

    def test_main_py_wires_begin_and_attach(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("startup_log.begin()", src)
        self.assertIn("startup_log.attach(app)", src)
        # 关键阶段必须都有打点（少了哪个，那个阶段的卡顿就成了盲区）
        for needle in ("libmpv", "stylesheet", "mainwindow", "shown"):
            self.assertIn(f'startup_log.stage("{needle}"', src)


class ExportLogTests(unittest.TestCase):
    def test_export_writes_zip_to_app_root(self):
        """导出：不打文件对话框，zip 落在播放器根目录，含最近日志。"""
        import tempfile
        import zipfile

        from app import settings_dialog

        tmp = Path(tempfile.mkdtemp(prefix="export-log-"))
        logs = tmp / "logs"
        logs.mkdir()
        (logs / "startup_20260101_000001.log").write_text("a", encoding="utf-8")
        (logs / "startup_20260101_000002.log").write_text("b", encoding="utf-8")

        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])

        with mock.patch.object(settings_dialog, "APP_DIR", tmp), \
                mock.patch("app.startup_log.recent_logs",
                           return_value=sorted(logs.glob("*.log"),
                                               reverse=True)), \
                mock.patch.object(QMessageBox, "information") as info:
            dlg = settings_dialog.SettingsDialog.__new__(
                settings_dialog.SettingsDialog)
            dlg._export_diagnostic_log()

        zips = list(tmp.glob("播放器诊断日志_*.zip"))
        self.assertEqual(1, len(zips), "zip 必须落在播放器根目录")
        with zipfile.ZipFile(zips[0]) as zf:
            names = zf.namelist()
            self.assertIn("startup_20260101_000001.log", names)
            self.assertIn("startup_20260101_000002.log", names)
            self.assertIn("环境信息.txt", names)
        info.assert_called_once()

    def test_settings_has_export_button(self):
        src = (ROOT / "app" / "settings_dialog.py").read_text(encoding="utf-8")
        self.assertIn("_export_diagnostic_log", src)
        self.assertIn("recent_logs", src)


if __name__ == "__main__":
    unittest.main()
