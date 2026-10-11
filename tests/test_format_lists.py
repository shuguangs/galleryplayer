"""各处格式列表必须与 media 的真实格式表一致（曾漏写 .asf：能播却在对话框/文件关联里缺失）。"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import assoc, media  # noqa: E402


class FormatListTests(unittest.TestCase):
    def test_assoc_covers_every_media_ext(self):
        self.assertEqual(set(assoc.EXTENSIONS), media.MEDIA_EXTS)

    def test_dialog_patterns_cover_every_ext(self):
        pats = media.dialog_patterns(media.MEDIA_EXTS).split()
        self.assertEqual({p[1:] for p in pats}, media.MEDIA_EXTS)
        self.assertIn("*.asf", pats)

    def test_tools_dialog_uses_generated_filter(self):
        src = (ROOT / "app" / "tools_dialog.py").read_text(encoding="utf8")
        self.assertIn("media.dialog_patterns(media.VIDEO_EXTS)", src)


if __name__ == "__main__":
    unittest.main()
