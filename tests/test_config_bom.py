"""设置文件带 BOM 时不得静默重置成默认值。

背景（实测）：用 PowerShell 改过 config.json（Set-Content -Encoding utf8 会写 BOM），
旧 _JsonStore 按严格 utf-8 解析 → JSONDecodeError → 整份设置回到默认，
用户关掉的"网络盘视频缩略图"开关被悄悄打开，一分钟内抓了 265 张网盘缩略图。
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.config import _JsonStore


class BomTests(unittest.TestCase):
    def test_bom_file_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"remote_video_thumbs": False}).encode())
            store = _JsonStore(p, {"remote_video_thumbs": True})
            self.assertFalse(store._data["remote_video_thumbs"],
                             "带 BOM 的设置文件被当成损坏并重置为默认值")

    def test_plain_utf8_still_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({"x": 1}), encoding="utf-8")
            self.assertEqual(_JsonStore(p, {"x": 0})._data["x"], 1)


if __name__ == "__main__":
    unittest.main()
