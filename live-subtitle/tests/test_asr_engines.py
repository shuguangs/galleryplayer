import unittest

import asr_engines


class FakeVAD:
    """可配置段时长的假 VAD：segments 为 [(start_ms, end_ms), ...]。"""

    def __init__(self, segments):
        self._segments = segments

    def generate(self, input):  # noqa: A002 - matches funasr API
        return [{"value": self._segments}]


class Lang:
    """假语言码，便于测试中区分投票序列。"""

    def __init__(self, code):
        self.code = code

    def __eq__(self, other):
        return isinstance(other, Lang) and other.code == self.code

    def __hash__(self):
        return hash(self.code)

    def __repr__(self):
        return f"Lang({self.code})"


def _silence(total_secs: float) -> list[float]:
    return [0.0] * int(total_secs * asr_engines.SR)


class LanguageLockTests(unittest.TestCase):
    """新语言策略：长段(≥LOCK_MIN_SECS)多数投票；短段用锁定语言强制。"""

    def _run(self, vad_segments, fake_results, block_secs=600.0):
        """跑 stream_transcribe，返回 (每次调用的语言参数, 输出行数)。

        fake_results: 每次 qwen_transcribe 调用返回的 (text, detected)；
        detected 需要能被 detected_language_code 映射——直接 monkeypatch
        detected_language_code 更简单：fake_results 给语言码列表。
        """
        calls = []

        def fake_qwen(_model, _audio, lang):
            calls.append(lang)
            i = len(calls) - 1
            text, detected = fake_results[i] if i < len(fake_results) else ("x.", "English")
            return text, detected

        original_qwen = asr_engines.qwen_transcribe
        original_code = asr_engines.detected_language_code
        asr_engines.qwen_transcribe = fake_qwen
        asr_engines.detected_language_code = lambda d: d  # 直接透传语言码
        try:
            # 段时间戳是毫秒；音频长度按最长段尾留 1s 余量
            total_secs = max(e for _s, e in vad_segments) / 1000.0 + 1.0
            rows = list(asr_engines.stream_transcribe(
                object(), FakeVAD(vad_segments), "qwen",
                _silence(total_secs), "auto",
                block_secs=block_secs, language_lock_after=3,
            ))
        finally:
            asr_engines.qwen_transcribe = original_qwen
            asr_engines.detected_language_code = original_code
        return calls, rows

    def test_short_segments_never_vote_or_lock(self):
        """短段（<LOCK_MIN_SECS）不投票：语气词连片误判不再锁死全片。

        回归：多语言测试样本 前 8 段里 3 个"嗯/诶"短段被误判 Chinese，
        旧逻辑第 8 段锁 zh 全片报废。新逻辑：短段全程 auto，永不锁。
        """
        # 6 个 0.8s 短段，检测全是 zh（模拟误判连片）
        segs = [(i * 1000, i * 1000 + 800) for i in range(6)]
        results = [(f"嗯{i}。", "zh") for i in range(6)]
        calls, rows = self._run(segs, results)
        # 没有长段投票 → 无锁 → 全部 auto（None）
        self.assertEqual(calls, [None] * 6)
        self.assertEqual(len(rows), 6)

    def test_long_majority_locks_short_segments(self):
        """长段多数投票 → 后续短段用锁定语言（原"Yeah"防抖语义保留）。"""
        # 3 个 2.5s 长段检测 en → 锁 en；之后 2 个短段强制 en
        segs = [
            (0, 2500), (3000, 5500), (6000, 8500),        # 长段 ×3
            (10000, 10500), (11000, 11400),                # 短段 ×2
        ]
        results = [
            ("hello world.", "en"), ("more text.", "en"), ("yes.", "en"),
            ("Yeah", "en"), ("ok", "en"),
        ]
        calls, rows = self._run(segs, results)
        # 前三个（长段）auto；第 3 个长段投票后锁成立 → 后两个短段强制 en
        self.assertEqual(calls, [None, None, None, "en", "en"])

    def test_single_outlier_does_not_break_majority(self):
        """单个误判长段不清零多数（旧逻辑连续计数一断就重来）。"""
        # 4 个长段：en, zh(误判), en, en → 窗口内 en 占 3/4 → 锁 en
        segs = [(i * 3000, i * 3000 + 2500) for i in range(4)]
        # 第 5 个短段应被强制 en（若被 zh 误判带偏则错）
        segs.append((13000, 13600))
        results = [
            ("a.", "en"), ("嗯。", "zh"), ("b.", "en"), ("c.", "en"),
            ("Yeah", "en"),
        ]
        calls, rows = self._run(segs, results)
        self.assertEqual(calls[-1], "en")

    def test_lock_follows_sliding_window(self):
        """锁随投票窗口滑动：误锁可被后续长段自然纠正（可变性）。"""
        # 长段序列：zh,zh,zh（误锁 zh）→ ja,ja,ja,ja,ja（窗口翻转为 ja）
        segs = [(i * 3000, i * 3000 + 2500) for i in range(8)]
        results = [
            ("嗯1。", "zh"), ("嗯2。", "zh"), ("嗯3。", "zh"),
            ("あ。", "ja"), ("い。", "ja"), ("う。", "ja"), ("え。", "ja"), ("お。", "ja"),
        ]
        calls, rows = self._run(segs, results)
        # 长段恒 auto；第 8 段时窗口=[zh,ja,ja,ja,ja]（窗口5）多数 ja
        # （此处只验证长段不被锁污染）
        self.assertEqual(calls, [None] * 8)

    def test_forced_language_bypasses_all(self):
        """用户显式指定语言（非 auto）：全程强制，投票/锁定全不参与。"""
        segs = [(0, 2500), (3000, 5500)]
        results = [("a.", "en"), ("b.", "en")]
        calls = []
        original_qwen = asr_engines.qwen_transcribe

        def fake_qwen(_m, _a, lang):
            calls.append(lang)
            i = len(calls) - 1
            return results[i]

        asr_engines.qwen_transcribe = fake_qwen
        try:
            list(asr_engines.stream_transcribe(
                object(), FakeVAD(segs), "qwen", _silence(7.0), "ja",
                block_secs=600.0))
        finally:
            asr_engines.qwen_transcribe = original_qwen
        self.assertEqual(calls, ["ja", "ja"])


if __name__ == "__main__":
    unittest.main()
