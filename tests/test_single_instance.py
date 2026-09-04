"""单实例机制的回归测试：裸双击也必须转交/唤醒，互斥体排除第二实例。"""
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402

import app.single_instance as si  # noqa: E402

_TEST_PIPE = "LocalMediaPlayer.TestSingleInstance"


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.app = QCoreApplication.instance() or QCoreApplication([])
        # 换用测试专属名字：与本机可能正在运行的真实播放器完全隔离
        self._orig_pipe = si.SERVER_NAME
        self._orig_mutex = si.MUTEX_NAME
        si.SERVER_NAME = _TEST_PIPE
        si.MUTEX_NAME = (f"Local\\{_TEST_PIPE}.{id(self):x}"
                         f".{int(time.monotonic() * 1e9):x}")

    def tearDown(self):
        si.SERVER_NAME = self._orig_pipe
        si.MUTEX_NAME = self._orig_mutex

    def _pump(self, seconds: float = 1.5) -> None:
        """泵事件循环：连接建立/读缓冲/断开都是信号驱动的。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def test_mutex_excludes_second_instance(self):
        """互斥体：第一个拿到，同名的第二个拿不到（双开的大门）。"""
        self.assertTrue(si.acquire_single_instance())
        self.assertFalse(si.acquire_single_instance())

    def test_bare_launch_wakes_running_instance(self):
        """裸双击（无参数）也必须唤醒已有实例，而不是开第二个窗口。

        客户端在服务器泵事件之前就走完了全程（正是唤醒一个还在启动中
        的实例的时序）——接受时已非 Connected 的连接必须当场收尾。
        """
        server = si.InstanceServer()
        self.assertTrue(server.listen())
        raised = []
        server.raise_requested.connect(lambda: raised.append(1))
        try:
            self.assertTrue(si.forward_to_running([]))
            self._pump()
            self.assertEqual(raised, [1])
            self.assertTrue(server.take_early()[1])  # 早期缓冲同步记录
        finally:
            server._server.close()

    def test_forward_delivers_paths(self):
        """带文件的启动：路径完整送达，早期缓冲可补投。"""
        server = si.InstanceServer()
        self.assertTrue(server.listen())
        got = []
        server.paths_received.connect(got.append)
        try:
            self.assertTrue(si.forward_to_running(["a.mp4", "b.mp4"]))
            self._pump()
            self.assertEqual(got, [["a.mp4", "b.mp4"]])
            early_paths, _ = server.take_early()
            self.assertEqual(early_paths, ["a.mp4", "b.mp4"])
        finally:
            server._server.close()

    def test_no_server_means_first_launch(self):
        """没有活实例：forward 返回 False（正常首启，继续走启动流程）。"""
        self.assertFalse(si.forward_to_running(["x.mp4"]))
        self.assertFalse(si.forward_to_running([]))


if __name__ == "__main__":
    unittest.main()
