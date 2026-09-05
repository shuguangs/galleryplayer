"""安装器对"用户机器上什么都没装"的处理：缺 exe / 缺 Ollama / 缺合规 Python。

背景（用户反馈"下载模型失败"的三条真因，均与网络无关）：
1. `run()` 用 subprocess.call 直呼 winget/curl/ollama——机器上没有这些
   exe 时抛 FileNotFoundError，整场安装以 Python traceback 收尾，用户只
   看到"安装失败"，日志里连缺什么都没写。
2. Ollama 定位只看 %LOCALAPPDATA%\\Programs\\Ollama：winget 装成机器级
   （Program Files）就判成"没装"，装完又不刷新 PATH。且 winget 静默装完
   服务不会在当前会话自启，而 `ollama pull` 必须连服务——新机器上必然
   拉取失败，还会误走 GGUF 镜像回退（默认 qwen3:8b 没有映射 → 判死）。
3. funasr（qwen/sensevoice 的依赖）钉 numpy<2，而 numpy 1.x 只出到 cp312
   轮子：在 Python 3.13 上 pip 只能现编译 numpy 且必然失败。venv 必须用
   3.10-3.12 建；已用 3.13 建过的 venv 要重建，否则用户永远卡在同一错误。
"""
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "live-subtitle" / "install_engine.py"


