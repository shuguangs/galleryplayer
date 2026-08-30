"""容器音频时间轴偏移回归测试。

背景：faster-whisper 的 decode_audio 按样本序解码并丢弃时间戳，VAD/whisper
报的时间相对"音频流首帧"；播放器按容器时间轴呈现（MP4 edit list / TS 起始
PTS / MKV codec delay 会让首帧落在非 0 的媒体时刻）。生成 SRT 与实时字幕
必须把 audio_stream_start 加回时间戳，否则字幕整体提早或推迟。

分三层：
- 纯 mock（CI 可跑，需 av）：audio_stream_start 的换算与容错；
- 纯逻辑（CI 可跑）：write_srt_file 负时间钳 0；
- 端到端（需系统 ffmpeg + av + fsmn-vad，本地跑）：真实带偏移 MP4 上
  验证 VAD 时间戳 + 偏移 = 媒体时间、seek 解码不多裁前导。
"""
import shutil
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "jfk.wav"


def _av_available() -> bool:
    try:
        import av  # noqa: F401

        return True
    except ImportError:
        return False


class _FakeStream:
    def __init__(self, stream_type="audio", start_time=0, time_base=None):
        self.type = stream_type
        self.start_time = start_time
        self.time_base = time_base or Fraction(1, 44100)


class _FakeContainer:
    def __init__(self, streams):
        self.streams = streams

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@unittest.skipUnless(_av_available(), "av 未安装（仅引擎 venv 有）")
class AudioStreamStartTests(unittest.TestCase):
    """audio_stream_start：流 start_time → 秒，及各容错分支。"""

    def test_converts_stream_start_time_to_seconds(self):
        import asr_engines

        container = _FakeContainer([_FakeStream(start_time=20992,
                                                time_base=Fraction(1, 44100))])
        with mock.patch("av.open", return_value=container):
            self.assertAlmostEqual(
                asr_engines.audio_stream_start("x.mp4"), 20992 / 44100.0)

    def test_negative_start_time_is_preserved(self):
        """负偏移（音频早于媒体 0 呈现）必须原样返回：T = B + S 靠它成立。"""
        import asr_engines

        container = _FakeContainer([_FakeStream(start_time=-1024,
                                                time_base=Fraction(1, 48000))])
        with mock.patch("av.open", return_value=container):
            self.assertAlmostEqual(
                asr_engines.audio_stream_start("x.mkv"), -1024 / 48000.0)

    def test_none_start_time_returns_zero(self):
        import asr_engines

        container = _FakeContainer([_FakeStream(start_time=None)])
        with mock.patch("av.open", return_value=container):
            self.assertEqual(asr_engines.audio_stream_start("x.mp4"), 0.0)

    def test_no_audio_stream_returns_zero(self):
        import asr_engines

        container = _FakeContainer([_FakeStream(stream_type="video")])
        with mock.patch("av.open", return_value=container):
            self.assertEqual(asr_engines.audio_stream_start("x.mp4"), 0.0)

    def test_open_failure_returns_zero(self):
        import asr_engines

        with mock.patch("av.open", side_effect=OSError("boom")):
            self.assertEqual(asr_engines.audio_stream_start("bad.mp4"), 0.0)

    def test_plain_wav_has_zero_offset(self):
        import asr_engines

        if not SAMPLE.is_file():
            self.skipTest(f"缺少样本 {SAMPLE}")
        self.assertEqual(asr_engines.audio_stream_start(str(SAMPLE)), 0.0)


