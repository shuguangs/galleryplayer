"""外部打开路径的回归测试（不构造完整主窗口）。

背景 bug：播放器已在运行时（单实例模式）从资源管理器双击/“打开方式”打开
视频，第二个进程把路径转发给 MainWindow.handle_external_paths →
_on_external_resolved 只调用了 open_playlist([item]) + set_folder(quiet=True)，
却没设置 self._startup_file——文件夹扫描完成后 _handle_scan_batch 的
startup 分支不触发，右侧播放列表永远停留在单文件，同文件夹的视频不加载。

命令行首启路径（main.py 把 startup_file 传进构造函数）没这个问题；本测试
保证转发路径与它同构。

用替身对象 + 未绑定方法直调，避免为一条 slot 构造整个 QMainWindow
（toolbar/browser/viewer/thumbs 全家桶）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main_window import MainWindow  # noqa: E402


class _FakeViewer:
    def __init__(self):
        self.opened = []

    def open_playlist(self, items, index):
        self.opened.append(([i.path for i in items], index))

    def isVisible(self):
        return True


class _FakeWindow:
    """MainWindow 的最小替身：只提供 _on_external_resolved 触及的属性。"""

    def __init__(self):
        self._startup_file = None
        self.viewer = _FakeViewer()
        self.set_folder_calls = []

    def ensure_viewer(self):
        return self.viewer

    def set_folder(self, folder, force=False, quiet=False):
        self.set_folder_calls.append((folder, force, quiet))


class ExternalOpenTests(unittest.TestCase):
    def _call(self, decision):
        win = _FakeWindow()
        # 绑定真实方法到替身上：被测逻辑是 MainWindow 的，不是替身的
        fn = MainWindow._on_external_resolved.__get__(win)
        fn(decision)
        return win

    def test_media_sets_startup_file_for_scan_handoff(self):
        """转发路径必须设置 _startup_file，扫描 done 才能把完整列表交给 viewer。"""
        target = Path(r"X:\videos\a.mp4")
        item = mock.Mock()
        item.path = target
        with mock.patch("app.main_window.media.item_for_path",
                        return_value=item) as m:
            win = self._call(("media", target))
            m.assert_called_once_with(target)

        self.assertEqual(win._startup_file, target,
                         "转发路径未设置 _startup_file：扫描完成后播放列表"
                         "不会替换成同文件夹的完整列表")
        self.assertEqual(win.viewer.opened, [([target], 0)])
        self.assertEqual(win.set_folder_calls, [(target.parent, True, True)],
                         "应以 quiet+force 扫描打开所在文件夹：quiet=不切浏览器，"
                         "force=同文件夹时 set_folder 会短路返回，不强制重扫就"
                         "没有扫描 done，_startup_file 永远无人消费")

    def test_unsupported_media_ignored(self):
        """非媒体文件（item_for_path 返回 None）不应触发任何打开动作。"""
        with mock.patch("app.main_window.media.item_for_path",
                        return_value=None):
            win = self._call(("media", Path(r"X:\notes.txt")))
        self.assertIsNone(win._startup_file)
        self.assertEqual(win.viewer.opened, [])
        self.assertEqual(win.set_folder_calls, [])

    def test_folder_decision_keeps_existing_behaviour(self):
        folder = Path(r"X:\videos")
        win = self._call(("folder", folder))
        self.assertEqual(win.set_folder_calls, [(folder, False, False)])
        self.assertIsNone(win._startup_file)


if __name__ == "__main__":
    unittest.main()
