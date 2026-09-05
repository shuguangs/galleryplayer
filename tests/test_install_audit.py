"""安装相关代码的第二轮审计（从头逐条复查）留下的回归。

覆盖本轮修掉的问题：
1. 安装运行环境.bat 有两个 :pyskip 标签 + 残留 for 块——cmd 的 goto 取
   第一个同名标签，而 :pychk 从不清 PY_OK，于是"python 本身就是 3.10-3.12"
   （最常见的正常机器）会回跳成死循环，一键安装脚本卡死。
2. 代理解析只认 "host:port" 与 http=/https=，只挂 SOCKS 的用户拿不到回退；
   PAC（AutoConfigURL）用户毫无提示。端口一律从注册表读，代码里不写死。
3. ms_download 按"目录里有 .pt"判完成：下载在权重之后、分词器之前断掉，
   下次永久跳过，模型加载永远 NOT_FOUND，重装也修不好。
4. `ollama list` 子串判定：装了 qwen3:8b-instruct 却要 qwen3:8b 会误判
   成已装，跳过拉取。
5. Modelfile 的 FROM 路径没加引号：引擎装在带空格目录下导入必失败。
6. 用户手动指定的引擎目录只被当作"找现成引擎"的线索，指到空目录时被静静
   忽略、装到别处去（C 盘不够想装 D 盘的诉求落空）。
"""
import importlib.util
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "live-subtitle" / "install_engine.py"
BAT = ROOT / "安装运行环境.bat"


