import unittest

from live_transcribe import split_words_to_lines


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


if __name__ == "__main__":
    unittest.main()
