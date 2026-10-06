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
class CoarseSeekContainerTests(unittest.TestCase):
    """容器 seek 落点远早于目标（ASF/WMV 实测早 5~17 秒）时，seek 解码的
    缓冲区样本 0 仍必须对应请求的 seek 时刻。

    回归点：旧实现按臆想的 (seek-2s) 起点裁剪，WMV 上多解的几秒全留在缓冲区，
    拖进度条后所有实时字幕整体推迟 ~8s（用户实测"时间戳错位"）。
    """

    BEEP_AT = 33.0

    @classmethod
    def setUpClass(cls):
        import subprocess

        cls._td = tempfile.TemporaryDirectory()
        cls.wmv = Path(cls._td.name) / "beep.wmv"
        subprocess.run([
            _ffmpeg(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=60",
            "-f", "lavfi", "-i",
            f"aevalsrc='if(between(t,{cls.BEEP_AT},{cls.BEEP_AT + 0.5}),"
            f"0.8*sin(2*PI*1000*t),0)':s=44100:d=60",
            "-c:v", "wmv2", "-g", "250", "-c:a", "wmav2", "-b:a", "64k",
            str(cls.wmv)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _beep_media_time(self, seek: float) -> float:
        import numpy as np

        from live_transcribe import _decode_audio_from

        audio = _decode_audio_from(str(self.wmv), seek,
                                   max_seconds=float("inf"),
                                   should_cancel=lambda: False)
        hop = 160
        energy = np.array([np.abs(audio[i:i + hop]).mean()
                           for i in range(0, len(audio) - hop, hop)])
        self.assertTrue((energy > 0.1).any(), "缓冲区里找不到提示音")
        return seek + int(np.argmax(energy > 0.1)) * hop / 16000.0

    def test_container_seek_is_actually_coarse(self):
        """前提自检：这个夹具确实能复现"容器 seek 早落"——否则下面的
        断言对旧实现也会通过，测试形同虚设。"""
        import av

        with av.open(str(self.wmv)) as c:
            a = c.streams.audio[0]
            c.seek(int(28.0 / float(a.time_base)), stream=a)
            first = next(fr for fr in c.decode(a) if fr.pts is not None)
            landed = float(first.pts * a.time_base)
        self.assertLess(landed, 27.0, "夹具未复现粗粒度 seek，测试失效")

    def test_seek_buffer_aligned_to_media_time(self):
        for seek in (10.0, 30.0):
            with self.subTest(seek=seek):
                self.assertAlmostEqual(self._beep_media_time(seek),
                                       self.BEEP_AT, delta=0.15)

    def test_seek_zero_unchanged(self):
        self.assertAlmostEqual(self._beep_media_time(0.0),
                               self.BEEP_AT, delta=0.15)


@unittest.skipUnless(_av_available() and _ffmpeg(), "需要 av 与系统 ffmpeg")
class SparseDownloadTests(unittest.TestCase):
    """边下边播：文件按完整大小预分配，未下载区全是 0（迅雷/BT/网盘挂载的
    典型落盘方式）。用户拖进度条后下载跳到后面，形成"前段 + 空洞 + 后段"。

    回归点：旧实现读到第一个坏包解码器生成器就终止——seek 正好落在空洞里
    时一个字都解不出（实测 65~96s 数据已在盘上，解出 0 秒）。
    """

    BEEPS = (10, 40, 70, 100)

    @classmethod
    def setUpClass(cls):
        import subprocess

        cls._td = tempfile.TemporaryDirectory()
        src = Path(cls._td.name) / "beeps.mp4"
        expr = "+".join(f"between(t,{b},{b + 0.4})" for b in cls.BEEPS)
        subprocess.run([
            _ffmpeg(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=120",
            "-f", "lavfi", "-i",
            f"aevalsrc='0.8*sin(2*PI*1000*t)*({expr})':s=44100:d=120",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac",
            "-movflags", "+faststart", str(src)], check=True)
        data = src.read_bytes()
        n = len(data)

        def holes(name, ranges):
            buf = bytearray(n)
            for a, b in ranges:
                buf[int(a * n):int(b * n)] = data[int(a * n):int(b * n)]
            p = Path(cls._td.name) / name
            p.write_bytes(bytes(buf))
            return p

        # 音频 65s 约在文件 54.5%，96s 约在 80%
        cls.jump = holes("jump.mp4", [(0, 0.20), (0.55, 0.80)])
        cls.head = holes("head.mp4", [(0, 0.30)])
        cls.full = src

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _beeps(self, path, seek):
        import numpy as np

        from live_transcribe import _decode_audio_from

        audio = _decode_audio_from(str(path), seek, max_seconds=float("inf"),
                                   should_cancel=lambda: False)
        hop = 160
        e = np.array([np.abs(audio[i:i + hop]).mean()
                      for i in range(0, len(audio) - hop, hop)])
        on = e > 0.1
        out = []
        for i in range(len(on)):
            if on[i] and (i == 0 or not on[i - 1]):
                out.append(round(seek + i * hop / 16000, 1))
        return out, len(audio) / 16000

    def test_seek_into_downloaded_island_after_hole(self):
        got, _ = self._beeps(self.jump, 60.0)
        self.assertEqual(len(got), 1, f"空洞后的已下载段没解出来：{got}")
        self.assertAlmostEqual(got[0], 70.0, delta=0.2, msg="空洞后的时间戳错位")

    def test_stops_at_download_frontier(self):
        """顺序下载中：解到已下载的边界就停，不跨过空洞把后面的内容接上来。"""
        got, secs = self._beeps(self.head, 0.0)
        self.assertEqual(got, [10.0])
        self.assertLess(secs, 40.0)

    def test_gap_task_does_not_jump_to_far_island(self):
        """补空缺的任务从空洞里开始：不能一路跳到 40 秒外的下一段（那段已由
        别的任务负责，跳过去会重复识别，时间也不对）。"""
        got, _secs = self._beeps(self.jump, 25.0)
        self.assertEqual(got, [], f"从空洞起的任务跳去了远处的数据段：{got}")

    def test_complete_file_unchanged(self):
        got, _ = self._beeps(self.full, 0.0)
        self.assertEqual(got, [10.0, 40.0, 70.0, 100.0])
        got, _ = self._beeps(self.full, 65.0)
        self.assertEqual(got, [70.0, 100.0])


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
