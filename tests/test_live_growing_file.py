"""边下边播时实时字幕自动跟进（播放器侧调度）。

背景（实测）：引擎把"当时已下载的部分"解码完就报 TASK_DONE；播放器立刻派的
补洞任务一读就撞到下载前沿、0 行产出，此后再没人派任务——字幕停住直到下载完。
另：没有人声的片子同样 0 产出，补洞立刻重派，日志里同一文件"做完" 84 次（空转）。

规则（LiveCaptionController.after_task）：
- 覆盖到片尾 → done；
- 文件还在变（大小 或 修改时间；迅雷/BT 预分配完整大小，只有 mtime 在变）→
  wait：几秒后再派，间隔 3s 起、无新进展逐步拉长到 15s；
- 文件没变且本任务有新产出 → 立即补洞（原行为）；
- 文件没变且本任务 0 产出 → idle：停止重派（修空转）；
- 文件超过 120s 没变 → idle。
补洞起点优先接播放位置所在的那段往后，而不是回头补最早的空缺。
"""
import unittest
from pathlib import Path

from app.live_caption_controller import (GROW_STALE_S, GROW_WAIT_MAX_MS,
                                         GROW_WAIT_MIN_MS, LiveCaptionController)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _ctl(stat, clock, duration=600.0):
    c = LiveCaptionController(stat_fn=stat, clock=clock)
    c.begin_media(Path("D:/dl/a.mp4"), 0.0, 1, catching=False, duration=duration)
    return c


def _line(c, t0, t1, text="hello"):
    return c.accept_line({"g": c.generation, "t": t0, "end": t1, "text": text, "zh": ""})


class AfterTaskTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.stat_val = (1000, 1.0)
        self.c = _ctl(lambda p: self.stat_val, self.clock)

    def _next_task(self, start=None):
        self.c.begin_full_pass(self.c.generation + 1,
                               start if start is not None else self.c.next_full_pass_start(600.0))

    def test_complete_local_file_keeps_old_behaviour(self):
        _line(self.c, 0, 5)
        action, _ = self.c.after_task(self.c.generation)
        self.assertEqual(action, "full_pass", "文件没变、有新产出 → 立即补洞（原行为）")

    def test_growing_size_waits(self):
        _line(self.c, 0, 5)
        self.stat_val = (2000, 2.0)
        action, delay = self.c.after_task(self.c.generation)
        self.assertEqual(action, "wait")
        self.assertEqual(delay, GROW_WAIT_MIN_MS)

    def test_preallocated_file_mtime_only_counts_as_growing(self):
        """迅雷/BT：文件一开始就是完整大小，下载中只有修改时间在变。"""
        _line(self.c, 0, 5)
        self.stat_val = (1000, 9.0)
        action, _ = self.c.after_task(self.c.generation)
        self.assertEqual(action, "wait")

    def test_backoff_without_progress_caps_at_max(self):
        delays = []
        for i in range(8):
            self.stat_val = (1000 + i + 1, float(i + 2))   # 一直在变
            action, delay = self.c.after_task(self.c.generation)
            self.assertEqual(action, "wait")
            delays.append(delay)
            self._next_task()                              # 0 产出的新任务
        self.assertEqual(delays[0], GROW_WAIT_MIN_MS)
        self.assertTrue(all(b >= a for a, b in zip(delays, delays[1:])), delays)
        self.assertEqual(delays[-1], GROW_WAIT_MAX_MS)

    def test_progress_resets_backoff(self):
        for i in range(3):
            self.stat_val = (1000 + i + 1, float(i + 2))
            self.c.after_task(self.c.generation)
            self._next_task()
        _line(self.c, 0, 5)                                # 这次有新产出
        self.stat_val = (5000, 50.0)
        _action, delay = self.c.after_task(self.c.generation)
        self.assertEqual(delay, GROW_WAIT_MIN_MS)

    def test_zero_output_and_unchanged_file_stops(self):
        """没有人声/文件不变：0 产出就停，不再空转重派（旧版连刷 84 次）。"""
        action, _ = self.c.after_task(self.c.generation)
        self.assertEqual(action, "idle")

    def test_stale_download_stops(self):
        """文件很久没变（下载停了）：即使这次任务有新产出，也不再立刻补洞。"""
        _line(self.c, 0, 5)
        self.stat_val = (2000, 2.0)
        self.assertEqual(self.c.after_task(self.c.generation)[0], "wait")
        self._next_task()
        _line(self.c, 5, 10, "more")                         # 有新产出
        self.clock.t += GROW_STALE_S + 1                     # 但文件很久没变
        action, _ = self.c.after_task(self.c.generation)
        self.assertEqual(action, "idle")

    def test_recently_changed_file_with_output_still_full_passes(self):
        """对照：文件不久前还在变、这次没变但有产出 → 照常补洞（不被超时误伤）。"""
        _line(self.c, 0, 5)
        self.stat_val = (2000, 2.0)
        self.c.after_task(self.c.generation)
        self._next_task()
        _line(self.c, 5, 10, "more")
        self.clock.t += 10
        self.assertEqual(self.c.after_task(self.c.generation)[0], "full_pass")

    def test_covered_to_end_is_done(self):
        _line(self.c, 0, 599.5)
        self.stat_val = (2000, 2.0)
        self.assertEqual(self.c.after_task(self.c.generation)[0], "done")

    def test_stale_generation_ignored(self):
        self.assertEqual(self.c.after_task(self.c.generation - 1)[0], "ignored")

    def test_missing_file_is_idle(self):
        def gone(_p):
            raise OSError
        c = _ctl(gone, self.clock)
        self.assertEqual(c.after_task(c.generation)[0], "idle")


