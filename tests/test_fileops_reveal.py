"""fileops.reveal 的分支测试：Shell API 优先，失败回退 explorer /select,。"""
import ctypes as ctypes_mod
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import fileops  # noqa: E402


class RevealTests(unittest.TestCase):
    def test_api_success_skips_commandline(self):
        """SHOpenFolderAndSelectItems 成功 → 不再走命令行。"""
        fake_shell = mock.MagicMock()

        def fake_parse(name, _reserved, byref_pidl, _flags, _reserved2):
            byref_pidl._obj.value = 0x1234
            return 0

        fake_shell.SHParseDisplayName.side_effect = fake_parse
        fake_shell.SHOpenFolderAndSelectItems.return_value = 0
        with mock.patch.object(ctypes_mod, "windll",
                               mock.MagicMock(shell32=fake_shell)), \
                mock.patch.object(fileops.subprocess, "Popen") as popen:
            fileops.reveal(Path("Z:/x/file.mp4"))
        fake_shell.SHOpenFolderAndSelectItems.assert_called_once()
        arr = fake_shell.SHOpenFolderAndSelectItems.call_args[0][0]
        self.assertEqual(arr[0], 0x1234)  # 传入的是 PIDL 数组
        popen.assert_not_called()

    def test_api_failure_falls_back(self):
        """API 返回非 0 → 回退 explorer /select 命令行。"""
        fake_shell = mock.MagicMock()

        def fake_parse(name, _reserved, byref_pidl, _flags, _reserved2):
            byref_pidl._obj.value = 0x1234
            return 0

        fake_shell.SHParseDisplayName.side_effect = fake_parse
        fake_shell.SHOpenFolderAndSelectItems.return_value = 1
        with mock.patch.object(ctypes_mod, "windll",
                               mock.MagicMock(shell32=fake_shell)), \
                mock.patch.object(fileops.subprocess, "Popen") as popen:
            fileops.reveal(Path("Z:/x/file.mp4"))
        popen.assert_called_once()
        self.assertIn("/select,", popen.call_args[0][0])

    def test_parse_failure_falls_back(self):
        """路径解析失败（SHParseDisplayName 非 0）→ 回退命令行。"""
        fake_shell = mock.MagicMock()
        fake_shell.SHParseDisplayName.return_value = 0x80004005  # E_FAIL
        with mock.patch.object(ctypes_mod, "windll",
                               mock.MagicMock(shell32=fake_shell)), \
                mock.patch.object(fileops.subprocess, "Popen") as popen:
            fileops.reveal(Path("Z:/x/file.mp4"))
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
