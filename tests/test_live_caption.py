import unittest

from PySide6.QtCore import QCoreApplication

from app.caption_text import (
    apply_glossary,
    deduplicate_rows,
    format_bilingual,
    merge_short_rows,
)
from pathlib import Path

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

    def test_display_ranges_jump_and_backfill(self) -> None:
        """用户场景：0-1min 转写 → 跳 2min 续转 → 补洞连成一片。"""
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=False)
        ctl.accept_line({"g": 1, "t": 0, "end": 60, "text": "a", "zh": ""})
        self.assertEqual(ctl.display_ranges(), [(0.0, 60.0)])

        # 跳到 2 分钟：新任务区间，1-2 分钟空洞如实保留
        ctl.begin_media(Path("a.mp4"), 120.0, 2, catching=True)
        ctl.accept_line({"g": 2, "t": 120, "end": 240, "text": "b", "zh": ""})
        self.assertEqual(ctl.display_ranges(), [(0.0, 60.0), (120.0, 240.0)])

        # 回头补 60-120：三段相接连成一片
        ctl.begin_full_pass(3, 60.0)
        ctl.accept_line({"g": 3, "t": 60, "end": 90, "text": "c", "zh": ""})
        ctl.accept_line({"g": 3, "t": 90, "end": 120, "text": "d", "zh": ""})
        self.assertEqual(ctl.display_ranges(), [(0.0, 240.0)])

    def test_translation_backfill_updates_row_in_place(self) -> None:
        """异步翻译：原文行先到（zh 空），译文更新行原地补齐、不产生重复。"""
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=False)
        self.assertTrue(ctl.accept_line(
            {"g": 1, "t": 0, "end": 5, "text": "Hello.", "zh": ""}))
        self.assertEqual(ctl.rows[0][3], "")
        # 译文更新行：同 (t0,t1,seg)，zh 有值
        self.assertTrue(ctl.accept_line(
            {"g": 1, "t": 0, "end": 5, "text": "Hello.", "zh": "你好。"}))
        self.assertEqual(len(ctl.rows), 1)
        self.assertEqual(ctl.rows[0][3], "你好。")
        # 相同的更新行再来一次（重复投递）：拒绝且不产生重复
        self.assertFalse(ctl.accept_line(
            {"g": 1, "t": 0, "end": 5, "text": "Hello.", "zh": "你好。"}))
        self.assertEqual(len(ctl.rows), 1)

    def test_translation_backfill_out_of_order_and_reindex(self) -> None:
        """多条行的乱序回填 + 复用 (t0,t1,seg) 索引：O(1) 定位且互不串行。"""
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=False)
        for i, text in enumerate(("One.", "Two.", "Three.")):
            self.assertTrue(ctl.accept_line(
                {"g": 1, "t": i * 10, "end": i * 10 + 4, "text": text, "zh": ""}))
        self.assertEqual(len(ctl.rows), 3)
        # 乱序补译文：中间行与最后一行
        self.assertTrue(ctl.accept_line(
            {"g": 1, "t": 10, "end": 14, "text": "Two.", "zh": "二。"}))
        self.assertTrue(ctl.accept_line(
            {"g": 1, "t": 20, "end": 24, "text": "Three.", "zh": "三。"}))
        self.assertEqual([r[3] for r in ctl.rows], ["", "二。", "三。"])
        # 时间戳不匹配 → 新增行而非误更新
        self.assertTrue(ctl.accept_line(
            {"g": 1, "t": 40, "end": 44, "text": "Four.", "zh": ""}))
        self.assertEqual(len(ctl.rows), 4)
        # 译文二次更新（重译覆盖）：仍原地更新
        self.assertTrue(ctl.accept_line(
            {"g": 1, "t": 10, "end": 14, "text": "Two.", "zh": "第二句。"}))
        self.assertEqual(ctl.rows[1][3], "第二句。")
        self.assertEqual(len(ctl.rows), 4)

    def test_live_captions_suppress_and_restore_file_subtitles(self) -> None:
        """实时字幕开启时隐藏 mpv 文件字幕，停止后恢复原可见状态。"""
        import os

        from app.runtime import VENDOR_DIR

        os.environ["PATH"] = str(VENDOR_DIR) + os.pathsep + os.environ.get("PATH", "")
        from app.viewer import Viewer

        class Video:
            def __init__(self):
                self.visible = True

            @property
            def sub_visible(self):
                return self.visible

            def set_file_subtitle_visible(self, visible):
                self.visible = bool(visible)

        class Controls:
            def __init__(self):
                self.visible = True

            def set_sub_visible(self, visible):
                self.visible = bool(visible)

        video = Video()
        controls = Controls()
        viewer = Viewer.__new__(Viewer)
        viewer.video_view = video
        viewer.controls = controls
        viewer._live_saved_sub_visible = None

        Viewer._suppress_file_subtitles_for_live(viewer)
        self.assertFalse(video.visible)
        self.assertFalse(controls.visible)
        self.assertTrue(viewer._live_saved_sub_visible)

        # 换片后 mpv 重新加载字幕/track-list 变化，会再次调用压制；
        # 此时不能把已保存的 True 覆盖成当前的 False。
        Viewer._suppress_file_subtitles_for_live(viewer)
        self.assertTrue(viewer._live_saved_sub_visible)

        Viewer._restore_file_subtitles_after_live(viewer)
        self.assertTrue(video.visible)
        self.assertTrue(controls.visible)
        self.assertIsNone(viewer._live_saved_sub_visible)


    def test_silence_gap_inside_span_does_not_restart(self) -> None:
        """青色区间（任务起点→前沿）内的 VAD 静音间隙不应触发重转。"""
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=False)
        # 同一任务两行，中间 100 秒无语音（前沿已推进到 140）
        ctl.accept_line({"g": 1, "t": 30, "end": 35, "text": "A.", "zh": ""})
        ctl.accept_line({"g": 1, "t": 135, "end": 140, "text": "B.", "zh": ""})
        # 行级未覆盖，但任务区间已覆盖 → covered，不请求重转
        self.assertTrue(ctl.span_covered(80.0))
        self.assertFalse(ctl.is_covered(80.0))
        self.assertEqual(ctl.handle_position(80.0, audio_mode=True), "covered")
        # 超出前沿且任务已收尾（task_running=False）才追赶：
        # 引擎任务在途时严禁追赶重启（降噪期无行产出，旧逻辑 8 秒顶掉
        # 在途任务 → 从头降噪 → 再被顶，死循环风暴，实测 Nyles 6+ 轮）
        self.assertFalse(ctl.span_covered(200.0))
        ctl.task_running = False
        self.assertEqual(ctl.handle_position(200.0, audio_mode=True), "restart")
        # 任务在途：同一位置不再 restart
        ctl.task_running = True
        ctl.last_position = 100.0  # 制造跳变（handle_position 需要 pos 变化 >2s）
        self.assertEqual(ctl.handle_position(200.0, audio_mode=True), "normal")


class LangRewriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_parse_lang_rewrite_event(self):
        """LANG_REWRITE 状态行解析：detail = "<lang>;<a>-<b>;..."。"""
        event = parse_engine_line("# LANG_REWRITE ja;45.2-120.0;300.5-360.0")
        self.assertIsNotNone(event)
        self.assertEqual(event.event, EngineEvent.LANG_REWRITE)
        self.assertEqual(event.detail, "ja;45.2-120.0;300.5-360.0")

    def test_rewrite_rows_drops_hit_ranges(self):
        """rewrite_rows：清与区间相交的行；区间外的行保留；新行可覆盖。"""
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=False)
        # 嘈杂区误判行（将被 0-60 区间清掉）
        ctl.accept_line({"g": 1, "t": 10, "end": 12, "text": "嗯。", "zh": ""})
        ctl.accept_line({"g": 1, "t": 30, "end": 32, "text": " itu sih.", "zh": ""})
        # 真实中文长句（区间外，保留——真实多语言不该被误伤）
        ctl.accept_line({"g": 1, "t": 65, "end": 70, "text": "我们下周去东京吧。", "zh": ""})
        # 中段另一个误判小区间
        ctl.accept_line({"g": 1, "t": 200, "end": 205, "text": "거기서는.", "zh": ""})
        removed = ctl.rewrite_rows([(0.0, 60.0), (198.0, 210.0)])
        self.assertEqual(removed, 3)
        self.assertEqual(len(ctl.rows), 1)
        self.assertEqual(ctl.rows[0][2], "我们下周去东京吧。")
        # task_spans 不动（覆盖前沿保持，重转行以同代次重新推进）
        self.assertEqual(ctl.task_spans[1][1], 205.0)
        # 重转的新行以相同时间戳重新覆盖
        self.assertTrue(ctl.accept_line(
            {"g": 1, "t": 10, "end": 12, "text": "グレーして。", "zh": ""}))
        self.assertEqual(len(ctl.rows), 2)
        # 译文后补仍按 (t,end,seg) 定位
        self.assertTrue(ctl.accept_line(
            {"g": 1, "t": 65, "end": 70, "text": "我们下周去东京吧。", "zh": "要去。"}))
        zh_row = next(r for r in ctl.rows if r[0] == 65.0)
        self.assertEqual(zh_row[3], "要去。")

    def test_rewrite_rows_bad_ranges_is_noop(self):
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=False)
        ctl.accept_line({"g": 1, "t": 100, "end": 110, "text": "later", "zh": ""})
        self.assertEqual(ctl.rewrite_rows([]), 0)
        self.assertEqual(ctl.rewrite_rows([(50.0, 40.0)]), 0)  # 空/倒置区间
        self.assertEqual(len(ctl.rows), 1)


