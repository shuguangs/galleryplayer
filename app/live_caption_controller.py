"""Scheduling state for live captions, kept separate from viewer UI."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


class LiveCaptionController(QObject):
    """Own caption rows and seek decisions for one viewer session.

    The viewer remains responsible for engine processes and rendering. This
    class deliberately has no label or mpv dependency, keeping scheduling
    decisions unit-testable.
    """

    rows_changed = Signal()
    restart_requested = Signal(float, bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.rows: list[tuple[float, float, str, str]] = []
        self._row_keys: set[tuple[float, float, str, str]] = set()
        # (t0, t1, 原文) → rows 下标：译文更新行 O(1) 定位（全片 ~2000 行，
        # 逐行线性扫是 O(n²)）；rows 只追加不删除，下标稳定
        self._row_index: dict[tuple[float, float, str], int] = {}
        # 每个任务（generation）的转写覆盖 [起点, 前沿]，供 seekbar 显示：
        # 跳转后旧任务的区间保留、新任务从跳转点另行延伸，空洞如实显示，
        # 回头补洞的任务再补上空缺（UI 语义，与补转调度解耦）
        self.task_spans: dict[int, list[float]] = {}
        self.generation = 0
        # 内容版本号：每次行增删/译文原地更新都递增。自动存盘的脏检查用
        # 它而不是行数——译文回填不改变行数，按行数比较会漏存译文
        self.data_version = 0
        self.catching = False
        self.media_path: Path | None = None
        self.task_start_seek = 0.0
        self.full_pass_running = False
        self.full_pass_done = False
        self.last_position: float | None = None
        self._last_restart_pos = -10_000.0
        self._last_submit_at = 0.0
        self._last_submit_seek = -10_000.0
        self._pending_restart: float | None = None
        self._pending_catching = True

        self.restart_timer = QTimer(self)
        self.restart_timer.setSingleShot(True)
        self.restart_timer.setInterval(1200)
        self.restart_timer.timeout.connect(self._submit_pending_restart)

    def reset_for_media(self, item_is_video: bool) -> bool:
        self.rows = []
        self._row_keys = set()
        self._row_index = {}
        self.data_version += 1
        self.task_spans.clear()
        self.media_path = None
        self.full_pass_running = False
        self.full_pass_done = False
        self.task_start_seek = 0.0
        self.last_position = None
        self._pending_restart = None
        self.restart_timer.stop()
        self.rows_changed.emit()
        return item_is_video

    def begin_media(self, media: Path, seek: float, generation: int,
                    catching: bool) -> None:
        if self.media_path != media:
            self.rows = []
            self._row_keys = set()
            self._row_index = {}
            self.data_version += 1
            self.rows_changed.emit()
        self.media_path = media
        self.generation = generation
        start = max(0.0, float(seek))
        self.task_spans[generation] = [start, start]
        self.task_start_seek = start
        self.full_pass_running = self.task_start_seek <= 0.5
        self.full_pass_done = False
        self.catching = catching
        self.last_position = seek
        self._last_submit_at = time.time()
        self._last_submit_seek = seek

    def accept_line(self, obj: dict) -> bool:
        if int(obj.get("g", -1)) != self.generation:
            return False
        t0 = float(obj.get("t", 0))
        t1 = max(t0, float(obj.get("end", t0)))
        seg = str(obj.get("text", "")).strip()
        zh = str(obj.get("zh", "")).strip()
        if not seg or t1 <= t0:
            return False
        # 翻译异步化：引擎先发原文行（zh 空），译文就绪后发同 (t0,t1,seg)
        # 的更新行 → 原地补译文，不产生重复段
        rt0, rt1 = round(t0, 2), round(t1, 2)
        idx = self._row_index.get((rt0, rt1, seg))
        if idx is not None:
            r0, r1, rseg, rzh = self.rows[idx]
            if zh == rzh:
                return False  # 完全相同的重复行
            if zh:  # 译文后补：原地更新
                self.rows[idx] = (r0, r1, rseg, zh)
                self._row_keys.discard((rt0, rt1, seg, rzh))
                self._row_keys.add((rt0, rt1, seg, zh))
                self.data_version += 1
                self.rows_changed.emit()
                return True
            return False  # 重复的原文行
        key = (rt0, rt1, seg, zh)
        if key in self._row_keys:
            return False
        self._row_keys.add(key)
        self._row_index[(rt0, rt1, seg)] = len(self.rows)
        self.data_version += 1
        self.rows.append((t0, t1, seg, zh))
        span = self.task_spans.get(self.generation)
        if span is not None:
            span[1] = max(span[1], t1)
        self.rows_changed.emit()
        return True

    def is_covered(self, pos: float) -> bool:
        return any(start - 0.2 <= pos <= end + 0.3
                   for start, end, _seg, _zh in self.rows)

    def span_covered(self, pos: float) -> bool:
        """pos 是否落在某个任务的 [起点, 前沿] 内（进度条青色区间的语义）。

        行级 is_covered 会被 VAD 静音间隙误判：区间已推进到前沿，中间无语音
        的位置本就没有行，不应触发重转（曾致青色已覆盖区反复弹"追赶中"）。
        """
        return any(start - 0.2 <= pos <= end + 0.3
                   for start, end in self.display_ranges())

    def display_ranges(self) -> list[tuple[float, float]]:
        """seekbar 显示用：每个任务的真实覆盖 [起点, 前沿]，合并相邻段。

        与 caption_ranges()（逐句精确，供补洞决策）不同：任务内部用前沿
        平滑（转写顺序推进，前沿前的语音都已处理），任务之间空洞如实保留。
        """
        spans = sorted(
            (values[0], max(values[0], values[1]))
            for values in self.task_spans.values()
        )
        merged: list[list[float]] = []
        for start, end in spans:
            if end <= start:
                continue
            if merged and start <= merged[-1][1] + 0.5:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(start, end) for start, end in merged]

    def caption_ranges(self) -> list[tuple[float, float]]:
        merged: list[list[float]] = []
        for start, end, _seg, _zh in sorted(self.rows):
            if merged and start <= merged[-1][1] + 0.5:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(start, end) for start, end in merged]

    def next_full_pass_start(self, duration: float | None = None) -> float:
        """Choose the earliest uncovered timestamp for a background pass."""
        ranges = self.caption_ranges()
        if not ranges:
            return 0.0
        if ranges[0][0] > 0.5:
            return 0.0
        for index in range(len(ranges) - 1):
            if ranges[index + 1][0] - ranges[index][1] > 1.0:
                return ranges[index][1]
        tail_start = ranges[-1][1]
        if duration is not None and tail_start >= max(0.0, duration - 1.0):
            return -1.0
        return tail_start

    def handle_position(self, pos: float, audio_mode: bool) -> str:
        """Return one of covered, normal, or restart."""
        prev = self.last_position
        self.last_position = pos
        if not audio_mode:
            return "normal"
        if prev is None or abs(pos - prev) < 2:
            return "normal"
        if abs(pos - self._last_restart_pos) < 2:
            return "normal"
        if self.span_covered(pos):
            self.restart_timer.stop()
            self._pending_restart = None
            self.catching = False
            return "covered"

        self.request_restart(pos)
        return "restart"

    def request_restart(self, pos: float, catching: bool = True) -> None:
        if (time.time() - self._last_submit_at < 12.0
                and abs(pos - self._last_submit_seek) < 2.0):
            return
        if self._pending_restart == pos:
            return
        self._pending_restart = pos
        self._pending_catching = catching
        self.restart_timer.start()

    def _submit_pending_restart(self) -> None:
        pos = self._pending_restart
        if pos is None:
            return
        self._pending_restart = None
        self._last_restart_pos = pos
        self.restart_requested.emit(pos, self._pending_catching)

    def task_done(self, generation: int) -> str:
        if generation != self.generation:
            return "ignored"
        if self.full_pass_running or self.task_start_seek <= 0.5:
            self.full_pass_done = True
            self.full_pass_running = False
            return "done"
        self.full_pass_running = False
        return "needs_full_pass"

    def begin_full_pass(self, generation: int, seek: float = 0.0) -> None:
        self.generation = generation
        start = max(0.0, float(seek))
        self.task_spans[generation] = [start, start]
        self.task_start_seek = start
        self.full_pass_running = True
        self.catching = False
