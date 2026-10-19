"""播放器滚轮切片节流 + 重名文件不丢。

需求（用户）：随手滚一下跳三四个视频太敏感——优先跳一个，滚轮还在滚才继续。
另：含子文件夹浏览时，两个子文件夹里同名的文件都要显示，不能只剩一个。
"""
import tempfile
import unittest
from pathlib import Path

from app import media
from app.wheel_policy import GAP_S, REPEAT_S, WheelStepper


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class WheelTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.w = WheelStepper(self.clock)

    def flick(self, notches, dy=-120, dt=0.01):
        out = []
        for _ in range(notches):
            out.append(self.w.feed(dy))
            self.clock.t += dt
        return [d for d in out if d]

    def test_one_flick_of_four_notches_steps_once(self):
        self.assertEqual(self.flick(4), [1])

    def test_first_notch_steps_immediately(self):
        self.assertEqual(self.w.feed(-120), 1)

    def test_up_is_previous(self):
        self.assertEqual(self.w.feed(120), -1)

    def test_keeps_stepping_while_scrolling(self):
        # 持续滚 2 秒（每 50ms 一格）：首格 + 之后约每 REPEAT_S 一部
        steps = self.flick(40, dt=0.05)
        self.assertGreaterEqual(len(steps), 3)
        self.assertLessEqual(len(steps), int(2.0 / REPEAT_S) + 1)

    def test_pause_starts_new_gesture(self):
        self.assertEqual(self.flick(3), [1])
        self.clock.t += GAP_S + 0.05
        self.assertEqual(self.flick(3), [1])

    def test_reverse_steps_back_immediately(self):
        self.assertEqual(self.flick(3), [1])
        self.assertEqual(self.w.feed(120), -1)

    def test_touchpad_small_deltas_accumulate(self):
        self.assertEqual(self.flick(3, dy=-30), [])
        self.assertEqual(self.w.feed(-30), 1)


class DuplicateNameTests(unittest.TestCase):
    def test_same_name_in_two_subfolders_both_kept(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for sub in ("A", "B"):
                (root / sub).mkdir()
                (root / sub / "5.mp4").write_bytes(b"x")
            items = media.scan_folder(root, recursive=True, use_cache=False)
            paths = sorted(str(i.path.relative_to(root)) for i in items)
            self.assertEqual(paths, [str(Path("A/5.mp4")), str(Path("B/5.mp4"))])
            for key in ("name", "custom", "mtime", "size"):
                out = media.sort_items(items, key, False, 0, ["5.mp4"])
                self.assertEqual(len(out), 2, key)
                out = media.group_sort_items(items, root, "name", False,
                                             key, False, 0, ["5.mp4"])
                self.assertEqual(len(out), 2, key)


if __name__ == "__main__":
    unittest.main()