class ResumePointTests(unittest.TestCase):
    """seek 导致下载跳跃：补洞优先接"播放位置所在段"往后。"""

    def test_resume_after_playback_segment_not_earliest_gap(self):
        c = _ctl(lambda p: (1, 1.0), _Clock())
        _line(c, 0, 30, "a")                      # 前面下载的一段
        c.begin_media(Path("D:/dl/a.mp4"), 300, 2, catching=False, duration=600)
        _line(c, 300, 340, "b")                   # 拖到 300s 后下载并识别的一段
        self.assertEqual(c.next_full_pass_start(600.0), 30.0, "原函数：最早空缺")
        self.assertEqual(c.resume_point(600.0, position=320.0), 340.0,
                         "应接着播放位置所在段往后，而不是回头补 30s")

    def test_resume_falls_back_when_playhead_uncovered(self):
        c = _ctl(lambda p: (1, 1.0), _Clock())
        _line(c, 0, 30, "a")
        self.assertEqual(c.resume_point(600.0, position=200.0), 30.0)

    def test_resume_at_end_returns_minus_one(self):
        c = _ctl(lambda p: (1, 1.0), _Clock())
        _line(c, 0, 599.5, "a")
        self.assertEqual(c.resume_point(600.0, position=100.0), -1.0)


class DeferredSeekTests(unittest.TestCase):
    """引擎任务在跑时拖进度条：旧逻辑直接忽略这次拖动（防降噪死循环），且没人
    记住它——任务结束后字幕继续补开头，永远到不了拖到的位置（真机端到端实测：
    拖到 144s 后 90 秒内字幕一直停在 38~54s）。"""

    def setUp(self):
        self.c = _ctl(lambda p: (1, 1.0), _Clock())
        _line(self.c, 0, 30)

    def _user_seek(self, pos):
        self.c.note_user_seek(pos)
        return self.c.handle_position(pos, audio_mode=True)

    def test_seek_during_task_is_remembered_not_restarted(self):
        self.assertTrue(self.c.task_running)
        self.c.last_position = 31.0
        self.assertEqual(self._user_seek(144.0), "normal",
                         "任务在跑时不能顶掉它（防降噪死循环）")
        self.assertEqual(self.c.pending_seek, 144.0)

    def test_playback_jump_does_not_overwrite_user_seek(self):
        """读到文件末尾时 mpv 位置跳到片尾（真机实测 144→240）：不是用户操作，
        不能把拖到的 144s 覆盖成 240s。"""
        self.c.last_position = 31.0
        self._user_seek(144.0)
        self.c.handle_position(240.0, audio_mode=True)     # 播放器自己跳的
        self.assertEqual(self.c.pending_seek, 144.0)

    def test_resume_point_prefers_pending_seek_then_clears(self):
        self.c.last_position = 31.0
        self._user_seek(144.0)
        self.c.after_task(self.c.generation)
        # 与普通 seek 任务一致：从拖到位置前 5 秒起，避免起点切在一句话中间
        self.assertEqual(self.c.resume_point(240.0, position=240.0), 139.0,
                         "任务结束后应先去识别拖到的位置")
        self.assertIsNone(self.c.pending_seek, "用掉后应清掉，不重复跳")

    def test_pending_seek_inside_covered_range_ignored(self):
        self.c.last_position = 100.0
        self._user_seek(10.0)                               # 拖回已识别的地方
        self.assertIsNone(self.c.pending_seek)

    def test_latest_seek_wins(self):
        self.c.last_position = 31.0
        self._user_seek(100.0)
        self._user_seek(180.0)
        self.assertEqual(self.c.pending_seek, 180.0)

    def test_follows_seek_island_after_reaching_it(self):
        """真机实测：去到拖到的位置识别了一段（159~168s）后，下一个任务又跳回
        开头 9s 去补空缺——那里正在等下载，字幕在拖到的位置停住。
        规则：最近一次拖到的那段还没识别到片尾/下载前沿时，接着它往后识别。"""
        self.c.last_position = 31.0
        self._user_seek(144.0)
        self.c.after_task(self.c.generation)
        start = self.c.resume_point(240.0, position=240.0)    # 139
        self.c.begin_full_pass(self.c.generation + 1, start)
        _line(self.c, 159, 168, "island")                     # 拖到的那段识别出一些
        self.c.after_task(self.c.generation)
        self.assertEqual(self.c.resume_point(240.0, position=240.0), 168.0,
                         "应接着拖到的那段往后，而不是跳回开头补空缺")

    def test_seek_task_to_hole_keeps_focus_on_seek_point(self):
        """真机实测：拖到 144s 时引擎正好空闲，直接派了 seek 任务（139s 起）。
        那里还没下载，任务 0 产出就收尾；之后的补洞又按"最早空缺"回到开头 41s，
        再也没人去 144s——字幕停在前面。拖到的位置必须一直是关注点。"""
        c = _ctl(lambda p: (1, 1.0), _Clock())
        _line(c, 0, 41, "head")
        c.after_task(c.generation)                           # 开头那段做完、引擎空闲
        c.last_position = 16.0
        c.note_user_seek(144.0)
        self.assertEqual(c.handle_position(144.0, audio_mode=True), "restart")
        c.begin_media(Path("D:/dl/a.mp4"), 139.0, 9, catching=True, duration=240)  # seek 任务
        c.after_task(c.generation)                           # 0 产出：那里还在下载
        self.assertEqual(c.resume_point(240.0, position=240.0), 139.0,
                         "seek 任务在空洞里 0 产出后，应继续盯着拖到的位置")

    def test_focus_segment_reaching_silent_tail_falls_back_to_gap(self):
        """真机实测：拖到的那段一路识别到 237s（片尾 3 秒没人声）后，每次从 237s
        起的任务都 0 产出，调度判 idle 停下——41~139s 的空缺（那时正在下载）再也
        没人补。关注段到片尾无产出时，应转去补最早的空缺，而不是停。"""
        c = _ctl(lambda p: (1, 1.0), _Clock(), duration=240)
        _line(c, 0, 41, "head")
        c.after_task(c.generation)
        c.last_position = 16.0
        c.note_user_seek(144.0)
        c.begin_media(Path("D:/dl/a.mp4"), 139.0, 9, catching=True, duration=240)
        _line(c, 159, 237, "island")
        c.after_task(c.generation)
        start = c.resume_point(240.0, position=240.0)
        self.assertEqual(start, 237.0)                         # 先接着关注段
        c.begin_full_pass(c.generation + 1, start)
        action, _ = c.after_task(c.generation)                 # 片尾无人声：0 产出
        self.assertEqual(action, "full_pass", "关注段到头后不该停，要去补前面的空缺")
        self.assertEqual(c.resume_point(240.0, position=240.0), 41.0)

    def test_new_media_clears_pending_seek(self):
        self.c.last_position = 31.0
        self._user_seek(144.0)
        self.c.begin_media(Path("D:/dl/b.mp4"), 0.0, 9, catching=False, duration=600)
        self.assertIsNone(self.c.pending_seek)


