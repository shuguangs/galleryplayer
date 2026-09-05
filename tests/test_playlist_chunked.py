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
from app.playlist_panel import (ITEM_ROLE, ROW_H_COMPACT, ROW_H_THUMB,
                                MediaListWidget)
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


class AppliedMirrorTests(unittest.TestCase):
    """留档快路径 / 填充中续填 / 统一行高（起播掉帧的三处真凶）。

    背景 bug（用户实测 + 日志实锤，309k 项文件夹）：
    - `增量同步 125000→309054 行 2058ms`：set_items 开头先 stop() 了填充
      定时器，紧随其后的 `not self.is_filling` 守卫因此永远为真，于是拿
      只填了一半的控件当"旧列表"做增量对比，把没填的 18.4 万行同步插了
      进去——2.1s 硬冻结，正好砸在点开视频那一刻（mpv 画面同在 GUI 线程
      渲染，见 mpv_widget.MpvWidget.paintGL）。
    - 同序同项也要从控件回读三十万行做对比：每次点开视频 420-510ms。
    - uniformItemSizes 关着：QListView 每插一批行都重算全表行位置，30 万
      行分块填充实测 109.3s GUI 时间（开了 4.5s）——播放期间每隔 1 秒一条
      gui-stall 的来源。单块 addItem 只 7-13ms，自适应减半量不到它。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.thumbs = ThumbnailCache()

    def _widget(self) -> MediaListWidget:
        w = MediaListWidget(self.thumbs)
        w.resize(360, 800)
        return w

    def _pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.002)

    def _wait_fill(self, w: MediaListWidget, total: int, timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not w.is_filling and w.count() == total:
                return
            time.sleep(0.005)
        self.fail(f"填充超时: {w.count()}/{total}")

    def _assert_order(self, w: MediaListWidget, src: list[MediaItem]) -> None:
        for i in (0, 1, len(src) // 3, len(src) // 2, len(src) - 2, len(src) - 1):
            self.assertIs(src[i], w.item(i).data(ITEM_ROLE),
                          f"行号≠序号（第 {i} 行）：双击会播错文件")

    def test_uniform_item_sizes_on_and_density_still_switches(self):
        w = self._widget()
        self.assertTrue(w.uniformItemSizes(), "关掉它 30 万行填充要多花 100s")
        w.show()
        w.set_items(_items(SMALL), playing=0)
        self.assertEqual(ROW_H_THUMB, w.visualItemRect(w.item(3)).height())
        w.set_thumb_mode(False)
        self.app.processEvents()
        self.assertEqual(ROW_H_COMPACT, w.visualItemRect(w.item(3)).height())
        w.set_thumb_mode(True)
        self.app.processEvents()
        self.assertEqual(ROW_H_THUMB, w.visualItemRect(w.item(3)).height())

    def test_same_content_again_is_cheap_and_keeps_filling(self):
        """同序同项再来一次：只换播放行，且绝不打断正在跑的填充。

        打断＝列表永远停在半成品（旧版的等长"无变化"分支会清掉填充状态）。
        """
        w = self._widget()
        items = _items(BIG)
        w.set_items(items, playing=0)
        self._pump(0.25)
        mid = w.count()
        self.assertLess(mid, BIG)
        t0 = time.perf_counter()
        w.set_items(list(items), playing=7)      # 新 list 对象，同一批元素
        took = (time.perf_counter() - t0) * 1000
        self.assertLess(took, 120, f"留档快路径没命中：{took:.0f}ms")
        self.assertEqual(7, w.playing_row)
        self.assertTrue(w.is_filling, "填充被打断，列表会永远停在半成品")
        self._wait_fill(w, BIG)
        self._assert_order(w, items)

    def test_midfill_tail_append_resumes_instead_of_sync_catchup(self):
        """填充中尾部追加：原地续填，不许同步补齐（旧版 2.1s 冻结）。"""
        w = self._widget()
        items = _items(BIG)
        w.set_items(items[:BIG // 2], playing=0)
        self._pump(0.25)
        mid = w.count()
        t0 = time.perf_counter()
        w.set_items(items, playing=0)
        took = (time.perf_counter() - t0) * 1000
        self.assertLess(took, 250, f"同步补齐没被挡住：{took:.0f}ms")
        self.assertGreaterEqual(w._fill_pos, mid, "续填应保留已填进度")
        self._wait_fill(w, BIG)
        self._assert_order(w, items)

    def test_midfill_reorder_falls_back_and_order_is_correct(self):
        """填充中顺序变了：回退分块替换，最终行号==序号。"""
        w = self._widget()
        items = _items(BIG)
        w.set_items(items, playing=0)
        self._pump(0.25)
        shuffled = list(reversed(items))
        t0 = time.perf_counter()
        w.set_items(shuffled, playing=0)
        # 必须走分块替换分支（绝不同步补齐/同步 clear）。时序断言只做宽松
        # 护栏：满套件跑下来 gen2 GC 偶发秒级暂停会误伤精确时序
        self.assertTrue(w._replace_phase, "途中重排未走分块替换分支")
        self.assertLess((time.perf_counter() - t0) * 1000, 1500)
        self._wait_fill(w, BIG)
        self._assert_order(w, shuffled)

    def test_incremental_sync_still_applies_on_complete_list(self):
        """控件填满后的纯新增/纯减少仍走增量同步（不整表重填）。"""
        w = self._widget()
        base = _items(SMALL)
        w.set_items(base, playing=0)
        grown = base[:100] + _items(5) + base[100:]
        w.set_items(grown, playing=0)
        self.assertEqual(len(grown), w.count())
        self._assert_order(w, grown)
        shrunk = [it for i, it in enumerate(grown) if i % 7]
        w.set_items(shrunk, playing=0)
        self.assertEqual(len(shrunk), w.count())
        self._assert_order(w, shrunk)

    def test_user_edited_rows_invalidate_the_mirror(self):
        """用户改过行 → 留档作废 → 同内容也要重新对齐（不能判成"没变"）。"""
        w = self._widget()
        items = _items(SMALL)
        w.set_items(items, playing=0)
        w.insertItem(10, w.takeItem(3))          # 模拟上下移动/拖拽
        w.invalidate_applied()
        self.assertIsNone(w._applied)
        w.set_items(items, playing=0)
        self._assert_order(w, items)

    def test_deferred_fill_invalidates_mirror_when_incomplete(self):
        """半成品被推迟：留档不能再声称控件已是完整内容。"""
        w = self._widget()
        items = _items(BIG)
        w.set_items(items, playing=0)
        self._pump(0.2)
        self.assertLess(w.count(), BIG)
        w.mark_deferred(5)
        self.assertIsNone(w._applied)
        w.take_deferred_playing()
        w.set_items(items, playing=5)
        self._wait_fill(w, BIG)
        self.assertEqual(BIG, w.count())
        self._assert_order(w, items)


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
