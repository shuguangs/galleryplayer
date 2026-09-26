"""退出时选"关闭并退出（释放显存）"要连 Ollama 一起关。

背景（用户需求）：翻译走独立的 Ollama 服务进程（ollama serve + 模型 runner），
旧 kill() 只收引擎进程及其子进程——Ollama 若由引擎拉起，它是分离进程不在
子进程树里；若本来就在跑，更不归引擎管。结果点了"释放显存"，翻译模型那
~5GB 显存照旧被占着。

实现约束：
- 不能用 `ollama stop` / `ollama ps` 这类 CLI：服务没起时 CLI 会一直挂着
  不返回（实测超过 60s），退出流程会被卡死。改为按进程名直接收。
- 只收 Ollama 安装目录下的进程：同名进程若来自别处，不碰。
- "保留模型并退出"不得关 Ollama。
"""
import unittest
from pathlib import Path
from unittest import mock

from app import live_engine


class _Proc:
    def __init__(self, pid, name, exe):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "exe": exe}
        self.killed = False

    def children(self, recursive=False):
        return []

    def kill(self):
        self.killed = True

    def terminate(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


OLLAMA_DIR = r"C:\Users\u\AppData\Local\Programs\Ollama"


class StopOllamaTests(unittest.TestCase):
    def _run(self, procs):
        import psutil

        with mock.patch.object(psutil, "process_iter", return_value=procs), \
                mock.patch.object(live_engine, "_ollama_install_dirs",
                                  return_value=[Path(OLLAMA_DIR)]), \
                mock.patch.object(psutil, "wait_procs", return_value=([], [])):
            return live_engine.stop_ollama()

    def test_kills_server_runner_and_tray(self):
        """实机进程布局（Ollama 0.34）：模型跑在 lib\\ollama\\llama-server.exe 里，
        是 ollama.exe 的子进程。只杀 ollama.exe 会留下孤儿 llama-server，
        显存 5.4GB 一点不降（实测）。"""
        serve = _Proc(10, "ollama.exe", OLLAMA_DIR + r"\ollama.exe")
        runner = _Proc(11, "llama-server.exe",
                       OLLAMA_DIR + r"\lib\ollama\llama-server.exe")
        tray = _Proc(12, "ollama app.exe", OLLAMA_DIR + r"\ollama app.exe")
        n = self._run([serve, runner, tray])
        self.assertEqual(n, 3)
        self.assertTrue(runner.killed, "模型 runner（llama-server）没被收，显存不会释放")
        self.assertTrue(serve.killed and tray.killed)

    def test_orphan_runner_found_via_children(self):
        """runner 若不在枚举结果里（比如权限原因拿不到 exe），也要经父进程的
        子进程树收掉。"""
        runner = _Proc(11, "llama-server.exe", "")
        serve = _Proc(10, "ollama.exe", OLLAMA_DIR + r"\ollama.exe")
        serve.children = lambda recursive=False: [runner]
        self._run([serve])
        self.assertTrue(runner.killed, "没顺着子进程树收 runner")

    def test_foreign_llama_server_untouched(self):
        """用户自己跑的 llama-server（不在 Ollama 目录下）不碰——SRT 的
        HY-MT2 翻译也用 llama-server，归引擎自己管。"""
        other = _Proc(40, "llama-server.exe", r"J:\播放器\live-subtitle\llama\llama-server.exe")
        self._run([other])
        self.assertFalse(other.killed)

    def test_leaves_foreign_same_name_process(self):
        foreign = _Proc(20, "ollama.exe", r"D:\other\ollama.exe")
        self.assertEqual(self._run([foreign]), 0)
        self.assertFalse(foreign.killed, "误杀了安装目录外的同名进程")

    def test_leaves_unrelated_processes(self):
        py = _Proc(30, "python.exe", r"C:\Python\python.exe")
        self._run([py])
        self.assertFalse(py.killed)

    def test_no_cli_call(self):
        """绝不调 ollama CLI（服务未起时会无限挂起，卡死退出流程）。"""
        with mock.patch("subprocess.run") as run, \
                mock.patch("subprocess.Popen") as popen:
            self._run([])
        for call in run.call_args_list + popen.call_args_list:
            self.assertNotIn("ollama", " ".join(map(str, call.args[0])).lower())


class QuitFlowTests(unittest.TestCase):
    def test_close_button_stops_ollama_keep_button_does_not(self):
        src = (Path(__file__).resolve().parent.parent / "app" /
               "main_window.py").read_text(encoding="utf-8")
        body = src[src.index("def closeEvent"):]
        stop_branch = body[body.index("if clicked is stop_btn:"):]
        stop_branch = stop_branch[:stop_branch.index("\n\n")]
        self.assertTrue("stop_ollama" in stop_branch,
                        "关闭并退出分支没有关 Ollama")
        keep_region = body[:body.index("if clicked is stop_btn:")]
        self.assertFalse("stop_ollama" in keep_region,
                         "保留模型分支也会关 Ollama")


if __name__ == "__main__":
    unittest.main()
