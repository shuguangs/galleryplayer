import unittest

from PySide6.QtCore import QCoreApplication

from app.caption_text import (
    apply_glossary,
    deduplicate_rows,
    format_bilingual,
    merge_short_rows,
)
from app.live_caption_controller import LiveCaptionController
from app.live_engine_state import EngineEvent, parse_engine_line


class LiveCaptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_generation_filter_and_ranges(self):
        ctl = LiveCaptionController()
        ctl.begin_media("a.mp4", 100, 7, True)
        self.assertFalse(ctl.accept_line({"g": 6, "t": 1, "end": 2, "text": "old"}))
        self.assertTrue(ctl.accept_line({"g": 7, "t": 100, "end": 102, "text": "hello", "zh": "你好"}))
        self.assertFalse(ctl.accept_line({"g": 7, "t": 100, "end": 102, "text": "hello", "zh": "你好"}))
        self.assertEqual(ctl.caption_ranges(), [(100.0, 102.0)])
        self.assertTrue(ctl.is_covered(101))
        self.assertFalse(ctl.is_covered(103))

    def test_covered_seek_cancels_restart(self):
        ctl = LiveCaptionController()
        ctl.begin_media("a.mp4", 0, 1, True)
        ctl.accept_line({"g": 1, "t": 100, "end": 110, "text": "hello", "zh": "你好"})
        ctl.request_restart(500)
        self.assertTrue(ctl.restart_timer.isActive())
        self.assertEqual(ctl.handle_position(105, True), "covered")
        self.assertFalse(ctl.restart_timer.isActive())

    def test_full_pass_starts_at_missing_range(self):
        ctl = LiveCaptionController()
        ctl.begin_media("a.mp4", 500, 2, True)
        ctl.accept_line({"g": 2, "t": 500, "end": 600, "text": "later", "zh": "后面"})
        self.assertEqual(ctl.next_full_pass_start(), 0.0)
        ctl.accept_line({"g": 2, "t": 0, "end": 100, "text": "early", "zh": "前面"})
        self.assertEqual(ctl.next_full_pass_start(), 100.0)

    def test_text_postprocessing(self):
        rows = [
            (0, 1, "Hello.", "你好。"),
            (1, 2, "Hello.", "你好。"),
            (2, 3, "world", "世界"),
            (5.2, 6, "Nelson", "纳尔逊"),
        ]
        merged = merge_short_rows(rows)
        self.assertEqual([row[2] for row in merged], ["Hello. world", "Nelson"])
        self.assertEqual(apply_glossary("hello Nelson", {"Nelson": "纳尔逊"}), "hello 纳尔逊")
        self.assertEqual(format_bilingual("hello", "你好", 1.0), "hello\n你好")
        self.assertEqual(format_bilingual("hello", "你好", 0.0), "你好")

    def test_engine_event_parser(self):
        self.assertEqual(parse_engine_line("# MODEL_READY").event, EngineEvent.MODEL_READY)
        event = parse_engine_line("# TASK_DONE 12")
        self.assertEqual(event.event, EngineEvent.TASK_DONE)
        self.assertEqual(event.generation, 12)
        self.assertEqual(parse_engine_line('{"g":1}'), None)

    def test_model_preset_overrides_stale_model_setting(self):
        from app.config import settings
        from app.live_engine import effective_model

        old = {
            key: settings[key]
            for key in ("live_model_preset", "live_asr_model", "hardware_aware_model")
        }
        try:
            settings["live_model_preset"] = "balanced"
            settings["live_asr_model"] = "large-v3"
            settings["hardware_aware_model"] = False
            self.assertEqual(effective_model(), "medium")
            settings["live_model_preset"] = "custom"
            self.assertEqual(effective_model(), "large-v3")
        finally:
            for key, value in old.items():
                settings[key] = value


if __name__ == "__main__":
    unittest.main()
