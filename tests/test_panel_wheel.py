"""滚轮越过列表边界不得切换视频（用户实测 bug）。

场景：播放界面右侧文件列表滚到最底（或最顶）后继续滚鼠标滚轮，播放器会
跳到下一部/上一部视频。

根因：MediaListWidget 用 QAbstractScrollArea 的默认 wheelEvent——列表滚到
边界后事件被 ignore，Qt 把它冒泡给父窗口；父窗口正是 Viewer，而 Viewer 的
滚轮语义就是「上一部 / 下一部」（Telegram 风格）。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PLAYER_AUTOMATION", "1")

from app.runtime import init_libmpv  # noqa: E402

try:
    init_libmpv()
except RuntimeError as exc:
    raise unittest.SkipTest(f"libmpv unavailable: {exc}")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.media import MediaItem  # noqa: E402
from app.playlist_panel import MediaListWidget, PlaylistPanel  # noqa: E402
from app.thumbs import ThumbnailCache  # noqa: E402


def wheel(widget, dy: int = -120):
    """把滚轮事件送给控件（走 QApplication.notify，具备父级冒泡语义）。"""
    ev = QWheelEvent(
        QPointF(widget.rect().center()), QPointF(widget.rect().center()),
        QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier,
        Qt.NoScrollPhase, False,
    )
    QApplication.sendEvent(widget, ev)
    return ev


class _FakeViewer:
    """替身父窗口：记录收到的滚轮（真实 Viewer 收到就会切视频）。"""

    def __init__(self):
        self.wheels = 0


class ListWheelBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.thumbs = ThumbnailCache()

    def _items(self, n: int) -> list[MediaItem]:
        return [MediaItem(path=Path(f"I:/v/v{i}.mp4"), is_video=True, size=1,
                          mtime=0.0, is_archive=False) for i in range(n)]

    def test_list_accepts_wheel_at_bottom_boundary(self):
        """列表滚到底再滚：事件必须被列表吃掉，不再冒泡给父窗口。"""
        from PySide6.QtWidgets import QWidget

        host = QWidget()
        host.resize(400, 300)
        lst = MediaListWidget(self.thumbs, host)
        lst.setGeometry(0, 0, 400, 300)
        lst.set_items(self._items(30), playing=0)
        host.show()
        self.app.processEvents()

        bar = lst.verticalScrollBar()
        bar.setValue(bar.maximum())          # 拉到底
        self.app.processEvents()

        before = bar.value()
        ev = wheel(lst.viewport(), dy=-120)  # 继续往下滚
        self.assertTrue(ev.isAccepted(),
                        "列表滚到底后滚轮事件被 ignore —— 会冒泡给播放器切视频")
        self.assertEqual(before, bar.value())  # 到底了滚不动是正常的
        lst.deleteLater()
        host.deleteLater()

    def test_list_accepts_wheel_at_top_boundary(self):
        host = __import__("PySide6.QtWidgets", fromlist=["QWidget"]).QWidget()
        host.resize(400, 300)
        lst = MediaListWidget(self.thumbs, host)
        lst.setGeometry(0, 0, 400, 300)
        lst.set_items(self._items(30), playing=0)
        host.show()
        self.app.processEvents()
        lst.verticalScrollBar().setValue(0)   # 拉到顶
        self.app.processEvents()
        ev = wheel(lst.viewport(), dy=120)
        self.assertTrue(ev.isAccepted(), "列表滚到顶后滚轮事件被 ignore")
        lst.deleteLater()
        host.deleteLater()

    def test_panel_swallows_leftover_wheel(self):
        """面板空白处/搜索框/标签栏上的滚轮也不许漏给播放器。"""
        panel = PlaylistPanel(self.thumbs)
        panel.resize(320, 600)
        panel.set_playlist(self._items(20), 0)
        panel.show()
        self.app.processEvents()
        ev = wheel(panel, dy=-120)
        self.assertTrue(ev.isAccepted(), "面板把滚轮漏给了父窗口（=切换视频）")
        panel.deleteLater()


class ViewerWheelRoutingTests(unittest.TestCase):
    """端到端：真 Viewer + 真面板，列表到底继续滚不得切视频。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_wheel_at_list_bottom_does_not_switch_media(self):
        from app.viewer import Viewer

        viewer = Viewer(ThumbnailCache())
        items = [MediaItem(path=Path(f"I:/v/v{i}.mp4"), is_video=True, size=1,
                           mtime=0.0, is_archive=False) for i in range(30)]
        # 不真的去载媒体：只验证滚轮路由，换片逻辑打桩
        with mock.patch.object(Viewer, "step") as step, \
                mock.patch.object(Viewer, "show_index"):
            viewer.open_playlist(items, 0)
            self.app.processEvents()
            lst = viewer.panel.list
            bar = lst.verticalScrollBar()
            bar.setValue(bar.maximum())
            self.app.processEvents()
            step.reset_mock()
            wheel(lst.viewport(), dy=-120)   # 列表到底后继续往下滚
            self.app.processEvents()
            step.assert_not_called()
        viewer.close()
        viewer.deleteLater()


if __name__ == "__main__":
    unittest.main()
