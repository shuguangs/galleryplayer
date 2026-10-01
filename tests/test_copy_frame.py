"""播放器右键"截取当前画面到剪贴板"（暂停/播放都可用）。

需求（用户）：视频播放窗口右键直接截当前画面并复制到剪贴板，不落盘。
"""
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication

from app import fileops

_app = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent


class CopyPilImageTests(unittest.TestCase):
    def test_frame_lands_on_clipboard_at_full_size(self):
        img = Image.new("RGB", (1280, 720), (10, 200, 30))
        self.assertTrue(fileops.copy_pil_image_to_clipboard(img))
        got = QApplication.clipboard().image()
        self.assertFalse(got.isNull(), "剪贴板里没有图片")
        self.assertEqual((got.width(), got.height()), (1280, 720), "截图被缩放了")
        c = got.pixelColor(640, 360)
        self.assertEqual((c.red(), c.green(), c.blue()), (10, 200, 30), "像素颜色不对")

    def test_none_frame_reports_failure(self):
        self.assertFalse(fileops.copy_pil_image_to_clipboard(None))


class ViewerActionTests(unittest.TestCase):
    """_copy_frame_to_clipboard：取帧 → 剪贴板 → 提示；取不到帧给失败提示。"""

    def _viewer_stub(self, frame):
        from app.viewer import Viewer

        toasts = []
        stub = SimpleNamespace(
            video_view=SimpleNamespace(grab_frame=lambda: frame),
            _current_is_video=lambda: True,
            _show_toast=toasts.append,
        )
        return Viewer, stub, toasts

    def test_success_toast(self):
        from app.i18n import t

        Viewer, stub, toasts = self._viewer_stub(Image.new("RGB", (64, 36), "red"))
        Viewer._copy_frame_to_clipboard(stub)
        self.assertEqual(toasts, [t("viewer.frame_copied")])
        self.assertEqual(QApplication.clipboard().image().width(), 64)

    def test_failure_toast_when_no_frame(self):
        from app.i18n import t

        Viewer, stub, toasts = self._viewer_stub(None)
        Viewer._copy_frame_to_clipboard(stub)
        self.assertEqual(toasts, [t("viewer.frame_copy_failed")])


class MenuWiringTests(unittest.TestCase):
    def test_video_menu_has_copy_frame_entry(self):
        src = (ROOT / "app" / "viewer.py").read_text(encoding="utf-8")
        body = src[src.index("def _build_media_menu"):]
        body = body[:body.index("\n    def ", 10)]
        video_part = body[body.index("if item.is_video:"):body.index("if not item.is_video:")]
        self.assertTrue("viewer.copy_frame" in video_part,
                        "视频右键菜单里没有\"截取当前画面到剪贴板\"")
        self.assertTrue("_copy_frame_to_clipboard" in video_part)


if __name__ == "__main__":
    unittest.main()
