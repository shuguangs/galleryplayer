"""音量：鼠标最多 100%，键盘 ↑ 才能放大到 130%。

需求（用户）：保持 130% 上限，但拖滑块/滚轮只到 100%；超过 100% 要按键盘 ↑；
鼠标移到音量条上时提示可以继续按 ↑。
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from app import volume_policy as vp

_app = QApplication.instance() or QApplication([])


class PolicyTests(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(vp.MOUSE_MAX, 100)
        self.assertEqual(vp.BOOST_MAX, 130)

    def test_mouse_clamped_to_100(self):
        self.assertEqual(vp.clamp(95 + 10, keyboard=False), 100)

    def test_mouse_never_pushes_boost_down_by_raising(self):
        """已在 120% 时滚轮向上不得把音量压回 100%（那等于越调越小）。"""
        self.assertEqual(vp.step(120, +5, keyboard=False), 120)

    def test_mouse_lowering_from_boost_still_works(self):
        self.assertEqual(vp.step(120, -5, keyboard=False), 115)

    def test_keyboard_goes_to_130(self):
        v = 100
        for _ in range(10):
            v = vp.step(v, +5, keyboard=True)
        self.assertEqual(v, 130)

    def test_keyboard_below_zero(self):
        self.assertEqual(vp.step(3, -5, keyboard=True), 0)

    def test_stored_value_clamped(self):
        """设置里存着 150（旧版上限）时，启动也不得超过 130。"""
        self.assertEqual(vp.clamp(150, keyboard=True), 130)


class _StubPreviewer(QObject):
    frame_ready = Signal(float, object)

    def cached(self, _t):
        return None

    def request(self, _t):
        pass


class SliderTests(unittest.TestCase):
    def setUp(self):
        from app.controls import ControlBar
        self.bar = ControlBar(_StubPreviewer())

    def test_slider_range_is_0_100(self):
        self.assertEqual((self.bar.vol.minimum(), self.bar.vol.maximum()), (0, 100))

    def test_boosted_volume_pins_slider_and_shows_value(self):
        self.bar.set_volume(120, False)
        self.assertEqual(self.bar.vol.value(), 100)
        self.assertIn("120", self.bar.vol.toolTip())

    def test_tooltip_mentions_up_key_at_full(self):
        self.bar.set_volume(100, False)
        tip = self.bar.vol.toolTip()
        self.assertIn("↑", tip)
        self.assertIn("130", tip)

    def test_pinned_slider_emits_nothing(self):
        """120% 时把滑块同步到 100 不得回写音量（否则一显示就被压回 100%）。"""
        got = []
        self.bar.volume_selected.connect(got.append)
        self.bar.set_volume(120, False)
        self.assertEqual(got, [])


class WiringTests(unittest.TestCase):
    def test_viewer_uses_policy(self):
        src = (Path(__file__).resolve().parent.parent / "app" / "viewer.py").read_text(encoding="utf-8")
        body = src[src.index("def _adjust_volume"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertTrue("volume_policy" in body or "vp." in body,
                        "_adjust_volume 未走统一策略")

    def test_widget_clamps_to_130(self):
        """底层 set_volume 是最后一道闸：任何调用方传 150 都只能落到 130。"""
        from types import SimpleNamespace

        from app.config import settings
        from app.mpv_widget import MpvWidget

        saved = settings["volume"]
        fake = SimpleNamespace(mpv=SimpleNamespace(volume=0.0))
        try:
            MpvWidget.set_volume(fake, 150.0)
            self.assertEqual(fake.mpv.volume, 130.0, "底层允许超过 130%")
            MpvWidget.set_volume(fake, -5.0)
            self.assertEqual(fake.mpv.volume, 0.0)
        finally:
            settings["volume"] = saved


if __name__ == "__main__":
    unittest.main()
