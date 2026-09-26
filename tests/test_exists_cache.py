"""播放列表绘制路径不得 stat（网络盘面板重绘卡顿回归）。

背景 bug（用户实测）：面板行委托每画一行调一次 MediaItem.exists →
Path.stat；网络盘上每次一次网络往返，重绘几十行 GUI 线程卡数秒。
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from app.exists_cache import ExistsCache

_app = QApplication.instance() or QApplication([])


def _pump_until(pred, ms=3000):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


class ExistsCacheTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.present = self.root / "a.mp4"
        self.present.write_bytes(b"x")
        self.gone = self.root / "gone.mp4"
        self.cache = ExistsCache()

    def tearDown(self):
        self.cache.shutdown()
        self._td.cleanup()

    def test_query_never_stats_on_caller_thread(self):
        """模拟慢网盘：stat 要 2 秒。调用方（GUI 线程）必须立即返回。"""
        caller = threading.get_ident()
        stat_threads = []
        real = os.path.exists

        def slow(p):
            stat_threads.append(threading.get_ident())
            time.sleep(2.0)
            return real(p)

        with mock.patch("app.exists_cache.os.path.exists", side_effect=slow):
            t0 = time.perf_counter()
            for _ in range(50):          # 一次重绘 50 行
                self.cache.known_missing(self.gone)
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 0.05, f"查询阻塞了 {elapsed:.2f}s")
            _pump_until(lambda: stat_threads, 1000)
        self.assertNotIn(caller, stat_threads, "stat 跑在了调用方线程上")
        self.assertEqual(len(stat_threads), 1, "同一路径重复排队（未去重）")

    def test_unknown_is_treated_as_present(self):
        """未确认前不标灰（宁可晚一拍标灰，不误标存在的文件）。"""
        self.assertFalse(self.cache.known_missing(self.gone))

    def test_missing_reported_after_background_check(self):
        fired = []
        self.cache.changed.connect(lambda: fired.append(1))
        self.cache.known_missing(self.gone)
        self.assertTrue(_pump_until(lambda: fired), "未发 changed 通知重画")
        self.assertTrue(self.cache.known_missing(self.gone))
        self.assertFalse(self.cache.known_missing(self.present))


class DelegateNoStatTests(unittest.TestCase):
    def test_paint_path_does_not_call_item_exists(self):
        """行委托源码里不得再读 item.exists（它会同步 stat）。"""
        src = (Path(__file__).resolve().parent.parent / "app" /
               "playlist_panel.py").read_text(encoding="utf-8")
        start = src.index("class MediaRowDelegate")
        end = src.index("\nclass ", start + 10)
        body = src[start:end]
        self.assertFalse("item.exists" in body,
                         "MediaRowDelegate 仍在绘制时调用 item.exists")


if __name__ == "__main__":
    unittest.main()
