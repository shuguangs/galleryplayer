"""translate_service 纯函数单测：token 清理与三格式字幕导出。"""
import tempfile
import unittest
from pathlib import Path

from translate_service import SCENARIO_HINTS, clean_output, system_prompt, write_srt_file


class SystemPromptTests(unittest.TestCase):
    """提示词第 1 条要求必须跟着目标语言走（英文目标不能再要求"中文口语腔"）。"""

    def test_chinese_targets_keep_chinese_style(self):
        for target in ("zh", "zh-Hant"):
            prompt = system_prompt(target)
            self.assertIn("中文影视字幕的口语腔", prompt)
        self.assertIn("简体中文", system_prompt("zh"))
        self.assertIn("繁体中文", system_prompt("zh-Hant"))

    def test_english_target_drops_chinese_style(self):
        prompt = system_prompt("en")
        self.assertIn("English", prompt)
        self.assertNotIn("中文影视字幕的口语腔", prompt)
        self.assertNotIn("英式/日式语序", prompt)

    def test_unknown_target_falls_back_to_raw_name(self):
        prompt = system_prompt("ja")
        self.assertIn("ja", prompt)
        self.assertNotIn("{", prompt)  # 占位符全部填上，没有漏的 format 键


class ScenarioPromptTests(unittest.TestCase):
    """场景提示词组：general 不追加；nsfw 粗俗对等；各场景非空且可组装。"""

    def test_general_has_no_extra_block(self):
        base = system_prompt("zh")
        self.assertNotIn("场景补充", base)
        self.assertEqual(system_prompt("zh", "general"), base)

    def test_nsfw_keeps_vulgar_register(self):
        p = system_prompt("zh", "nsfw")
        self.assertIn("场景补充", p)
        self.assertIn("鸡巴", p)          # 粗俗对等，而非临床词
        self.assertIn("禁止", p)

    def test_all_scenarios_loaded_from_json(self):
        for key, hint in SCENARIO_HINTS.items():
            if key == "general":
                self.assertEqual(hint, "")
                self.assertEqual(system_prompt("zh", key), system_prompt("zh"))
                continue
            self.assertTrue(hint.strip(), key)
            self.assertIn("场景补充——" + hint, system_prompt("zh", key))

    def test_translator_payload_carries_scenario(self):
        import json as _json

        from translate_service import Translator

        tr = Translator("http://127.0.0.1:9", "test-model", scenario="nsfw")
        payload = _json.loads(tr._payload("hello"))
        self.assertIn("鸡巴", payload["messages"][0]["content"])


class CleanOutputTests(unittest.TestCase):
    def test_strips_junk_tokens(self):
        self.assertEqual(
            clean_output("前 <|END_OF_TURN_TOKEN|> 后 <|im_end|>"),
            "前  后",
        )

    def test_strips_hy_tokens(self):
        self.assertEqual(
            clean_output("<｜hy_User｜>台词<｜hy_Assistant｜>"), "台词")

    def test_strips_wrapping_quotes(self):
        self.assertEqual(clean_output('"Hello there."'), "Hello there.")
        self.assertEqual(clean_output("「你好」"), "你好")
        # 只有一侧引号不算包裹，不能剥
        self.assertEqual(clean_output('"不平衡'), '"不平衡')

    def test_think_block_removed(self):
        # JUNK_TOKENS 按标记剥离，标记之间的内容保留
        self.assertEqual(clean_output("<think>x</think>答案"), "x答案")

    def test_plain_text_untouched(self):
        self.assertEqual(clean_output("算法不行，再快点。"), "算法不行，再快点。")


class WriteSubtitleTests(unittest.TestCase):
    ROWS = [
        (0.0, 2.5, "Hello.", "你好。"),
        (3.0, 5.0, "Cut me some slack.", ""),  # 译文空 → 只写原文
    ]

    def test_srt_format(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.zh.srt"
            write_srt_file(path, self.ROWS, fmt="srt")
            text = path.read_text(encoding="utf-8")
        self.assertIn("1\n00:00:00,000 --> 00:00:02,500\nHello.\n你好。\n", text)
        self.assertIn("00:00:03,000 --> 00:00:05,000\nCut me some slack.\n", text)
        self.assertNotIn("WEBVTT", text)

    def test_vtt_format(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.zh.vtt"
            write_srt_file(path, self.ROWS, fmt="vtt")
            text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:02.500", text)
        self.assertNotIn("--> 00:00:02,500", text)  # VTT 用 . 不是 ,

    def test_ass_format(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.zh.ass"
            write_srt_file(path, self.ROWS, fmt="ass")
            text = path.read_text(encoding="utf-8")
        self.assertIn("[Script Info]", text)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:02.50,Sub,Hello.\\N你好。", text)
        self.assertIn("Dialogue: 0,0:00:03.00,0:00:05.00,Sub,Cut me some slack.", text)

    def test_default_is_srt(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.srt"
            write_srt_file(path, self.ROWS)
            self.assertNotIn("WEBVTT", path.read_text(encoding="utf-8"))

    def test_two_line_translation_is_single_event(self):
        """双语在 ASS 里用 \\N 软换行（一个 Dialogue 事件），VTT 里是真实换行。"""
        with tempfile.TemporaryDirectory() as td:
            p_srt = Path(td) / "b.srt"
            p_ass = Path(td) / "b.ass"
            write_srt_file(p_srt, self.ROWS[:1], fmt="srt")
            write_srt_file(p_ass, self.ROWS[:1], fmt="ass")
            self.assertEqual(p_srt.read_text(encoding="utf-8").count("\n"), 4)
            self.assertEqual(p_ass.read_text(encoding="utf-8").count("Dialogue:"), 1)


if __name__ == "__main__":
    unittest.main()
