"""播放器右键菜单"仅原语（不翻译）"开关。

需求（用户）：把设置里的"不翻译（仅原语）"放进播放界面右键菜单，直接切换。
- 勾上：live_ollama_model = "none"，并记住之前的翻译模型；
- 取消：恢复之前的翻译模型（没记录时用默认 qwen3:8b）；
- 实时字幕正在跑时立刻按新设置重启引擎，不在跑时只改设置。
"""
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.config import DEFAULTS, settings

_app = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent


class ToggleTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: settings[k] for k in ("live_ollama_model", "live_translate_last_model")}

    def tearDown(self):
        for k, v in self._saved.items():
            settings[k] = v

    def _stub(self, live_on):
        from app.viewer import Viewer

        calls = []
        stub = SimpleNamespace(
            _live_on=live_on,
            _live_paused=False,
            _restart_live_with_new_settings=lambda: calls.append("restart"),
            _show_toast=lambda text: calls.append(("toast", text)),
        )
        return Viewer, stub, calls

    def test_turn_on_remembers_model_and_sets_none(self):
        settings["live_ollama_model"] = "qwen3:14b"
        Viewer, stub, _ = self._stub(False)
        Viewer._set_live_translate_off(stub, True)
        self.assertEqual(settings["live_ollama_model"], "none")
        self.assertEqual(settings["live_translate_last_model"], "qwen3:14b")

    def test_turn_off_restores_previous_model(self):
        settings["live_ollama_model"] = "qwen3:14b"
        Viewer, stub, _ = self._stub(False)
        Viewer._set_live_translate_off(stub, True)
        Viewer._set_live_translate_off(stub, False)
        self.assertEqual(settings["live_ollama_model"], "qwen3:14b")

    def test_turn_off_without_memory_uses_default(self):
        settings["live_ollama_model"] = "none"
        settings["live_translate_last_model"] = ""
        Viewer, stub, _ = self._stub(False)
        Viewer._set_live_translate_off(stub, False)
        self.assertEqual(settings["live_ollama_model"], DEFAULTS["live_ollama_model"])
        self.assertNotEqual(settings["live_ollama_model"], "none")

    def test_restarts_engine_only_when_running(self):
        settings["live_ollama_model"] = "qwen3:8b"
        Viewer, stub, calls = self._stub(True)
        Viewer._set_live_translate_off(stub, True)
        self.assertIn("restart", calls, "字幕在跑时没按新设置重启")
        Viewer, stub, calls = self._stub(False)
        Viewer._set_live_translate_off(stub, False)
        self.assertNotIn("restart", calls, "字幕没开时不该启动引擎")

    def test_no_change_no_restart(self):
        settings["live_ollama_model"] = "none"
        Viewer, stub, calls = self._stub(True)
        Viewer._set_live_translate_off(stub, True)
        self.assertNotIn("restart", calls)


class MenuWiringTests(unittest.TestCase):
    def test_menu_has_checkable_entry_in_subtitle_menu(self):
        src = (ROOT / "app" / "viewer.py").read_text(encoding="utf-8")
        body = src[src.index("def _build_media_menu"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertTrue("viewer.live_no_translate" in body, "右键菜单没有\"仅原语（不翻译）\"")
        seg = body[body.index("viewer.live_no_translate"):][:500]
        self.assertTrue("setCheckable(True)" in seg)
        self.assertTrue("_set_live_translate_off" in seg)

    def test_default_setting(self):
        self.assertEqual(DEFAULTS["live_translate_last_model"], "")


if __name__ == "__main__":
    unittest.main()
