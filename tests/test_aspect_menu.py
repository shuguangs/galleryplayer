"""视频右键菜单：画面比例子菜单 + 全屏右键事件传播。

背景 bug（用户实测）：全屏播放时右键无反应。事件链路本身是通的——
离屏探针证实全屏与窗口化都能把 contextMenuEvent 经子控件送达
Viewer——真机上是弹出的 QMenu 被全屏窗口压在下面（不可见＝无法右键）；
修复：全屏时给菜单加 WindowStaysOnTopHint。

新功能：右键菜单新增"画面比例"子菜单——默认（跟随源）/ 4:3 / 16:9 /
16:10 / 21:9 / 1:1 / 2.35:1 / 拉伸铺满窗口（-1）/ 裁切铺满窗口
（aspect no + panscan 1）。坑：mpv 把 video-aspect-override 以浮点
回报（no→-2.0、"4:3"→1.333…），current_aspect_mode 按容差映射回
规范键，菜单勾选态直接读 mpv，不另存状态。
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.runtime import init_libmpv

try:
    init_libmpv()
except RuntimeError as exc:
    raise unittest.SkipTest(f"libmpv unavailable: {exc}")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication

from app.i18n import set_language, t
from app.main_window import MainWindow
from app.media import MediaItem

MODES = ("no", "4:3", "16:9", "16:10", "21:9", "1:1", "2.35", "-1", "cropfill")


class AspectMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.win = MainWindow()
        cls.viewer = cls.win.ensure_viewer()
        v = cls.viewer
        v.items = [MediaItem(path=Path("I:/sample.mp4"), is_video=True,
                             size=1, mtime=0.0, is_archive=False)]
        v.index = 0
        v.stack.setCurrentWidget(v.video_view)
        v.panel.setVisible(False)

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()
        cls.win.close()

    def setUp(self):
        self.mpw = self.viewer.video_view
        self.mpw.set_aspect_mode("no")
        self.viewer.showNormal()
        self.viewer.resize(1280, 700)
        for _ in range(5):
            self.app.processEvents()

    def _aspect_actions(self) -> dict[str, object]:
        """画面比例子菜单的 {文字: action}。

        菜单本体必须留在 self 上：只攥着子菜单 action 的引用、让外层
        menu 包装器被回收时，PySide6 会连带删掉 C++ 侧的子菜单。
        """
        self._menu_holder = self.viewer._build_media_menu()
        out = {}
        for act in self._menu_holder.actions():
            sub = act.menu()
            if sub is not None:
                for a in sub.actions():
                    if a.text():
                        out.setdefault(a.text(), a)
        return out

    def test_aspect_mode_roundtrip_all_modes(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.mpw.set_aspect_mode(mode)
                self.assertEqual(mode, self.mpw.current_aspect_mode())

    def test_default_state_follows_source(self):
        """初始（未设置过）读回为默认跟随源，菜单勾在"默认"上。"""
        self.assertEqual("no", self.mpw.current_aspect_mode())
        actions = self._aspect_actions()
        self.assertTrue(actions[t("viewer.aspect_default")].isChecked())

    def test_menu_lists_all_choices(self):
        actions = self._aspect_actions()
        for text in (t("viewer.aspect_default"), "4:3", "16:9", "16:10",
                     "21:9", "1:1", "2.35:1（宽银幕）",
                     t("viewer.aspect_stretch"), t("viewer.aspect_cropfill")):
            self.assertIn(text, actions, f"子菜单缺 {text}")

    def test_triggering_ratio_action_applies_to_mpv(self):
        actions = self._aspect_actions()
        actions["4:3"].trigger()
        self.assertEqual("4:3", self.mpw.current_aspect_mode())
        actions[t("viewer.aspect_cropfill")].trigger()
        self.assertEqual("cropfill", self.mpw.current_aspect_mode())
        self.assertAlmostEqual(1.0, float(self.mpw.mpv["panscan"]))
        actions[t("viewer.aspect_default")].trigger()
        self.assertEqual("no", self.mpw.current_aspect_mode())
        self.assertAlmostEqual(0.0, float(self.mpw.mpv["panscan"]))

    def test_checkmark_tracks_current_mode(self):
        self.mpw.set_aspect_mode("21:9")
        actions = self._aspect_actions()
        checked = [text for text, a in actions.items() if a.isChecked()]
        self.assertEqual(["21:9"], checked)

    def test_context_menu_reaches_handler_windowed_and_fullscreen(self):
        """全屏右键事件传播回归：必须与窗口化一样到达 Viewer 处理器。"""
        for mode in ("normal", "full"):
            with self.subTest(mode=mode):
                if mode == "full":
                    self.viewer.showFullScreen()
                for _ in range(5):
                    self.app.processEvents()
                pos = QPoint(self.viewer.width() // 2,
                             self.viewer.height() // 2)
                child = self.viewer.childAt(pos)
                hit = []
                orig = self.viewer.contextMenuEvent
                self.viewer.contextMenuEvent = lambda e: hit.append(1)
                try:
                    ev = QContextMenuEvent(
                        QContextMenuEvent.Mouse, child.mapFrom(self.viewer, pos),
                        self.viewer.mapToGlobal(pos))
                    self.app.sendEvent(child, ev)
                    for _ in range(3):
                        self.app.processEvents()
                finally:
                    self.viewer.contextMenuEvent = orig
                self.assertTrue(hit, f"{mode} 下右键事件未到达处理器")
                if mode == "full":
                    self.viewer.showNormal()
                    for _ in range(3):
                        self.app.processEvents()

    def test_menu_builds_in_english(self):
        set_language("en")
        try:
            menu = self.viewer._build_media_menu()
            texts = [a.text() for a in menu.actions() if a.text()]
            self.assertTrue(any("Aspect" in x for x in texts))
        finally:
            set_language("zh")


if __name__ == "__main__":
    unittest.main()
