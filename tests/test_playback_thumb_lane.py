"""播放让路车道：播放中缩略图不再全停，只剩视口单路慢速。

背景（用户实测）：播放中右边播放列表的视频缩略图一张不出——旧版
set_playback_active(True) 后 request 拒收一切视频单、视频 worker 全体
空挂，用户想换片却看不到每个视频是什么。修复：
- request 只拒收批量/预热单（prio >= WARMUP_PRIO），视口单（行号
  优先级，来自正在显示的面板/网格）照收；
- 通用两路视频 worker 空挂，只剩视口专用线程（thumb-vid-vp）取件：
  天然单路串行，两次抓帧之间再喘 PLAYBACK_PACE_S；
- _work 的第二道闸只放弃批量单，视口单豁免；
- 面板 delegate 请求带 priority=行号（此前是 None → 排在预热之后，
  也拿不到车道准入票），绘制后调 focus() 清废单对齐 TileView。
"""
import os
import sys
import tempfile
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

from PySide6.QtWidgets import QApplication

from app import thumbs as thumbs_mod
from app.media import MediaItem
from app.playlist_panel import ITEM_ROLE, MediaListWidget
from app.thumbs import WARMUP_PRIO, ThumbnailCache


def _vitem(name: str) -> MediaItem:
    return MediaItem(path=Path(f"I:/tgdl/{name}.mp4"), is_video=True,
                     size=1, mtime=0.0, is_archive=False)


def _iitem(name: str) -> MediaItem:
    return MediaItem(path=Path(f"I:/tgdl/{name}.jpg"), is_video=False,
                     size=1, mtime=0.0, is_archive=False)


