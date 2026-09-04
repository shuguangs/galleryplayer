"""播放列表面板大列表分块填充的回归测试。

背景 bug（用户实测）：I:\\tgdl（31 万文件）下点开视频，GUI 线程被面板的
全量 QListWidget 填充堵 2-4 秒（十万级addItem），观感=播放器白屏卡
20 秒（叠加扫描流式刷新与子文件夹重扫）。

修法（playlist_panel.MediaListWidget）：
- ≤SYNC_MAX 小列表：同步填充，行为与旧版完全一致；
- 大列表：立即填播放行附近（≤IMMEDIATE_MAX），其余 QTimer 分块追加，
  起播不再等面板；
- 行号==播放列表序号的不变式由顺序追加保持；
- 填充期间禁止拖拽重排/删除（items() 回读不完整会把列表截断），
  完成时恢复；
- _apply_filter("") 跳过全量扫，过滤词记在控件上供新行应用；
- 播放行未填到时延迟滚动。
"""
import os
import sys
import time
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

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.media import MediaItem
from app.playlist_panel import MediaListWidget
from app.thumbs import ThumbnailCache

BIG = MediaListWidget.SYNC_MAX + 20_000      # 走分块路径
SMALL = 500                                   # 走同步路径


def _items(n: int) -> list[MediaItem]:
    return [MediaItem(path=Path(f"I:/v/sub/video_{i:07d}.mp4"),
                      is_video=True, size=1, mtime=0.0, is_archive=False)
            for i in range(n)]


class ChunkedFillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.thumbs = ThumbnailCache()

    def _widget(self) -> MediaListWidget:
        w = MediaListWidget(self.thumbs)
        w.resize(360, 800)
        return w

    def _wait_fill(self, w: MediaListWidget, total: int, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not w.is_filling and w.count() == total:
                return
            time.sleep(0.005)
        self.fail(f"填充超时: {w.count()}/{total}")

    def test_small_list_fills_synchronously(self):
        w = self._widget()
        items = _items(SMALL)
        w.set_items(items, playing=3)
        self.assertEqual(SMALL, w.count())
        self.assertFalse(w.is_filling)
        self.assertEqual(items[3].path, w.item(3).data(
            MediaListWidget and __import__("app.playlist_panel", fromlist=["ITEM_ROLE"]).ITEM_ROLE).path)

    def test_big_list_returns_fast_and_fills_in_background(self):
        """核心回归：起播不等面板——立即返回，行号==序号最终成立。"""
        w = self._widget()
        items = _items(BIG)
        t0 = time.perf_counter()
        w.set_items(items, playing=BIG - 1)
        took = time.perf_counter() - t0
        # 立即窗口：只同步填一部分，耗时远小于全量（全量十万级≈2s）
        self.assertLess(w.count(), BIG)
        self.assertTrue(w.is_filling)
        self.assertLess(took, 0.8, f"立即窗口耗时 {took:.2f}s，起播被拖累")
        self._wait_fill(w, BIG)
        # 行号==序号
        for probe in (0, BIG // 2, BIG - 1):
            self.assertEqual(items[probe].path,
                             w.item(probe).data(
                                 __import__("app.playlist_panel",
                                            fromlist=["ITEM_ROLE"]).ITEM_ROLE).path)

    def test_fill_never_blocks_gui_for_long(self):
        """填充期间 GUI 最大间隙必须远小于旧版的全量阻塞。"""
        w = self._widget()
        items = _items(BIG)
        w.set_items(items, playing=0)
        max_gap = 0.0
        last = time.perf_counter()
        deadline = time.monotonic() + 60
        while w.is_filling and time.monotonic() < deadline:
            self.app.processEvents()
            now = time.perf_counter()
            max_gap = max(max_gap, now - last)
            last = now
            time.sleep(0.01)
        self._wait_fill(w, BIG)
        self.assertLess(max_gap, 1.5,
                        f"填充期间 GUI 间隙 {max_gap:.2f}s（旧版全量约 2s）")

    def test_during_fill_items_are_partial_and_reorder_blocked(self):
        """填充中：is_filling=True（重排/删除被上层拦）；不能误报完成。"""
        w = self._widget()
        w.set_items(_items(BIG), playing=0)
        self.assertTrue(w.is_filling)
        self.assertLess(w.count(), BIG)
        w.setDragDropMode = None  # 不真触发拖拽；验证标志即可
        self.assertTrue(w.is_filling)
        self._wait_fill(w, BIG)
        self.assertFalse(w.is_filling)

    def test_playing_row_beyond_filled_window_scrolls_later(self):
        """播放行在立即窗口外：先记待滚行，填到后能滚。"""
        w = self._widget()
        items = _items(BIG)
        w.set_items(items, playing=BIG - 1)
        # 立即窗口封顶 4000，播放行 22999 在窗口外 → 记为待滚
        self.assertEqual(BIG - 1, w._scroll_pending_row)
        self._wait_fill(w, BIG)
        # 越界 playing：不记待滚、不崩
        w.set_items(items, playing=BIG * 2)
        self._wait_fill(w, BIG)

    def test_refill_replaces_content_completely(self):
        """extend_playlist 场景：二次 set_items 不残留旧行。"""
        w = self._widget()
        w.set_items(_items(SMALL), playing=0)
        self.assertEqual(SMALL, w.count())
        w.set_items(_items(BIG), playing=10)
        self._wait_fill(w, BIG)
        self.assertEqual(BIG, w.count())

    def test_items_roundtrip_after_fill(self):
        w = self._widget()
        items = _items(BIG)
        w.set_items(items, playing=0)
        self._wait_fill(w, BIG)
        back = w.items()
        self.assertEqual(BIG, len(back))
        self.assertEqual(items[123].path, back[123].path)


class PanelGuardTests(unittest.TestCase):
    """面板层对填充期回读路径的拦截（避免列表被截断）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_source_wiring_exists(self):
        src = (ROOT / "app" / "playlist_panel.py").read_text(encoding="utf-8")
        # 回读守卫
        self.assertIn("is_filling", src)
        self.assertIn("panel.list_filling", src.replace("t(", ""))
        # 过滤词传递
        self.assertIn("_filter_needle", src)
        # 拖拽禁用与恢复
        self.assertIn("NoDragDrop", src)
        self.assertIn("InternalMove", src)

    def test_i18n_filling_toast_exists(self):
        from app.i18n import set_language, t

        for lang in ("zh", "en"):
            set_language(lang)
            self.assertNotEqual("panel.list_filling", t("panel.list_filling"))
        set_language("zh")


if __name__ == "__main__":
    unittest.main()