class TaskDoneProgressionTests(unittest.TestCase):
    """追赶/补洞任务的完成判定：按"是否真的覆盖全片"而不是任务起点。

    回归：③.mp4（3646s）追赶任务（seek=0）转完 900s 解码窗口就 TASK_DONE，
    旧逻辑因 task_start_seek≤0.5 宣称 full_pass_done 并去预转写下一集——
    长片尾部永远空白（"写一半去写下一个"）。新逻辑：行覆盖前沿 < 媒体
    时长 → needs_full_pass（继续补洞）；达到时长 → done（才 prefetch）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_catchup_window_done_still_needs_full_pass(self):
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=True, duration=3646.0)
        ctl.accept_line({"g": 1, "t": 0, "end": 676, "text": "追赶窗口", "zh": ""})
        self.assertEqual(ctl.task_done(1), "needs_full_pass")

    def test_full_pass_window_done_still_needs_more(self):
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=True, duration=3646.0)
        ctl.begin_full_pass(2, seek=676.0)
        ctl.accept_line({"g": 2, "t": 676, "end": 1049, "text": "补洞窗口", "zh": ""})
        self.assertEqual(ctl.task_done(2), "needs_full_pass")

    def test_done_only_when_front_reaches_duration(self):
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=True, duration=1200.0)
        ctl.accept_line({"g": 1, "t": 0, "end": 900, "text": "第一窗", "zh": ""})
        ctl.begin_full_pass(2, seek=900.0)
        ctl.accept_line({"g": 2, "t": 900, "end": 1199.5, "text": "补到尾", "zh": ""})
        self.assertEqual(ctl.task_done(2), "done")
        self.assertTrue(ctl.full_pass_done)

    def test_unknown_duration_never_claims_done(self):
        """无时长信息（duration=None）时保守：继续补洞而非 prefetch。"""
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=True, duration=None)
        ctl.accept_line({"g": 1, "t": 0, "end": 900, "text": "x", "zh": ""})
        self.assertEqual(ctl.task_done(1), "needs_full_pass")

    def test_begin_media_new_file_clears_old_spans(self):
        """换片清空旧任务区间：旧片的 span 残留会混进新片的覆盖显示。"""
        ctl = LiveCaptionController()
        ctl.begin_media(Path("a.mp4"), 0.0, 1, catching=True, duration=100.0)
        ctl.accept_line({"g": 1, "t": 0, "end": 100, "text": "旧片", "zh": ""})
        ctl.begin_media(Path("b.mp4"), 0.0, 2, catching=True, duration=200.0)
        self.assertEqual(ctl.display_ranges(), [])
        self.assertFalse(ctl.span_covered(50.0))


if __name__ == "__main__":
    unittest.main()