class ViewerWiringTests(unittest.TestCase):
    """直调 Viewer 的事件处理（替身对象），确认每种决策落到正确的动作上。"""

    def _run(self, action, delay=0):
        from unittest import mock

        from app.live_engine_state import EngineEvent, EngineEventData
        from app.viewer import Viewer

        stub = mock.MagicMock()
        stub._live_no_audio = False
        stub._live_ctl.after_task.return_value = (action, delay)
        Viewer._handle_live_engine_event(stub, EngineEventData(EngineEvent.TASK_DONE, generation=5))
        stub._live_ctl.after_task.assert_called_once_with(5)
        return stub

    def test_wait_starts_retry_timer_with_delay(self):
        stub = self._run("wait", 3000)
        stub._live_grow_timer.start.assert_called_once_with(3000)
        stub._start_live_full_pass.assert_not_called()

    def test_full_pass_dispatches_immediately(self):
        stub = self._run("full_pass")
        stub._start_live_full_pass.assert_called_once()
        stub._live_grow_timer.start.assert_not_called()

    def test_idle_does_nothing(self):
        stub = self._run("idle")
        stub._start_live_full_pass.assert_not_called()
        stub._live_grow_timer.start.assert_not_called()
        stub._prefetch_next_live_media.assert_not_called()

    def test_retry_timer_skips_when_new_task_running_or_media_changed(self):
        from unittest import mock

        from app.viewer import Viewer

        for running, same_media, expect in ((False, True, True), (True, True, False),
                                            (False, False, False)):
            with self.subTest(running=running, same_media=same_media):
                stub = mock.MagicMock()
                stub._live_on, stub._live_paused = True, False
                stub._live_ctl.task_running = running
                p = Path("D:/dl/a.mp4")
                stub.items = [mock.MagicMock(path=p)]
                stub.index = 0
                stub._live_ctl.media_path = p if same_media else Path("D:/dl/b.mp4")
                Viewer._on_live_grow_timer(stub)
                self.assertEqual(stub._start_live_full_pass.called, expect)


if __name__ == "__main__":
    unittest.main()
