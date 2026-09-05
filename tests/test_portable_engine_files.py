"""便携包必须带齐字幕引擎的运行时脚本闭包。

背景 bug（用户实测"下载模型失败"的真凶之一）：便携包的 live-subtitle/
只带了 install_engine.py + ollama_modelfile.py。但播放器启动的是
live_transcribe.py / live_capture.py，它们又 import asr_engines /
translate_service / ollama_service——少这几个文件：
- 实时字幕、SRT 生成根本起不来（脚本不存在）；
- 更早一步，install_engine 最后的"验证模型可加载"就是
  `import asr_engines`，默认引擎（qwen/sensevoice）会在下完 4.7GB 模型
  之后以"✗ 模型加载验证失败"收场——而在 UTF-8 修复前，这条 ✗ 还会被
  GBK 编码崩溃盖掉，用户只看到 illegal multibyte sequence。

本测试用 AST 算出入口脚本的本地 import 闭包，与 build.ENGINE_FILES 对比：
以后谁给引擎加了新的本地模块，这里立刻会红。
"""
import ast
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENGINE_DIR = ROOT / "live-subtitle"
# 播放器真正 spawn 的入口（app/live_engine.py、app/viewer.py）
ENTRIES = ("live_transcribe.py", "live_capture.py", "install_engine.py")


def _load_build():
    spec = importlib.util.spec_from_file_location("build_mod", ROOT / "build.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _local_import_closure(entries) -> set[str]:
    """入口脚本经本地 import 能到达的全部 live-subtitle/*.py。"""
    local = {p.stem for p in ENGINE_DIR.glob("*.py")}
    seen: set[str] = set()
    queue = list(entries)
    while queue:
        name = queue.pop()
        if name in seen or not (ENGINE_DIR / name).is_file():
            continue
        seen.add(name)
        tree = ast.parse((ENGINE_DIR / name).read_text(
            encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module \
                    and node.level == 0:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                if mod in local:
                    queue.append(f"{mod}.py")
    return seen


class EngineRuntimeFilesTests(unittest.TestCase):
    def setUp(self):
        self.build = _load_build()

    def test_entries_exist(self):
        for name in ENTRIES:
            self.assertTrue((ENGINE_DIR / name).is_file(), f"缺 {name}")

    def test_packaged_files_cover_runtime_closure(self):
        """核心回归：闭包内的脚本必须全在 build.ENGINE_FILES 里。"""
        closure = _local_import_closure(ENTRIES)
        packaged = set(self.build.ENGINE_FILES)
        missing = sorted(closure - packaged)
        self.assertEqual(
            [], missing,
            f"便携包会缺这些引擎脚本 → 装完模型也拉不起字幕: {missing}")

    def test_packaged_files_all_exist_in_repo(self):
        for name in self.build.ENGINE_FILES:
            self.assertTrue((ENGINE_DIR / name).is_file(),
                            f"ENGINE_FILES 列了不存在的 {name}")

    def test_asr_engines_packaged_for_install_verification(self):
        """install_engine 的模型验证步 import asr_engines：必须打进包。"""
        src = (ENGINE_DIR / "install_engine.py").read_text(encoding="utf-8")
        self.assertIn("import asr_engines", src)
        self.assertIn("asr_engines.py", self.build.ENGINE_FILES)

    def test_portable_layout_resolves_all_local_imports(self):
        """按便携布局摆好文件后，闭包里的每个本地 import 都能就地解析。"""
        packaged = set(self.build.ENGINE_FILES)
        for name in sorted(_local_import_closure(ENTRIES)):
            tree = ast.parse((ENGINE_DIR / name).read_text(
                encoding="utf-8", errors="replace"))
            local = {p.stem for p in ENGINE_DIR.glob("*.py")}
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module \
                        and node.level == 0:
                    mods = [node.module.split(".")[0]]
                for mod in mods:
                    if mod in local:
                        self.assertIn(
                            f"{mod}.py", packaged,
                            f"{name} import {mod}，但便携包没带 {mod}.py")


if __name__ == "__main__":
    unittest.main()