class LaneWorkerTests(unittest.TestCase):
    """车道准入与单路串行（worker 层，_work 打桩不真解码）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.thumbs = ThumbnailCache()

    @classmethod
    def tearDownClass(cls):
        cls.thumbs.set_playback_active(False)

    def setUp(self):
        thumbs_mod.PLAYBACK_PACE_S = 0.0
        self.thumbs.set_playback_active(False)
        self.thumbs.invalidate_queue()

    def tearDown(self):
        self.thumbs.set_playback_active(False)
        self.thumbs.invalidate_queue()
        for attr in ("_work", "_work_inner"):
            if attr in self.thumbs.__dict__:
                delattr(self.thumbs, attr)

    def _record_work(self, work_ms: float = 0.0):
        done = []
        state = {"cur": 0, "max": 0}

        def stub(item, gen, prio=1e18):
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            if work_ms:
                time.sleep(work_ms / 1000.0)
            state["cur"] -= 1
            done.append((item.cache_key, prio))

        self.thumbs._work = stub
        return done, state

    def _wait(self, cond, timeout=8.0, what="条件"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.thumbs._prio_wake.set()   # 兜底：workers 最长 0.5s 一醒
            if cond():
                return
            time.sleep(0.01)
        self.fail(f"等待超时：{what}")

    def test_bulk_refused_viewport_accepted_during_playback(self):
        a, b = _vitem("a"), _vitem("b")
        self.thumbs.set_playback_active(True)
        self.thumbs.request(b)                    # 批量单（prio=None）
        self.assertEqual(0, self.thumbs.queued_count(),
                         "播放中批量/预热单必须拒收")
        self.thumbs.request(a, priority=3)        # 视口单（行号优先级）
        self.assertEqual(1, self.thumbs.queued_count(),
                         "播放中视口单必须收（这就是面板缩略图能出的前提）")

    def test_viewport_job_served_during_playback(self):
        a = _vitem("served")
        done, _ = self._record_work()
        self.thumbs.set_playback_active(True)
        self.thumbs.request(a, priority=0)
        self._wait(lambda: done, what="播放中视口单被车道消化")
        self.assertEqual([(a.cache_key, 0.0)], done)

    def test_lane_is_serialized_to_one_worker(self):
        """播放中所有视频单并发度恒为 1（通用线程空挂，只剩视口线程）。"""
        items = [_vitem(f"serial_{i}") for i in range(10)]
        done, state = self._record_work(work_ms=15)
        self.thumbs.set_playback_active(True)
        for i, it in enumerate(items):
            self.thumbs.request(it, priority=i)
        self._wait(lambda: len(done) >= len(items),
                   what="10 张视口单全部消化")
        self.assertEqual(1, state["max"],
                         f"播放中出现并发解码（max={state['max']}），"
                         "通用线程没有空挂")

    def test_bulk_job_parked_while_playing_resumes_after(self):
        """播放前已入队的批量单：播放中不出件、播放结束自动恢复。"""
        bulk = _vitem("bulk")
        done, _ = self._record_work()
        self.thumbs.request(bulk)                 # 播放前入队（prio=None）
        self.thumbs.set_playback_active(True)
        time.sleep(0.4)                           # 足够 worker 醒来扫一遍堆
        self.assertEqual([], done, "播放中批量单被出件了")
        self.assertEqual(1, self.thumbs.queued_count())
        self.thumbs.set_playback_active(False)
        self._wait(lambda: done, what="播放结束批量单恢复消化")

    def test_second_gate_spares_viewport_jobs_only(self):
        """_work 第二道闸：批量单放弃，视口单豁免（车道要服务的正是它们）。"""
        bulk, vp = _vitem("gate_bulk"), _vitem("gate_vp")
        inner = []
        self.thumbs._work_inner = lambda item, gen, key: inner.append(item.cache_key)
        gen = self.thumbs._generation
        self.thumbs._pending.update({bulk.cache_key, vp.cache_key})
        self.thumbs.set_playback_active(True)
        ThumbnailCache._work(self.thumbs, bulk, gen, WARMUP_PRIO)
        self.assertNotIn(bulk.cache_key, inner)
        self.assertNotIn(bulk.cache_key, self.thumbs._pending, "批量单应被放弃")
        ThumbnailCache._work(self.thumbs, vp, gen, 5.0)
        self.assertIn(vp.cache_key, inner, "视口单被第二道闸误杀")
        self.assertIn(vp.cache_key, self.thumbs._pending)

    def test_pace_constant_exists(self):
        self.assertIsInstance(thumbs_mod.PLAYBACK_PACE_S, float)
        self.assertGreaterEqual(thumbs_mod.PLAYBACK_PACE_S, 0.0)


class PanelRequestTests(unittest.TestCase):
    """面板侧：请求带行号优先级、绘制后 focus 清废单、播放中能入队。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.thumbs = ThumbnailCache()

    def setUp(self):
        thumbs_mod.PLAYBACK_PACE_S = 0.0
        self.thumbs.set_playback_active(False)
        self.thumbs.invalidate_queue()
        self.thumbs._work_inner = lambda item, gen, key: None  # 不真解码

    def tearDown(self):
        self.thumbs.set_playback_active(False)
        self.thumbs.invalidate_queue()
        if "_work_inner" in self.thumbs.__dict__:
            delattr(self.thumbs, "_work_inner")

    def _widget(self, n=500) -> MediaListWidget:
        # delegate 的 missing 守卫会跳过不存在文件的请求——用真实临时文件
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        paths = []
        for i in range(n):
            p = tmp / f"p_{i:04d}.mp4"
            p.write_bytes(b"x")
            paths.append(p)
        w = MediaListWidget(self.thumbs)
        w.resize(360, 800)
        items = [MediaItem(path=p, is_video=True,
                           size=1, mtime=0.0, is_archive=False)
                 for p in paths]
        w.set_items(items, playing=0)
        w.show()
        return w, items

    def test_delegate_requests_with_row_priority(self):
        """可见行按行号优先级入队（旧版 priority=None 排在预热之后）。"""
        w, items = self._widget()
        w.grab()                                   # 强制一次完整 paint
        prio = self.thumbs._key_prio
        self.assertEqual(0.0, prio[items[0].cache_key])
        self.assertEqual(5.0, prio[items[5].cache_key])
        w.deleteLater()
        self.app.processEvents()

    def test_playback_no_longer_blocks_visible_panel_rows(self):
        """核心回归：播放中绘制面板 → 可见视频行照常入队（旧版全被拒）。"""
        w, items = self._widget()
        self.thumbs.set_playback_active(True)
        w.grab()
        self.assertGreater(self.thumbs.queued_count(), 0,
                           "播放中面板可见行没能入队——车道准入失效")
        w.deleteLater()
        self.app.processEvents()

    def test_paint_focus_evicts_scrolled_out_requests(self):
        """绘制后清废单：滚出视口的排队请求被请出队列（车道不耗在屏外）。"""
        w, items = self._widget()
        far = _vitem("offscreen_row")
        self.thumbs.request(far, priority=5)       # 一张"屏外单"
        self.assertIn(far.cache_key, self.thumbs._pending)
        w.grab()
        self.assertNotIn(far.cache_key, self.thumbs._pending,
                         "绘制后的 focus 没有清掉屏外单")
        w.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
