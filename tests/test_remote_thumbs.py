"""网络盘视频缩略图：不整夹预热，只抓可见项；开关关掉则一张不抓。

背景（用户实测）：Y: 为 RaiDrive/CloudDrive 挂载的网盘。每张视频缩略图
进程只读 ~0.16%，但网盘工具预读让网卡实际下载 80~100MB；后台预热会
把整个文件夹都抓一遍（687 个视频 ≈ 60GB）。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.runtime import init_libmpv

try:
    init_libmpv()
except RuntimeError as exc:
    raise unittest.SkipTest(f"libmpv unavailable: {exc}")

from PySide6.QtWidgets import QApplication

from app import netpath
from app.config import settings
from app.media import MediaItem
from app.thumbs import WARMUP_PRIO, ThumbnailCache


def _item(drive: str, name: str, video: bool = True) -> MediaItem:
    ext = "mp4" if video else "jpg"
    return MediaItem(path=Path(f"{drive}:/media/{name}.{ext}"), is_video=video,
                     size=1, mtime=0.0, is_archive=False)


def _fake_remote(path) -> bool:
    return str(path).upper().startswith("Y:")


class RemoteThumbPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.thumbs = ThumbnailCache()

    def setUp(self):
        self._saved = settings["remote_video_thumbs"]
        settings["remote_video_thumbs"] = True
        self.thumbs.invalidate_queue()
        # 只看"是否入队"，不真解码：把 worker 取件前的队列状态作为判据
        self._p = mock.patch.object(netpath, "is_remote", _fake_remote)
        self._p.start()
        self._w = mock.patch.object(self.thumbs, "_prio_wake")
        self._w.start()

    def tearDown(self):
        self._w.stop()
        self._p.stop()
        settings["remote_video_thumbs"] = self._saved
        self.thumbs.invalidate_queue()

    def _queued(self, item) -> bool:
        with self.thumbs._lock:
            return item.cache_key in self.thumbs._pending

    def test_local_warmup_still_queued(self):
        it = _item("I", "local")
        self.thumbs.request(it, priority=WARMUP_PRIO)
        self.assertTrue(self._queued(it), "本地盘视频预热被误拒")

    def test_remote_warmup_refused(self):
        it = _item("Y", "warm")
        self.thumbs.request(it, priority=WARMUP_PRIO)
        self.assertFalse(self._queued(it), "网络盘视频仍在整夹预热")

    def test_remote_visible_accepted(self):
        it = _item("Y", "visible")
        self.thumbs.request(it, priority=3)          # 视口单：行号优先级
        self.assertTrue(self._queued(it), "网络盘可见视频没生成缩略图")

    def test_remote_disabled_refuses_visible_too(self):
        settings["remote_video_thumbs"] = False
        it = _item("Y", "off")
        self.thumbs.request(it, priority=3)
        self.assertFalse(self._queued(it), "开关关闭后仍在抓网络盘视频")

    def test_remote_images_unaffected(self):
        settings["remote_video_thumbs"] = False
        it = _item("Y", "pic", video=False)
        self.thumbs.request(it, priority=WARMUP_PRIO)
        self.assertTrue(self._queued(it), "网络盘图片被误拦")


class WarmupSkipsRemoteVideoTests(unittest.TestCase):
    def test_warmup_loop_skips_remote_video(self):
        src = (ROOT / "app" / "main_window.py").read_text(encoding="utf-8")
        body = src[src.index("def _warmup_tick"):src.index("def ", src.index("def _warmup_tick") + 10)]
        self.assertTrue("netpath.is_remote" in body,
                        "预热循环未跳过网络盘视频（会白烧队列水位）")


if __name__ == "__main__":
    unittest.main()
