"""mpv 属性写幂等化的回归测试（换片卡顿的实测根因之一）。

背景：`mpv[key] = value` 在 python-mpv 里是**同步阻塞**的——空闲时 0.02ms，
但紧跟在 loadfile 前后（核心正在换片）实测 200ms~1.5s。旧代码每次换片都
重写整批缓存属性（值根本没变）、写 speed 还要读回一次、并清两次 A-B
标记，全部撞在这个最忙的窗口里。

修法：把"我们下发过什么"记成影子值，值没变就不写。这里用替身 mpv 对象
验证"没变就不写 / 变了才写"，不需要真起 mpv 实例。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _FakeMpv:
    """记录属性写次数的替身（字典式与属性式写都要拦：python-mpv 两种都用）。"""

    def __init__(self):
        object.__setattr__(self, "writes", [])
        object.__setattr__(self, "commands", [])
        object.__setattr__(self, "_vals", {})

    def __setattr__(self, key, value):
        if key.startswith("_"):
            object.__setattr__(self, key, value)
            return
        self.writes.append((key, value))
        self._vals[key] = value

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        return self._vals.get(key)          # 未设过 → None（与 mpv 未就绪一致）

    def __setitem__(self, key, value):
        self.writes.append((key, value))
        self._vals[key] = value

    def __getitem__(self, key):
        return self._vals.get(key)

    def command(self, *args):
        self.commands.append(args)

    def loadfile(self, path, mode="replace"):
        self.commands.append(("loadfile", path, mode))


class MpvWriteIdempotenceTests(unittest.TestCase):
    """直接驱动 MpvWidget 的方法，避开真 mpv/GL（只验证写策略）。"""

    def _widget(self):
        from app.mpv_widget import MpvWidget

        w = MpvWidget.__new__(MpvWidget)     # 不跑 __init__（会起真 mpv）
        w.mpv = _FakeMpv()
        w._cache_mode = None
        w._pause_intent = None
        w._speed_intent = None
        w._ab_a_set = False
        w._ab_b_set = False
        w._pending_seek = None
        w.media_loaded = False
        w.playback_starting = mock.MagicMock()
        # 信号也要替身：__new__ 造出来的实例没有 C++ 对象，emit 会报
        # "Signal source has been deleted"
        w.pause_changed = mock.MagicMock()
        w.speed_changed = mock.MagicMock()
        return w

    def _count(self, w, key):
        return sum(1 for k, _ in w.mpv.writes if k == key)

    # ---- speed

    def test_speed_written_once_then_skipped(self):
        from app.config import settings

        w = self._widget()
        settings["speed"] = 1.0
        w.set_speed(1.0)
        self.assertEqual(1, self._count(w, "speed"))
        w.set_speed(1.0)          # 没变 → 不再写（换片窗口内不再阻塞）
        w.set_speed(1.0)
        self.assertEqual(1, self._count(w, "speed"))
        w.set_speed(2.0)          # 变了 → 写
        self.assertEqual(2, self._count(w, "speed"))
        settings["speed"] = 1.0

    def test_speed_clamped_and_stored(self):
        from app.config import settings

        w = self._widget()
        w.set_speed(99.0)
        self.assertEqual(8.0, w.mpv.writes[-1][1])
        self.assertEqual(8.0, settings["speed"])
        settings["speed"] = 1.0

    # ---- pause

    def test_pause_written_once_then_skipped(self):
        w = self._widget()
        w.set_pause(True)
        self.assertEqual(1, self._count(w, "pause"))
        w.set_pause(True)
        self.assertEqual(1, self._count(w, "pause"))
        w.set_pause(False)
        self.assertEqual(2, self._count(w, "pause"))

    def test_toggle_uses_shadow_not_readback(self):
        w = self._widget()
        w.set_pause(False)                    # intent=False
        w.toggle_pause()                      # → True
        self.assertEqual(True, w.mpv.writes[-1][1])
        w.toggle_pause()                      # → False
        self.assertEqual(False, w.mpv.writes[-1][1])

    def test_observed_pause_updates_intent(self):
        """EOF 自动暂停后 intent 必须跟上，否则下次换片不会自动播。"""
        w = self._widget()
        w._on_pause_observed(True)
        self.assertTrue(w._pause_intent)
        w.set_pause(False)                    # 恢复播放要真写
        self.assertEqual(False, w.mpv.writes[-1][1])

    # ---- A-B loop

    def test_clear_ab_loop_is_noop_when_never_set(self):
        w = self._widget()
        w.clear_ab_loop()
        self.assertEqual([], w.mpv.writes,
                         "从没设过 A-B 就不该写（每次换片白等两次同步写）")

    def test_clear_ab_loop_writes_after_set(self):
        w = self._widget()
        w.set_ab_loop("a", 10.0)
        w.set_ab_loop("b", 20.0)
        w.mpv.writes.clear()
        w.clear_ab_loop()
        self.assertEqual(2, len(w.mpv.writes))
        self.assertEqual([("ab-loop-a", "no"), ("ab-loop-b", "no")],
                         w.mpv.writes)
        w.mpv.writes.clear()
        w.clear_ab_loop()                     # 已清过 → 再清是 no-op
        self.assertEqual([], w.mpv.writes)

    # ---- 缓存档位

    def test_cache_props_written_only_on_mode_change(self):
        w = self._widget()
        local = Path("I:/v/a.mp4")

        with mock.patch("app.mpv_widget.netpath.is_remote", return_value=False):
            w.load(local)                     # 首次：写 local 档
            first = len(w.mpv.writes)
            self.assertGreater(first, 0)
            w.load(local)                     # 同档：不再写
            w.load(local)
            self.assertEqual(first, len(w.mpv.writes),
                             "同一缓存档位重复换片不该重写（实测单次 200ms+）")
            with mock.patch("app.mpv_widget.netpath.is_remote",
                            return_value=True):
                w.load(local)                 # 切到 remote 档 → 写一次
            self.assertGreater(len(w.mpv.writes), first)

    def test_load_sets_pause_false_once(self):
        w = self._widget()
        p = Path("I:/v/a.mp4")
        with mock.patch("app.mpv_widget.netpath.is_remote", return_value=False):
            w.load(p)
            w.load(p)
        self.assertEqual(1, self._count(w, "pause"),
                         "pause=False 只在需要时写一次")


if __name__ == "__main__":
    unittest.main()
