"""起播即刻让路的回归测试（播放期间掉帧的最后一环）。

背景 bug（用户实测 + 日志实锤，309k 项文件夹）：让路开关由 500ms 轮询驱动，
判据是 `video_view.duration > 0 and not paused`——而 duration 要等 mpv 解复用
完成才回报。日志里 `viewer-play show_index 全部完成` 到
`thumb-gate 播放开始：视频抓帧整体让路` 相差 0.8-6.5s，而点开视频那一瞬间
正是面板填充 + 缩略图预热 + 定时归位一起抢 GUI 线程的时刻（mpv 画面同在
GUI 线程渲染，见 mpv_widget.MpvWidget.paintGL）——最该让路的窗口反而没人管。

修法：
- MpvWidget.media_loaded：loadfile 起播即真、stop() 归假（不等 mpv 回报）；
  判据换成 media_loaded and not paused。
- MpvWidget.playback_starting：在 loadfile *之前* 同步发出；MainWindow 收到
  就立刻走一遍让路开关，第一帧之前生效，不等下一次轮询。
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PLAYER_AUTOMATION", "1")   # 恢复播放列表的模态框会卡死

from app.runtime import init_libmpv

try:
    init_libmpv()
except RuntimeError as exc:                        # 未装 libmpv 的环境
    raise unittest.SkipTest(f"libmpv unavailable: {exc}")

from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow
from app.media import MediaItem


def _items(n: int) -> list[MediaItem]:
    return [MediaItem(path=Path(f"I:/tgdl/x_{i:06d}.mp4"), is_video=True,
                      size=1, mtime=0.0, is_archive=False)
            for i in range(n)]


class PlaybackGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.win = MainWindow()
        cls.viewer = cls.win.ensure_viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()
        cls.win.close()

    def setUp(self):
        self.mpw = self.viewer.video_view
        self.lst = self.viewer.panel.list
        self.mpw.media_loaded = False
        self.win._update_thumb_gates()             # 回到空闲基线

    def test_media_loaded_starts_false(self):
        self.assertFalse(self.mpw.media_loaded)
        self.mpw.stop()
        self.assertFalse(self.mpw.media_loaded)

    def test_playback_starting_freezes_background_synchronously(self):
        self.win._warmup_timer.start()
        self.win._resort_interval_timer.start()
        self.mpw.media_loaded = True
        self.mpw.playback_starting.emit()          # load() 会在 loadfile 前发它
        self.assertTrue(self.win._playback_active)
        self.assertFalse(self.win._warmup_timer.isActive())
        self.assertFalse(self.win._resort_interval_timer.isActive())
        self.assertTrue(self.lst._slow_pacing)

    def test_poll_agrees_and_does_not_undo_the_gate(self):
        """轮询判据必须认同 media_loaded（否则起播的让路会被立刻撤销）。"""
        self.mpw.media_loaded = True
        self.mpw.playback_starting.emit()
        self.win._update_thumb_gates()
        self.assertTrue(self.win._playback_active)

    def test_stop_resumes_background_work(self):
        self.win._warmup_timer.start()
        self.win._resort_interval_timer.start()
        self.mpw.media_loaded = True
        self.mpw.playback_starting.emit()
        self.mpw.media_loaded = False              # stop() 的效果
        self.win._update_thumb_gates()
        self.assertFalse(self.win._playback_active)
        self.assertTrue(self.win._warmup_timer.isActive())
        self.assertTrue(self.win._resort_interval_timer.isActive())
        self.assertFalse(self.lst._slow_pacing)

    def test_repeated_starting_keeps_the_first_snapshot(self):
        """重复起播不许覆盖 _bg_resume（否则播放结束后定时器回不来）。"""
        self.win._warmup_timer.start()
        self.win._resort_interval_timer.start()
        self.mpw.media_loaded = True
        self.mpw.playback_starting.emit()
        snapshot = self.win._bg_resume
        self.mpw.playback_starting.emit()
        self.assertEqual(snapshot, self.win._bg_resume)
        self.assertEqual((True, True), snapshot)

    def test_gate_is_active_before_the_panel_starts_filling(self):
        """顺序回归：open_playlist 里让路必须早于面板 set_playlist。"""
        order: list[tuple[str, bool, bool]] = []
        real_load = self.mpw.load
        real_set = self.viewer.panel.set_playlist

        def spy_load(path, start=None):            # 不碰真 libmpv
            order.append(("load", self.win._playback_active,
                          self.lst._slow_pacing))
            self.mpw.media_loaded = True
            self.mpw.playback_starting.emit()

        def spy_set_playlist(items, current):
            order.append(("panel", self.win._playback_active,
                          self.lst._slow_pacing))
            return real_set(items, current)

        self.viewer.panel.setVisible(True)
        self.mpw.load = spy_load
        self.viewer.panel.set_playlist = spy_set_playlist
        try:
            self.viewer.open_playlist(_items(6000), 100)
        finally:
            self.mpw.load = real_load
            self.viewer.panel.set_playlist = real_set
        self.assertEqual(["load", "panel"], [o[0] for o in order])
        self.assertEqual(("panel", True, True), order[1],
                         "面板开始填充时应已处于让路状态")


if __name__ == "__main__":
    unittest.main()