def load():
    spec = importlib.util.spec_from_file_location("install_engine_env", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class MissingExeTests(unittest.TestCase):
    def setUp(self):
        self.ie = load()

    def test_missing_exe_returns_code_not_traceback(self):
        rc = self.ie.run(["definitely_no_such_exe_xyz", "--help"])
        self.assertEqual(self.ie.MISSING_EXE, rc)

    def test_missing_exe_code_is_nonzero(self):
        """调用方一律按"非 0 即失败"处理，别撞上 0。"""
        self.assertNotEqual(0, self.ie.MISSING_EXE)

    def test_run_proxy_survives_missing_exe(self):
        self.ie.system_proxy = lambda: "http://p:1"
        rc = self.ie.run_proxy(["definitely_no_such_exe_xyz"])
        self.assertNotEqual(0, rc)


class OllamaLocateTests(unittest.TestCase):
    def setUp(self):
        self.ie = load()

    def test_none_when_nothing_installed(self):
        with mock.patch.dict(self.ie.os.environ,
                             {"LOCALAPPDATA": "", "ProgramFiles": "",
                              "ProgramFiles(x86)": "", "ProgramW6432": ""},
                             clear=False), \
                mock.patch.object(self.ie.shutil, "which", return_value=None):
            self.assertIsNone(self.ie.find_ollama())

    def test_finds_machine_scope_install(self):
        """winget 装成机器级（Program Files）也要认得出来。"""
        with mock.patch.dict(self.ie.os.environ,
                             {"LOCALAPPDATA": "", "ProgramFiles": r"C:\PF"},
                             clear=False), \
                mock.patch.object(self.ie.shutil, "which", return_value=None), \
                mock.patch.object(Path, "is_file",
                                  lambda self: str(self) == r"C:\PF\Ollama\ollama.exe"):
            found = self.ie.find_ollama()
        self.assertEqual(Path(r"C:\PF\Ollama\ollama.exe"), found)

    def test_falls_back_to_path_lookup(self):
        with mock.patch.dict(self.ie.os.environ,
                             {"LOCALAPPDATA": "", "ProgramFiles": ""},
                             clear=False), \
                mock.patch.object(self.ie.shutil, "which",
                                  return_value=r"D:\tools\ollama.exe"), \
                mock.patch.object(Path, "is_file", lambda self: True):
            self.assertEqual(Path(r"D:\tools\ollama.exe"), self.ie.find_ollama())


class OllamaDaemonTests(unittest.TestCase):
    def setUp(self):
        self.ie = load()

    def test_already_running_does_not_spawn(self):
        self.ie.ollama_ready = lambda exe, timeout=20.0: True
        with mock.patch.object(subprocess, "Popen") as popen:
            self.assertTrue(self.ie.ensure_ollama_daemon(Path("ollama.exe")))
        popen.assert_not_called()

    def test_spawns_serve_and_waits_until_ready(self):
        """服务没跑：起 `ollama serve` 并轮询到就绪（新机器 pull 的前提）。"""
        probes = []
        self.ie.ollama_ready = lambda exe, timeout=20.0: (
            probes.append(1), len(probes) > 2)[1]
        with mock.patch.object(subprocess, "Popen") as popen:
            ok = self.ie.ensure_ollama_daemon(Path("ollama.exe"), wait=10)
        self.assertTrue(ok)
        popen.assert_called_once()
        self.assertIn("serve", popen.call_args[0][0])

    def test_gives_up_cleanly_when_daemon_never_ready(self):
        self.ie.ollama_ready = lambda exe, timeout=20.0: False
        with mock.patch.object(subprocess, "Popen"):
            self.assertFalse(
                self.ie.ensure_ollama_daemon(Path("ollama.exe"), wait=0.1))

    def test_spawn_failure_is_not_fatal(self):
        self.ie.ollama_ready = lambda exe, timeout=20.0: False
        with mock.patch.object(subprocess, "Popen",
                               side_effect=OSError("boom")):
            self.assertFalse(
                self.ie.ensure_ollama_daemon(Path("ollama.exe"), wait=1))


class VenvPythonPickTests(unittest.TestCase):
    def setUp(self):
        self.ie = load()

    def test_whisper_accepts_any_interpreter(self):
        """whisper 档位不依赖 funasr：当前解释器（哪怕 3.13）直接用。"""
        self.ie._probe_python = lambda cmd: (3, 13)
        picked = self.ie.pick_venv_python(need_funasr=False)
        self.assertIsNotNone(picked)
        self.assertEqual([sys.executable], picked[0])

    def test_funasr_rejects_313_and_finds_312(self):
        def probe(cmd):
            if cmd == [sys.executable]:
                return (3, 13)          # 当前解释器不合规
            if cmd[:2] == ["py", "-3.12"]:
                return (3, 12)
            return None

        self.ie._probe_python = probe
        with mock.patch.object(self.ie.shutil, "which", return_value="py.exe"):
            picked = self.ie.pick_venv_python(need_funasr=True)
        self.assertEqual((["py", "-3.12"], (3, 12)), picked)

    def test_funasr_uses_current_interpreter_when_in_range(self):
        self.ie._probe_python = lambda cmd: (3, 11)
        picked = self.ie.pick_venv_python(need_funasr=True)
        self.assertEqual(([sys.executable], (3, 11)), picked)

    def test_funasr_none_when_only_313_present(self):
        """只有 3.13 的机器：返回 None，由调用方给出人话提示而不是硬装。"""
        self.ie._probe_python = lambda cmd: (3, 13) if cmd == [sys.executable] else None
        with mock.patch.object(self.ie.shutil, "which", return_value="py.exe"):
            self.assertIsNone(self.ie.pick_venv_python(need_funasr=True))

    def test_version_window_matches_numpy_wheels(self):
        self.assertEqual((3, 10), self.ie.FUNASR_PY_MIN)
        self.assertEqual((3, 12), self.ie.FUNASR_PY_MAX)

    def test_probe_python_reports_real_version(self):
        self.assertEqual(sys.version_info[:2],
                         self.ie._probe_python([sys.executable]))
        self.assertIsNone(self.ie._probe_python(["definitely_no_such_exe_xyz"]))


class GuardTextTests(unittest.TestCase):
    """失败分支必须留下可执行的出路（用户拿到日志就知道下一步做什么）。"""

    def test_actionable_messages_present(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for phrase in ("没有 winget 无法自动安装",
                       "重启电脑刷新 PATH",
                       "手动打开一次 Ollama 应用",
                       "系统缺少 curl.exe",
                       "重建虚拟环境",
                       "换成 whisper 档位"):
            self.assertIn(phrase, src, f"缺少可执行提示: {phrase}")

    def test_bat_requires_310_to_312(self):
        bat = (ROOT / "安装运行环境.bat").read_bytes().decode("gbk")
        self.assertIn(":pychk", bat)
        for ok in ("310", "311", "312"):
            self.assertIn(f'if "%_PYV%"=="{ok}" set "PY_OK=1"', bat)
        self.assertNotIn('if "%_PYV%"=="313"', bat)


if __name__ == "__main__":
    unittest.main()
