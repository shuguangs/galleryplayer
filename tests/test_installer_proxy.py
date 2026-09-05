"""安装器下载链的系统代理兜底：直连两个都下不动 → 抓系统代理重试。

背景（用户反馈的便携包安装失败场景）：GitHub/HuggingFace/PyTorch 直连
在不少用户的网络里下不动，而他们的系统里通常已经挂着代理软件——curl/
pip 不会自己读 Windows 的 IE 代理设置。install_engine.py 现在：
- `_curl`：镜像 → 直连 → （都失败）带系统代理再试镜像/直连（curl -x）；
- `run_proxy`：pip / 模型下载直连失败后自动带系统代理重试一次；
- `system_proxy`：从注册表读 IE 代理（含 "http=..;https=.." 与 <local>
  混合写法的解析）。
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "live-subtitle" / "install_engine.py"


def _load():
    spec = importlib.util.spec_from_file_location("install_engine", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class NormalizeProxyTests(unittest.TestCase):
    def setUp(self):
        self.ie = _load()

    def test_host_port_gets_scheme(self):
        self.assertEqual("http://127.0.0.1:7890",
                         self.ie._normalize_proxy(1, "127.0.0.1:7890"))

    def test_protocol_map_picks_https_then_http(self):
        self.assertEqual(
            "http://127.0.0.1:7899",
            self.ie._normalize_proxy(1, "http=127.0.0.1:7890;https=127.0.0.1:7899"))
        self.assertEqual(
            "http://127.0.0.1:7890",
            self.ie._normalize_proxy(1, "http=127.0.0.1:7890;<local>"))

    def test_disabled_or_empty(self):
        self.assertEqual("", self.ie._normalize_proxy(0, "127.0.0.1:7890"))
        self.assertEqual("", self.ie._normalize_proxy(1, ""))
        self.assertEqual("", self.ie._normalize_proxy(1, None))


class RunProxyFallbackTests(unittest.TestCase):
    def setUp(self):
        self.ie = _load()

    def tearDown(self):
        for name in ("run", "system_proxy"):
            if name in self.ie.__dict__:
                delattr(self.ie, name)

    def test_direct_success_never_touches_proxy(self):
        calls = []
        self.ie.run = lambda cmd, env=None, **kw: (calls.append(cmd), 0)[1]
        self.ie.system_proxy = lambda: "http://127.0.0.1:1"
        self.assertEqual(0, self.ie.run_proxy(["pip", "install", "x"]))
        self.assertEqual(1, len(calls))

    def test_direct_failure_retries_with_system_proxy(self):
        cmds, envs = [], []

        def fake_run(cmd, env=None, **kw):
            cmds.append(cmd)
            envs.append(env or {})
            return 0 if env and env.get("HTTPS_PROXY") else 1

        self.ie.run = fake_run
        self.ie.system_proxy = lambda: "http://127.0.0.1:7899"
        self.assertEqual(0, self.ie.run_proxy(["pip", "install", "x"]))
        self.assertEqual(2, len(cmds), "直连失败后必须带代理重试一次")
        self.assertEqual("http://127.0.0.1:7899", envs[1]["HTTPS_PROXY"])
        self.assertEqual("http://127.0.0.1:7899", envs[1]["https_proxy"])

    def test_business_env_survives_proxy_retry(self):
        """HF_ENDPOINT 等业务变量在代理重试时必须保留。"""
        seen = {}

        def fake_run(cmd, env=None, **kw):
            seen["env"] = env
            return 0 if env and env.get("HTTPS_PROXY") else 1

        self.ie.run = fake_run
        self.ie.system_proxy = lambda: "http://p:1"
        self.ie.run_proxy(["x"], env={"HF_ENDPOINT": "https://hf-mirror.com"})
        self.assertEqual("https://hf-mirror.com", seen["env"]["HF_ENDPOINT"])
        self.assertEqual("http://p:1", seen["env"]["HTTPS_PROXY"])

    def test_no_system_proxy_fails_plainly(self):
        calls = []
        self.ie.run = lambda cmd, env=None, **kw: (calls.append(1), 1)[1]
        self.ie.system_proxy = lambda: ""
        self.assertEqual(1, self.ie.run_proxy(["x"]))
        self.assertEqual(1, len(calls), "无代理时不得反复重试")


class CurlProxyAttemptsTests(unittest.TestCase):
    def setUp(self):
        self.ie = _load()
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))

    def tearDown(self):
        for name in ("run", "system_proxy"):
            if name in self.ie.__dict__:
                delattr(self.ie, name)

    def test_both_direct_fail_then_proxy_attempts(self):
        """镜像/直连都失败 → 抓系统代理再各试一轮（curl -x）。"""
        cmds = []

        def fake_run(cmd, env=None, **kw):
            cmds.append(cmd)
            return 1                       # 永远失败：逼出全部四轮尝试

        self.ie.run = fake_run
        self.ie.system_proxy = lambda: "http://p:7890"
        ok = self.ie._curl("https://example.org/big.bin",
                           self.tmp / "big.bin", min_bytes=10)
        self.assertFalse(ok)
        self.assertEqual(4, len(cmds), f"应有 4 轮尝试（镜像/直连 × 无代理/代理）")
        self.assertNotIn("-x", cmds[0])
        self.assertNotIn("-x", cmds[1])
        self.assertIn("-x", cmds[2])
        self.assertEqual("http://p:7890", cmds[2][cmds[2].index("-x") + 1])
        self.assertIn("-x", cmds[3])
        # 前两轮不带镜像域名外的前缀、后两轮同样覆盖镜像与直连
        self.assertTrue(any("gh-proxy.com" in " ".join(c) for c in cmds))
        self.assertTrue(all("example.org" in " ".join(c) for c in cmds))

    def test_success_on_first_attempt_skips_proxy(self):
        cmds = []

        def fake_run(cmd, env=None, **kw):
            cmds.append(cmd)
            (self.tmp / "big.bin.part").write_bytes(b"x" * 100)
            return 0

        self.ie.run = fake_run
        self.ie.system_proxy = lambda: "http://p:7890"
        ok = self.ie._curl("https://example.org/big.bin",
                           self.tmp / "big.bin", min_bytes=10)
        self.assertTrue(ok)
        self.assertEqual(1, len(cmds), "直连成功就绝不多试")


if __name__ == "__main__":
    unittest.main()
