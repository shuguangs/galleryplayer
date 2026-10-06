"""Scheduling state for live captions, kept separate from viewer UI."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

# 边下边播：引擎读到下载前沿就收尾，文件还在变时延后再派任务接着识别。
GROW_WAIT_MIN_MS = 3000      # 首次等待
GROW_WAIT_MAX_MS = 15000     # 连续无新产出时逐步拉长到这里
GROW_STALE_S = 120.0         # 文件超过这么久没变：不再等待（下载停了/完成了）


def _default_stat(path) -> tuple[int, float]:
    st = Path(path).stat()       # 调用方可能传 str
    return st.st_size, st.st_mtime


class LiveCaptionController(QObject):
    """Own caption rows and seek decisions for one viewer session.

    The viewer remains responsible for engine processes and rendering. This
    class deliberately has no label or mpv dependency, keeping scheduling
    decisions unit-testable.
    """

    rows_changed = Signal()
    restart_requested = Signal(float, bool)

    def __init__(self, parent: QObject | None = None, stat_fn=None, clock=None) -> None:
        super().__init__(parent)
        # 可注入：测试里用假的文件状态和时钟
        self._stat_fn = stat_fn or _default_stat
        self._clock = clock or time.monotonic
        self._task_file_state: tuple[int, float] | None = None
        self._task_rows_at_start = 0
        self._file_changed_at = self._clock()
        self._grow_wait_ms = GROW_WAIT_MIN_MS
        self._grow_waited = False
        # 任务在跑时用户拖到的位置（见 handle_position），收尾后优先识别那里
        self.pending_seek: float | None = None
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
        self.task_running = False
        self._media_duration: float | None = None
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
        self.task_running = False
        self.task_start_seek = 0.0
        self.last_position = None
        self._pending_restart = None
        self.restart_timer.stop()
        self.rows_changed.emit()
        return item_is_video

    def begin_media(self, media: Path, seek: float, generation: int,
                    catching: bool, duration: float | None = None) -> None:
        if self.media_path != media:
            self.rows = []
            self._row_keys = set()
            self._row_index = {}
            self.data_version += 1
            self.rows_changed.emit()
            self.task_spans.clear()
            self._media_duration = duration
        elif duration is not None:
            self._media_duration = duration
        self.media_path = media
        self.generation = generation
        # 新任务本身就是从拖到的位置（或新片）起：之前记下的拖动已兑现/作废
        self.pending_seek = None
        start = max(0.0, float(seek))
        self.task_spans[generation] = [start, start]
        self.task_start_seek = start
        self.full_pass_running = self.task_start_seek <= 0.5
        self.full_pass_done = False
        self.catching = catching
        self.task_running = True   # 引擎任务在途（降噪/转写中）
        self.last_position = seek
        self._last_submit_at = time.time()
        self._last_submit_seek = seek
        self._grow_wait_ms = GROW_WAIT_MIN_MS
        self._snapshot_task_start(reset_change_clock=True)

    def _file_state(self) -> tuple[int, float] | None:
        if self.media_path is None:
            return None
        try:
            return self._stat_fn(self.media_path)
        except OSError:
            return None

    def _snapshot_task_start(self, reset_change_clock: bool = False) -> None:
        """记下任务派出时的文件状态和行数，任务收尾时据此判断"还在下载吗"
        与"这次有没有新产出"。"""
        self._task_file_state = self._file_state()
        self._task_rows_at_start = len(self.rows)
        if reset_change_clock:
            self._file_changed_at = self._clock()

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

    def rewrite_rows(self, ranges: list[tuple[float, float]]) -> int:
        """延迟探测改判：丢弃落在给定区间内的行，引擎正逐区间重转。

        行与区间相交即清（重转的新行时间戳会重新覆盖）。task_spans 不动：
        任务覆盖前沿仍在（重转行到达时以相同代次推进，span_covered 的
        补洞判定不受影响）。返回删除的行数。
        """
        spans = [(max(0.0, float(a)), max(0.0, float(a), float(b)))
                 for a, b in ranges if float(b) > float(a)]
        if not spans:
            return 0

        def _hit(row) -> bool:
            r0, r1 = row[0], row[1]
            return any(a <= r1 and r0 <= b for a, b in spans)

        old = self.rows
        self.rows = [r for r in old if not _hit(r)]
        removed = len(old) - len(self.rows)
        if removed:
            self._row_keys = {
                (round(r[0], 2), round(r[1], 2), r[2], r[3]) for r in self.rows
            }
            self._row_index = {
                (round(r[0], 2), round(r[1], 2), r[2]): i
                for i, r in enumerate(self.rows)
            }
            self.data_version += 1
        self.rows_changed.emit()
        return removed

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
        # 引擎任务在途（降噪/转写中）：不当追赶失败——降噪期间无行产出，
        # 前沿恒 0，旧逻辑 8 秒即顶掉在途任务又从头降噪，死循环风暴。
        # 但要记住这次拖动：任务收尾后先去识别拖到的位置（边下边播时每个
        # 任务都要等下载，旧版这里直接丢掉拖动，字幕永远到不了拖到的地方）
        if self.task_running:
            return "normal"

        self.request_restart(pos)
        return "restart"

    def note_user_seek(self, pos: float) -> None:
        """用户主动拖动/跳转（进度条、方向键）。

        引擎任务在跑时，handle_position 不会顶掉它（防降噪死循环），这里记下
        位置，任务收尾后先去识别那里（边下边播时每个任务都要等下载，旧版直接
        丢掉这次拖动，字幕永远到不了拖到的地方）。
        只认用户主动操作：播放自然前进、读到文件末尾时 mpv 的位置跳变不算——
        曾把"拖到 144s"覆盖成 EOF 的 240s，任务跑去全是空洞的片尾。
        """
        if self.span_covered(pos):
            return
        # 关注点：无论引擎此刻忙不忙都记下。拖到处还没下载时 seek 任务会 0 产出
        # 收尾，之后的补洞必须继续盯着这里，而不是回头去补最早的空缺
        self._focus = pos
        if self.task_running:
            self.pending_seek = pos

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
        self.task_running = False  # 引擎任务收尾（追赶检测恢复判定资格）
        self.full_pass_running = False
        # 是否已覆盖全片：行级覆盖的前沿 ≥ 媒体时长（无时长信息时保守
        # 认为没完）。追赶任务（seek≤0.5）只转了一个解码窗口（~900s）
        # 就 done——旧逻辑在此宣称 full_pass_done 并去预转写下一集，
        # 长片从此再无补洞，尾部永远空白（实测 ③.mp4 3646s 只转到
        # 676s 就"写一半去写下一个"）。
        ranges = self.caption_ranges()
        front = max((r[1] for r in ranges), default=0.0)
        if self._media_duration is not None and front >= self._media_duration - 2.0:
            self.full_pass_done = True
            return "done"
        return "needs_full_pass"

    def begin_full_pass(self, generation: int, seek: float = 0.0) -> None:
        self.generation = generation
        start = max(0.0, float(seek))
        self.task_spans[generation] = [start, start]
        self.task_start_seek = start
        self.full_pass_running = True
        self.catching = False
        self.task_running = True
        self._snapshot_task_start()

    def after_task(self, generation: int) -> tuple[str, int]:
        """任务收尾后的下一步：(动作, 延迟毫秒)。

        - "done"：已覆盖到片尾；
        - "wait"：文件还在下载（大小或修改时间变了），延迟后再派任务接着识别；
        - "full_pass"：文件没变、本任务有新产出 → 立即补洞（原行为）；
        - "idle"：文件没变且本任务 0 产出，或文件很久没变了 → 停止重派
          （没有人声的片子旧版会连续空转重派几十次）；
        - "ignored"：过期任务。
        """
        result = self.task_done(generation)
        if result == "ignored":
            return "ignored", 0
        if result == "done":
            return "done", 0
        now = self._clock()
        state = self._file_state()
        if state is None:
            return "idle", 0
        changed = self._task_file_state is not None and state != self._task_file_state
        produced = len(self.rows) > self._task_rows_at_start
        if changed:
            self._file_changed_at = now
            if produced:
                self._grow_wait_ms = GROW_WAIT_MIN_MS
            else:
                self._grow_wait_ms = min(GROW_WAIT_MAX_MS, self._grow_wait_ms * 2) \
                    if getattr(self, "_grow_waited", False) else GROW_WAIT_MIN_MS
            self._grow_waited = True
            return "wait", self._grow_wait_ms
        if now - self._file_changed_at > GROW_STALE_S:
            return "idle", 0
        if produced:
            return "full_pass", 0
        # 0 产出：若这次是"接着关注段往后"的任务，说明关注段已经到头（片尾没人声
        # 或到下载前沿）。放下关注点，回头补前面的空缺——真机实测旧逻辑在这里
        # 停下，拖动前那段（41~139s，当时还在下载）再也没人补
        focus = getattr(self, "_focus", None)
        if focus is not None and self.span_covered(focus):
            # 只有关注点那里已经识别出过内容、这次是"接着往后"才算到头；关注点
            # 那里还一句都没有时，是还在下载，保持关注（见下一次 wait/重试）
            self._focus = None
            if self.next_full_pass_start(self._media_duration) >= 0:
                return "full_pass", 0
        return "idle", 0

    def resume_point(self, duration: float | None, position: float | None) -> float:
        """下一个补洞任务的起点。

        边下边播且拖过进度条时，下载器从拖到的位置往后下；字幕应接着"播放
        位置所在的那一段"往后识别，而不是回头去补最早的空缺（那里多半还没
        下载）。播放位置不在任何已识别段里时退回 next_full_pass_start。
        返回 -1 表示已覆盖到片尾。
        """
        seek, self.pending_seek = self.pending_seek, None
        if seek is not None and not self.span_covered(seek):
            self._focus = seek
            return max(0.0, seek - 5.0)   # 与 seek 任务一致：前 5 秒起，避免切在句中
        # 关注点：最近一次拖到的位置，没有则用播放位置。接着"关注点所在的那段"
        # 往后识别，直到片尾；到了片尾才回头补前面的空缺。（真机实测：去到拖到
        # 的位置识别了一段后，旧逻辑又跳回开头补空缺，字幕在拖到处停住）
        # 用任务覆盖区间（display_ranges，含句间静音）而不是逐句区间：拖到的
        # 位置常落在第一句话之前的静音里（实测拖到 144s，第一句在 159s）
        focus = getattr(self, "_focus", None)
        for anchor in (focus, position):
            if anchor is None:
                continue
            for start, end in self.display_ranges():
                if start - 6.0 <= anchor <= end + 1.0:
                    if duration is not None and end >= max(0.0, duration - 1.0):
                        break
                    return end
        # 关注点那里还一句都没识别出来（还在下载）：就从关注点本身再试，
        # 而不是回头补最早的空缺
        if focus is not None and not self.span_covered(focus):
            if duration is None or focus < duration - 1.0:
                return max(0.0, focus - 5.0)
        self._focus = None
        return self.next_full_pass_start(duration)
