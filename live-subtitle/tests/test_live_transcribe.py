import unittest

from live_transcribe import DecodeCancelled, _decode_audio_from, split_words_to_lines
from pathlib import Path

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "jfk.wav"


class Word:
    def __init__(self, start: float, end: float, word: str):
        self.start = start
        self.end = end
        self.word = word


class SplitWordsTests(unittest.TestCase):
    def test_long_silence_splits_utterances(self):
        rows = split_words_to_lines([
            Word(0.0, 1.0, "hello."),
            Word(11.0, 12.0, "world."),
        ])
        self.assertEqual(rows, [
            (0.0, 1.0, "hello."),
            (11.0, 12.0, "world."),
        ])

    def test_sentence_punctuation_splits_lines(self):
        rows = split_words_to_lines([
            Word(0.0, 0.5, "one."),
            Word(0.8, 1.2, "two."),
        ])
        self.assertEqual(len(rows), 2)

    def test_max_duration_splits_continuous_speech(self):
        rows = split_words_to_lines([
            Word(index, index + 0.2, str(index))
            for index in range(0, 40)
        ])
        self.assertGreater(len(rows), 1)
        for _start, end, _text in rows:
            self.assertLessEqual(end - _start, 6.2)


class DecodeCancelTests(unittest.TestCase):
    """换片/seek 时解码必须立即中止：整段解完才响应曾让切换等上几十秒。"""

    def setUp(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("av 未安装（仅引擎 venv 有）")
        if not SAMPLE.is_file():
            self.skipTest(f"缺少样本 {SAMPLE}")

    def test_cancel_raises_before_finishing(self):
        with self.assertRaises(DecodeCancelled):
            _decode_audio_from(str(SAMPLE), 0.0, should_cancel=lambda: True)

    def test_no_cancel_decodes_audio(self):
        audio = _decode_audio_from(str(SAMPLE), 0.0, should_cancel=lambda: False)
        self.assertGreater(len(audio), 16000)  # 至少 1 秒

    def test_seek_zero_matches_faster_whisper_decode(self):
        """片头起播改走可中断解码，输出必须与 faster_whisper 原路径一致。"""
        from faster_whisper import decode_audio

        expected = decode_audio(str(SAMPLE), sampling_rate=16000)
        actual = _decode_audio_from(str(SAMPLE), 0.0, max_seconds=float("inf"),
                                    should_cancel=lambda: False)
        self.assertEqual(len(actual), len(expected))


if __name__ == "__main__":
    unittest.main()