class WriteSrtNegativeClampTests(unittest.TestCase):
    """负时间戳（容器负偏移的极端情况）必须钳到 0，不得产出非法时间。"""

    def test_srt_negative_timestamps_clamped(self):
        from translate_service import write_srt_file

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.zh.srt"
            write_srt_file(path, [(-0.5, 1.0, "Hi.", "你好。")], fmt="srt")
            text = path.read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:01,000", text)

    def test_vtt_negative_timestamps_clamped(self):
        from translate_service import write_srt_file

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.zh.vtt"
            write_srt_file(path, [(-0.5, 1.0, "Hi.", "")], fmt="vtt")
            text = path.read_text(encoding="utf-8")
        self.assertIn("00:00:00.000 --> 00:00:01.000", text)

    def test_ass_negative_timestamps_clamped(self):
        from translate_service import write_srt_file

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.zh.ass"
            write_srt_file(path, [(-0.5, 1.0, "Hi.", "")], fmt="ass")
            text = path.read_text(encoding="utf-8")
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.00,Sub,Hi.", text)


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _make_offset_mp4s(td: str) -> tuple[Path, Path]:
    """生成对照文件：base.mp4（音视频同起）与 offset.mp4（音频被 edit list
    推迟约 0.5s 呈现）。模拟屏幕录制/转存视频的常见容器布局。"""
    import subprocess

    ffmpeg = _ffmpeg()
    base = Path(td) / "base.mp4"
    offset = Path(td) / "offset.mp4"
    common = ["-y", "-v", "error",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
              "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=3",
              "-c:a", "aac", "-b:a", "128k", "-c:v", "libx264", "-shortest"]
    subprocess.run([ffmpeg, *common, str(base)], check=True)
    subprocess.run([ffmpeg, "-y", "-v", "error",
                    "-i", str(base), "-itsoffset", "0.5", "-i", str(base),
                    "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "copy", str(offset)], check=True)
    return base, offset


@unittest.skipUnless(_av_available() and _ffmpeg(), "需要 av 与系统 ffmpeg")
class ContainerOffsetDecodeTests(unittest.TestCase):
    """真实带偏移容器上的解码与偏移读取（本地回归，CI 自动跳过）。"""

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.base, cls.offset = _make_offset_mp4s(cls._td.name)

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def test_offset_is_read_from_container(self):
        import asr_engines

        self.assertEqual(asr_engines.audio_stream_start(str(self.base)), 0.0)
        off = asr_engines.audio_stream_start(str(self.offset))
        self.assertGreater(off, 0.3, "edit list 偏移未被读出")
        self.assertLess(off, 0.7)

    def test_seek_below_first_frame_does_not_overtrim(self):
        """seek 落在音频首帧之前：不得按 (seek - start_seconds) 多裁前导。

        回归点：旧实现 keep_from = (seek - start_seconds)*16000，对首帧
        0.476s 的流 seek=0.3 会裁掉 0.3s 真实音频（起点错标成 0.3）。
        """
        from live_transcribe import _decode_audio_from

        full = _decode_audio_from(str(self.offset), 0.0,
                                  max_seconds=float("inf"),
                                  should_cancel=lambda: False)
        partial = _decode_audio_from(str(self.offset), 0.3,
                                     max_seconds=float("inf"),
                                     should_cancel=lambda: False)
        self.assertEqual(len(partial), len(full),
                         "seek < 首帧媒体时间时仍发生了前导裁剪")


@unittest.skipUnless(_av_available() and _ffmpeg(), "需要 av 与系统 ffmpeg")
class ContainerOffsetVadTests(unittest.TestCase):
    """端到端：VAD 时间戳 + 容器偏移 = 媒体时间（本地回归，CI 自动跳过）。

    需要 fsmn-vad 模型（引擎目录 models/models/fsmn-vad）。
    """

    @classmethod
    def setUpClass(cls):
        import asr_engines

        if not asr_engines.VAD_DIR.is_dir():
            raise unittest.SkipTest("fsmn-vad 模型未安装")
        cls._td = tempfile.TemporaryDirectory()
        cls.base, cls.offset = _make_offset_mp4s(cls._td.name)
        cls.vad = asr_engines.load_vad()

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def test_vad_plus_offset_equals_media_time(self):
        """offset.mp4 的正弦波在媒体时间 ~0.476s 才响起（播放器按 edit list
        呈现）；解码缓冲区里它在样本 0。修好后：VAD 首段 + 偏移 ≈ 偏移本身。
        """
        import asr_engines
        from faster_whisper import decode_audio

        off = asr_engines.audio_stream_start(str(self.offset))
        self.assertGreater(off, 0.3)
        audio = decode_audio(str(self.offset), sampling_rate=16000)
        segs = asr_engines.vad_segments(self.vad, audio)
        self.assertTrue(segs, "VAD 未检出正弦波")
        first_start = min(s for s, _e in segs)
        # 修复前 first_start ≈ 0（提早 off 秒）；修复语义上应为媒体时间
        self.assertAlmostEqual(first_start + off, off, delta=0.35)


if __name__ == "__main__":
    unittest.main()
