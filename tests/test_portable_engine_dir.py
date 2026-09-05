"""便携版引擎目录定位的回归测试。

背景 bug（用户在 GitHub release 便携版实测）：设置页点"下载模型"报
「未找到 install_engine.py（请先把 live-subtitle 放到项目旁或手动指定
引擎目录）」——但 live-subtitle 明明就在 exe 旁边，手动指定引擎目录后
依然报同样的错。

两个根因：
1. 打包版（onedir）里模块的 __file__ 在 <exe>/_internal/app/ 下，安装
   按钮的回退路径 `Path(__file__).parent.parent / "live-subtitle"` 解析
   成 <exe>/_internal/live-subtitle——而 build.py 把 live-subtitle 放在
   exe 根目录，永远找不到。
2. 「手动指定引擎目录」走 find_subtitle_pipeline_dir，它要求目录里
   已有 .venv/Scripts/python.exe——而安装恰恰发生在 venv 存在之前，
   手动指定的目录永远被拒，退回 1 的坏路径。

修法：config.find_subtitle_source_dir() 定位"源目录"（含
install_engine.py，不要求 venv；顺序：用户设置 → 打包版 exe 旁 →
源码树相对 → 环境变量），两个安装按钮与磁盘空间提示改用之；
find_subtitle_pipeline_dir 补上 exe 旁候选（装完后运行时能发现引擎）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import config as _config


class PortableEngineDirTests(unittest.TestCase):
    def setUp(self):
        # 沙盒：1:1 复刻便携版 onedir 布局（exe 根目录 + _internal + 旁边的
        # live-subtitle）。config.__file__ 指进 _internal，等价于打包 reality。
        self.sb = Path(tempfile.mkdtemp(prefix="portable-sb-"))
        (self.sb / "_internal" / "app").mkdir(parents=True)
        (self.sb / "_internal" / "app" / "config.py").write_text("", encoding="utf-8")
        (self.sb / "媒体播放器.exe").write_bytes(b"")
        self.sub = self.sb / "live-subtitle"
        self.sub.mkdir()
        # 沙盒里放真的 install_engine.py（纯 stdlib，--help 零依赖可跑）
        real = ROOT / "live-subtitle" / "install_engine.py"
        (self.sub / "install_engine.py").write_text(
            real.read_text(encoding="utf-8") if real.is_file()
            else "print('ok')\n", encoding="utf-8")
        self._old = (getattr(sys, "frozen", False), sys.executable,
                     _config.__file__, _config.settings["subtitle_pipeline_dir"])
        sys.frozen = True
        sys.executable = str(self.sb / "媒体播放器.exe")
        _config.__file__ = str(self.sb / "_internal" / "app" / "config.py")
        _config.settings["subtitle_pipeline_dir"] = ""

    def tearDown(self):
        frozen, exe, file_, custom = self._old
        sys.frozen = frozen
        sys.executable = exe
        _config.__file__ = file_
        _config.settings["subtitle_pipeline_dir"] = custom
        import shutil

        shutil.rmtree(self.sb, ignore_errors=True)

    def test_source_dir_found_next_to_exe(self):
        """核心回归：打包版从 exe 旁找到 live-subtitle（旧代码找 _internal）。"""
        self.assertEqual(self.sub, _config.find_subtitle_source_dir())

    def test_old_internal_fallback_misses(self):
        """实锤旧回退路径在便携布局下扑空（__file__ → _internal）。"""
        old = Path(_config.__file__).resolve().parent.parent / "live-subtitle"
        self.assertFalse((old / "install_engine.py").is_file())

    def test_custom_dir_works_without_venv(self):
        """手动指定引擎目录：不需要 venv 也必须能定位到安装脚本。"""
        other = self.sb / "elsewhere"
        other.mkdir()
        (other / "install_engine.py").write_text("", encoding="utf-8")
        _config.settings["subtitle_pipeline_dir"] = str(other)
        self.assertEqual(other, _config.find_subtitle_source_dir())

    def test_runtime_pipeline_dir_requires_venv_then_found(self):
        """运行时探测：没 venv 时 None（安装前）；装出 venv 后能发现
        exe 旁的引擎（便携版生命周期：下载模型 → 引擎就绪）。"""
        self.assertIsNone(_config.find_subtitle_pipeline_dir())
        venv = self.sub / ".venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        self.assertEqual(self.sub, _config.find_subtitle_pipeline_dir())

    def test_unfrozen_source_tree_still_works(self):
        """源码运行不受影响：frozen 关掉后走 __file__ 相对路径。"""
        sys.frozen = False
        _config.__file__ = str(ROOT / "app" / "config.py")
        self.assertEqual(ROOT / "live-subtitle", _config.find_subtitle_source_dir())

    def test_install_engine_script_is_runnable(self):
        """沙盒里真实跑一遍定位到的脚本（--help，零下载）：找得到的
        脚本必须真能被解释器执行。"""
        import subprocess

        pipe = _config.find_subtitle_source_dir()
        real_python = self._old[1]                 # setUp 捕获的真解释器
        done = subprocess.run(
            [real_python, str(pipe / "install_engine.py"), "--help"],
            capture_output=True, timeout=30, text=True,
        )
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("--dir", done.stdout)


if __name__ == "__main__":
    unittest.main()
