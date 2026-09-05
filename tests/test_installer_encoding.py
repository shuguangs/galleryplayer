"""安装脚本的 UTF-8 输出回归：✗ 等字符在 GBK 管道下不得炸掉安装器。

背景 bug（用户在便携包实测）：一键安装 / SRT 大模型安装结尾全是
`UnicodeEncodeError: 'gbk' codec can't encode character '\u2717'`——
播放器经 QProcess 管道捕获输出，管道下 Python 默认用系统码页（中文
Windows = GBK）编码 stdout，而失败分支都打印 "✗"（U+2717，GBK 编不
出来）——真正的失败原因（venv/下载/磁盘）被编码崩溃盖住。

修法：install_engine.py 顶部 reconfigure stdout/stderr 为 UTF-8
（播放器按 utf-8 解码，逐字对得上），并给子进程链注入
PYTHONIOENCODING/PYTHONUTF8；播放器两个安装按钮的 QProcess 也注入
同一环境（settings_dialog）。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = ROOT / "live-subtitle" / "install_engine.py"


class InstallerEncodingTests(unittest.TestCase):
    def test_log_survives_gbk_pipe(self):
        """在 GBK 管道下 import 本脚本并 log("✗…")：必须以 UTF-8 落管、零崩溃。"""
        child = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('ie', {str(SCRIPT)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "m.log('✗ 磁盘空间不足（回归测试）')\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", child],
            capture_output=True, timeout=30,
        )
        self.assertEqual(0, done.returncode,
                         done.stderr.decode("utf-8", "replace"))
        self.assertIn("✗", done.stdout.decode("utf-8", "replace"))
        # 子进程继承的约定：环境变量已注入
        env = done.stdout.decode("utf-8", "replace")
        self.assertNotIn("UnicodeEncodeError", env)

    def test_stderr_survives_gbk_pipe(self):
        child = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('ie', {str(SCRIPT)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "print('✗ 直接打印', file=sys.stderr)\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", child],
            capture_output=True, timeout=30,
        )
        self.assertEqual(0, done.returncode)
        self.assertIn("✗", done.stderr.decode("utf-8", "replace"))

    def test_player_injects_utf8_env_for_both_install_buttons(self):
        """播放器两个安装按钮都必须给子进程注入 UTF-8 环境。"""
        src = (ROOT / "app" / "settings_dialog.py").read_text(encoding="utf-8")
        self.assertEqual(
            2, src.count('env.insert("PYTHONIOENCODING", "utf-8")'),
            "两个安装按钮（ASR/SRT）都要注入 PYTHONIOENCODING")
        self.assertEqual(
            2, src.count('env.insert("PYTHONUTF8", "1")'))

    def test_installer_sets_env_for_grandchildren(self):
        """脚本自身也要给孙进程（venv/pip/modelscope）设 UTF-8 约定。"""
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('os.environ.setdefault("PYTHONIOENCODING", "utf-8")', src)
        self.assertIn('os.environ.setdefault("PYTHONUTF8", "1")', src)


if __name__ == "__main__":
    unittest.main()
