"""中文路径的 ASCII junction 自愈（SenseVoice 加载的前置条件）。

背景 bug（沙盒复现）：sentencepiece 打不开非 ASCII 路径，所以中文安装目录
下 SenseVoice 靠 %LOCALAPPDATA%\\MediaPlayerASR\\<模型名> 这个 NTFS junction
绕路。但**悬空 junction**（用户换了盘符 / 目标被删）上 Path.exists() 与
os.path.islink() 都返回 False——旧代码据此跳过删除，紧接着 mklink 报"已
存在"，函数回退中文路径，SenseVoice 从此永久加载失败：

    RuntimeError: NOT_FOUND: "J:\\播放器\\...\\chn_jpn_yue_eng_ko_spectok.bpe.model"

修法：os.lstat 才认得悬空 junction（_remove_link）；链接名带源路径哈希，
多份安装不再抢同一个名字；每次都校验 realpath 指向，悬空/指错就重建。
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "live-subtitle"))

import asr_engines as ae  # noqa: E402  顶层只依赖标准库


def _link_for(src: Path) -> Path:
    tag = hashlib.sha1(str(src).encode("utf-8")).hexdigest()[:8]
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MediaPlayerASR"
    return root / f"{src.name}-{tag}"


@unittest.skipUnless(sys.platform == "win32", "junction 是 NTFS 特性")
class AsciiJunctionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="junction-"))
        # 中文源目录（复刻 J:\播放器\... 的处境）
        self.src = self.tmp / "播放器测试" / "iic--SenseVoiceSmall"
        self.src.mkdir(parents=True)
        (self.src / "model.pt").write_bytes(b"x")
        self.link = _link_for(self.src)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        ae._remove_link(self.link)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_dangling(self):
        ae._remove_link(self.link)
        self.link.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.link),
                        str(self.tmp / "已经没有的目标")],
                       capture_output=True, timeout=15)

    def test_ascii_path_returned_unchanged(self):
        ascii_dir = self.tmp / "plain" / "model"
        ascii_dir.mkdir(parents=True)
        self.assertEqual(ascii_dir, ae._ascii_junction(ascii_dir))

    def test_creates_junction_for_chinese_path(self):
        got = ae._ascii_junction(self.src)
        self.assertNotEqual(self.src, got)
        self.assertTrue(got.is_dir())
        self.assertEqual(os.path.realpath(self.src), os.path.realpath(got))
        str(got).encode("ascii")           # 必须是纯 ASCII，否则白忙

    def test_dangling_junction_is_rebuilt(self):
        """核心回归：悬空 junction 必须被识别并重建，不能回退中文路径。"""
        self._make_dangling()
        self.assertFalse(self.link.exists(), "悬空 junction 上 exists() 本就是 False")
        self.assertFalse(os.path.islink(self.link), "islink() 也认不出来")
        got = ae._ascii_junction(self.src)
        self.assertNotEqual(self.src, got, "回退中文路径 = SenseVoice 加载必败")
        self.assertEqual(os.path.realpath(self.src), os.path.realpath(got))

    def test_junction_pointing_elsewhere_is_repointed(self):
        other = self.tmp / "别的模型"
        other.mkdir()
        ae._remove_link(self.link)
        self.link.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.link), str(other)],
                       capture_output=True, timeout=15)
        got = ae._ascii_junction(self.src)
        self.assertEqual(os.path.realpath(self.src), os.path.realpath(got))

    def test_idempotent(self):
        first = ae._ascii_junction(self.src)
        self.assertEqual(first, ae._ascii_junction(self.src))

    def test_link_name_is_per_source_path(self):
        """两份安装的同名模型目录不能抢同一个链接。"""
        other = self.tmp / "第二份安装" / "iic--SenseVoiceSmall"
        other.mkdir(parents=True)
        (other / "model.pt").write_bytes(b"y")
        a = ae._ascii_junction(self.src)
        b = ae._ascii_junction(other)
        self.addCleanup(ae._remove_link, _link_for(other))
        self.assertNotEqual(a, b)
        self.assertEqual(os.path.realpath(self.src), os.path.realpath(a))
        self.assertEqual(os.path.realpath(other), os.path.realpath(b))

    def test_missing_source_returns_input(self):
        ghost = self.tmp / "不存在的中文目录"
        self.assertEqual(ghost, ae._ascii_junction(ghost))

    def test_junction_ok_rejects_dangling(self):
        self._make_dangling()
        self.assertFalse(ae._junction_ok(self.link, self.src))

    def test_remove_link_handles_absent_path(self):
        ae._remove_link(self.tmp / "从来没有过的链接")   # 不许抛


if __name__ == "__main__":
    unittest.main()
