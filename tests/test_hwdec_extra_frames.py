"""硬解帧池余量（绿屏 / 大片色块回归）。

背景 bug（用户实测）：一个 720x1280 H.264 Main 竖屏 mp4（has_b_frames=16，
手机/聊天软件转存常见）打开时绿屏、闪烁、色块糊成一片；播完一遍再拖回从头
或切软解就正常。真机逐帧对比：hwdec=auto-safe 在第 1~10 秒与软解像素差高达
92~96（满 255），出现整屏绿；mpv 默认 --hwdec-extra-frames=6，调到 12 起
差值降到 0.1，取 16 留余量。

- 配置测试：任何环境都跑，确认播放器传了足够的余量；
- 端到端：需要 NVIDIA 硬解 + 系统 ffmpeg + 图形界面，本地跑，其余自动跳过。
"""
import os
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ConfigTests(unittest.TestCase):
    def test_extra_frames_constant(self):
        from app import mpv_widget

        self.assertGreaterEqual(mpv_widget.HWDEC_EXTRA_FRAMES, 12,
                                "硬解帧池余量不足：重排深的片子会绿屏")

    def test_passed_to_mpv(self):
        src = (ROOT / "app" / "mpv_widget.py").read_text(encoding="utf-8")
        body = src[src.index("self.mpv = mpv.MPV("):]
        body = body[:body.index("\n        )")]
        self.assertTrue("hwdec_extra_frames=str(HWDEC_EXTRA_FRAMES)" in body,
                        "播放器创建 mpv 时没有传 hwdec-extra-frames")


def _repro_file() -> Path | None:
    """复现文件从环境变量读，路径不进仓库（用户私有文件）。

    合成视频复现不了：原文件的问题出在异常时间戳/POC 让解码器把重排缓冲
    撑到 16，任何重新封装都会把它"修正"，所以只能用原片。
    """
    p = os.environ.get("GP_HWDEC_REPRO_FILE")
    return Path(p) if p and Path(p).is_file() else None


@unittest.skipIf(os.name != "nt" or os.environ.get("CI") or _repro_file() is None,
                 "本地回归：设 GP_HWDEC_REPRO_FILE 指向复现视频后运行")
class HwdecEndToEndTests(unittest.TestCase):
    """原片：硬解（播放器同款余量）逐帧对比软解。"""

    TIMES = [0.3, 1.0, 2.0, 4.0, 6.0, 8.0, 9.5]

    @classmethod
    def setUpClass(cls):
        from app.runtime import init_libmpv

        try:
            init_libmpv()
        except RuntimeError as exc:
            raise unittest.SkipTest(f"libmpv unavailable: {exc}")
        cls.video = _repro_file()

    def _run(self, hwdec: str, extra: int | None):
        import mpv
        import numpy as np

        opts = {"hwdec-codecs": "all"}
        if extra is not None:
            opts["hwdec-extra-frames"] = str(extra)
        m = mpv.MPV(vo="gpu", ao="null", audio="no", hwdec=hwdec, keep_open="yes",
                    geometry="180x320+0+0", border="no", osc="no",
                    **{"gpu-api": "opengl"}, **opts)
        m.play(str(self.video))
        frames, used, got = {}, None, set()
        start = time.time()
        while time.time() - start < 16 and len(got) < len(self.TIMES):
            pos = m.time_pos
            if pos is None:
                time.sleep(0.005)
                continue
            used = m.hwdec_current or used
            for t in self.TIMES:
                if t not in got and pos >= t:
                    img = m.screenshot_raw("video")
                    if img is not None:
                        frames[t] = np.asarray(img.convert("RGB"), dtype=np.int16)
                    got.add(t)
            time.sleep(0.005)
        m.terminate()
        return used, frames

    def test_hw_matches_sw_with_player_setting(self):
        import numpy as np

        from app.mpv_widget import HWDEC_EXTRA_FRAMES

        _u, ref = self._run("no", None)
        used, hw = self._run("auto-safe", HWDEC_EXTRA_FRAMES)
        if not used or used == "no":
            self.skipTest("本机硬解不可用")
        worst = max(float(np.abs(hw[t] - ref[t]).mean()) for t in self.TIMES
                    if t in hw and t in ref and hw[t].shape == ref[t].shape)
        self.assertLess(worst, 8.0, f"硬解({used})与软解差 {worst:.1f}：绿屏/色块")


if __name__ == "__main__":
    unittest.main()