def load_installer():
    spec = importlib.util.spec_from_file_location("install_engine_audit", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_build():
    spec = importlib.util.spec_from_file_location("build_audit", ROOT / "build.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class BatStructureTests(unittest.TestCase):
    """bat 的控制流结构：标签唯一、goto 有目标、检查块不回跳。"""

    def setUp(self):
        self.src = BAT.read_bytes().decode("gbk")

    def test_no_duplicate_labels(self):
        labels = re.findall(r"(?m)^:(\w+)", self.src)
        dupes = sorted({x for x in labels if labels.count(x) > 1})
        self.assertEqual([], dupes, f"重复标签会让 goto 回跳到第一个：{dupes}")

    def test_every_goto_has_target(self):
        labels = set(re.findall(r"(?m)^:(\w+)", self.src)) | {"eof"}
        gotos = set(re.findall(r"goto :(\w+)", self.src))
        self.assertEqual(set(), gotos - labels)

    def test_python_check_is_linear_no_goto_back(self):
        """检查块里不许再有 goto：线性 call + 子程序早退，杜绝死循环。"""
        block = self.src[self.src.index('set "PY_OK="'):
                         self.src.index("if defined PY_OK (")]
        self.assertNotIn("goto", block, "检查块含 goto，可能回跳成死循环")
        for cand in ("call :pychk python", "call :pychk py -3.12",
                     "call :pychk py -3.11", "call :pychk py -3.10"):
            self.assertIn(cand, block)

    def test_pychk_early_returns_when_already_found(self):
        sub = self.src[self.src.index("\r\n:pychk\r\n"):]
        self.assertIn("if defined PY_OK goto :eof", sub,
                      "子程序不早退 → 命中后仍会被反复调用")

    def test_only_310_to_312_accepted(self):
        for ok in ("310", "311", "312"):
            self.assertIn(f'if "%_PYV%"=="{ok}" set "PY_OK=1"', self.src)
        for bad in ("313", "39", "314"):
            self.assertNotIn(f'if "%_PYV%"=="{bad}"', self.src)

    def test_no_stale_verchk_variable(self):
        self.assertNotIn("VERCHK", self.src, "残留的旧内联判定变量")


class ProxyParsingTests(unittest.TestCase):
    """代理解析：端口全部来自注册表，覆盖常见代理软件的写法。"""

    def setUp(self):
        self.ie = load_installer()

    def test_no_hardcoded_port_in_source(self):
        """实现里不许出现固定代理端口（文档字符串/注释里举例不算）。"""
        import ast

        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docs.add(doc)
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value not in docs]
        for port in ("7890", "12589", "10809", "1080", "7897", "8888"):
            for text in literals:
                self.assertNotIn(port, text,
                                 f"代码里写死了代理端口 {port}: {text!r}")

    def test_single_value(self):
        self.assertEqual("http://10.1.2.3:3128",
                         self.ie._normalize_proxy(1, "10.1.2.3:3128"))

    def test_per_protocol_prefers_https(self):
        self.assertEqual(
            "http://10.0.0.1:3129",
            self.ie._normalize_proxy(1, "http=10.0.0.1:3128;https=10.0.0.1:3129"))

    def test_http_only_entry(self):
        self.assertEqual("http://a.b:1",
                         self.ie._normalize_proxy(1, "http=a.b:1;<local>"))

    def test_socks_only_gets_socks_scheme(self):
        """只挂 SOCKS 的（v2rayN/Clash 常见）也要能用上。"""
        self.assertEqual("socks5h://127.0.0.1:20808",
                         self.ie._normalize_proxy(1, "socks=127.0.0.1:20808"))
        self.assertEqual("socks5h://1.2.3.4:2080",
                         self.ie._normalize_proxy(1, "socks5=1.2.3.4:2080;ftp=x:1"))

    def test_scheme_kept_as_is(self):
        self.assertEqual("http://p.corp:80",
                         self.ie._normalize_proxy(1, "http://p.corp:80"))

    def test_whitespace_and_disabled_and_empty(self):
        self.assertEqual("http://127.0.0.1:9",
                         self.ie._normalize_proxy(1, "  127.0.0.1:9 "))
        self.assertEqual("", self.ie._normalize_proxy(0, "127.0.0.1:9"))
        self.assertEqual("", self.ie._normalize_proxy(1, ""))
        self.assertEqual("", self.ie._normalize_proxy(1, None))

    def test_unusable_protocols_ignored(self):
        self.assertEqual("", self.ie._normalize_proxy(1, "ftp=x:21"))

    def test_pac_only_setup_reports_hint(self):
        """PAC 用户：解析不了但要给出出路（设 HTTPS_PROXY）。"""
        logs = []
        self.ie.log = lambda m: logs.append(m)

        class FakeReg:
            HKEY_CURRENT_USER = 0

            @staticmethod
            def OpenKey(*_a, **_k):
                class K:
                    def Close(self):
                        pass
                return K()

            @staticmethod
            def QueryValueEx(_key, name):
                if name == "AutoConfigURL":
                    return ("http://wpad.corp/proxy.pac", 1)
                if name == "ProxyEnable":
                    return (0, 4)
                raise OSError("missing")

        with mock.patch.dict(sys.modules, {"winreg": FakeReg}), \
                mock.patch.object(self.ie.sys, "platform", "win32"):
            self.assertEqual("", self.ie.system_proxy())
        self.assertTrue(any("PAC" in m and "HTTPS_PROXY" in m for m in logs),
                        f"PAC 场景没给提示: {logs}")

    def test_run_proxy_keeps_original_code_without_proxy(self):
        self.ie.run = lambda cmd, env=None, **kw: 42
        self.ie.system_proxy = lambda: ""
        self.assertEqual(42, self.ie.run_proxy(["x"]))

    def test_run_proxy_skips_retry_for_missing_exe(self):
        """命令根本不存在时换代理毫无意义，别白跑一轮。"""
        calls = []
        self.ie.run = lambda cmd, env=None, **kw: (calls.append(1),
                                                   self.ie.MISSING_EXE)[1]
        self.ie.system_proxy = lambda: "http://p:1"
        self.assertEqual(self.ie.MISSING_EXE, self.ie.run_proxy(["nope"]))
        self.assertEqual(1, len(calls))


class MsDownloadSentinelTests(unittest.TestCase):
    def setUp(self):
        self.ie = load_installer()
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="msdl-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))

    def test_partial_download_is_not_treated_as_complete(self):
        """只有权重、没有哨兵 → 必须重新下载（旧代码永久跳过）。"""
        target = self.tmp / "model"
        target.mkdir()
        (target / "model.pt").write_bytes(b"x")      # 半截下载的残留
        calls = []
        self.ie.run_proxy = lambda cmd, **kw: (calls.append(cmd), 0)[1]
        ok = self.ie.ms_download(Path("py.exe"), "repo/x", target, "标签")
        self.assertTrue(ok)
        self.assertEqual(1, len(calls), "有哨兵才该跳过，半截残留必须重下")
        self.assertTrue((target / self.ie.MS_DONE).is_file(), "没写完成哨兵")

    def test_sentinel_short_circuits(self):
        target = self.tmp / "model2"
        target.mkdir()
        (target / self.ie.MS_DONE).write_text("ok", encoding="utf-8")
        calls = []
        self.ie.run_proxy = lambda cmd, **kw: (calls.append(cmd), 0)[1]
        self.assertTrue(self.ie.ms_download(Path("py.exe"), "r", target, "l"))
        self.assertEqual([], calls)

    def test_download_without_weights_fails(self):
        target = self.tmp / "model3"
        self.ie.run_proxy = lambda cmd, **kw: 0      # 假成功但没落文件
        self.assertFalse(self.ie.ms_download(Path("py.exe"), "r", target, "l"))


class OllamaModelMatchTests(unittest.TestCase):
    def setUp(self):
        self.ie = load_installer()

    def _fake_list(self, text):
        import subprocess as sp

        class Done:
            stdout = text
        return mock.patch.object(sp, "run", lambda *a, **k: Done())

    def test_exact_match(self):
        listing = ("NAME            ID    SIZE\n"
                   "qwen3:8b        aaa   5.2 GB\n")
        with self._fake_list(listing):
            self.assertTrue(self.ie.ollama_has_model(Path("o.exe"), "qwen3:8b"))

    def test_substring_is_not_a_match(self):
        """装的是 qwen3:8b-instruct，要的是 qwen3:8b → 必须判未装。"""
        listing = ("NAME                  ID    SIZE\n"
                   "qwen3:8b-instruct     aaa   5.2 GB\n")
        with self._fake_list(listing):
            self.assertFalse(self.ie.ollama_has_model(Path("o.exe"), "qwen3:8b"))

    def test_latest_tag_counts(self):
        listing = "NAME            ID    SIZE\nfoo:latest      aaa   1 GB\n"
        with self._fake_list(listing):
            self.assertTrue(self.ie.ollama_has_model(Path("o.exe"), "foo"))

    def test_missing_model(self):
        listing = "NAME            ID    SIZE\nother:8b        aaa   1 GB\n"
        with self._fake_list(listing):
            self.assertFalse(self.ie.ollama_has_model(Path("o.exe"), "qwen3:8b"))


class SourceHygieneTests(unittest.TestCase):
    def test_modelfile_path_is_quoted(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('FROM "{g}"', src, "带空格的引擎目录会导入失败")

    def test_llamacpp_tmp_cleanup_is_forgiving(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("tmp.rmdir()", src, "非空临时目录会抛 OSError")
        self.assertIn("shutil.rmtree(tmp, ignore_errors=True)", src)

    def test_curl_uses_resolved_path(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('["curl.exe", "-L"', src, "应使用 which 解析出的路径")

    def test_engine_scripts_single_source_of_truth(self):
        from app.engine_files import ENGINE_SCRIPTS

        self.assertEqual(tuple(ENGINE_SCRIPTS), tuple(load_build().ENGINE_FILES))
        for name in ENGINE_SCRIPTS:
            self.assertTrue((ROOT / "live-subtitle" / name).is_file(), name)

    def test_settings_dialog_installs_into_user_chosen_dir(self):
        src = (ROOT / "app" / "settings_dialog.py").read_text(encoding="utf-8")
        self.assertIn("_resolve_install_dir", src)
        self.assertEqual(2, src.count("pipe = self._resolve_install_dir()"),
                         "两个安装按钮都要走同一套目录解析")
        self.assertIn("ENGINE_SCRIPTS", src, "自定义目录要补齐引擎脚本")


if __name__ == "__main__":
    unittest.main()
