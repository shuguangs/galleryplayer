import unittest

import asr_engines


class FakeVAD:
    def generate(self, input):  # noqa: A002 - matches funasr API
        return [{"value": [(0, 800)]}]


class LanguageLockTests(unittest.TestCase):
    def test_detected_language_mapping(self):
        self.assertEqual(asr_engines.detected_language_code("English"), "en")
        self.assertEqual(asr_engines.detected_language_code("Cantonese"), "yue")
        self.assertIsNone(asr_engines.detected_language_code("English,Cantonese"))
        self.assertIsNone(asr_engines.detected_language_code(""))

    def test_qwen_locks_after_consecutive_segments(self):
        calls = []

        def fake_qwen(_model, _audio, lang):
            calls.append(lang)
            # 第 2 段故意混入一次误判；第 3-5 段连续 English 触发锁定。
            if len(calls) == 2:
                return "係啊。", "Cantonese"
            if len(calls) == 6:
                return "それね。", "Japanese"
            return "English text.", "English"

        original = asr_engines.qwen_transcribe
        asr_engines.qwen_transcribe = fake_qwen
        try:
            rows = list(asr_engines.stream_transcribe(
                object(), FakeVAD(), "qwen", [0.0] * (6 * asr_engines.SR),
                "auto", block_secs=1.0, language_lock_after=3,
            ))
        finally:
            asr_engines.qwen_transcribe = original

        self.assertEqual(len(rows), 6)
        self.assertEqual(calls, [None, None, None, None, None, "en"])


if __name__ == "__main__":
    unittest.main()
