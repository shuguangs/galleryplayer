"""缩略图网格（-thumbnail sheet）生成器。

把一个视频抽 R×C 帧，拼成一张网格大图（先行后列）。

抓帧位置由 `build_plans()` 算出——它是纯函数（只吃 duration 和参数，
不碰 mpv），所以每种模式的时间点数学都能单测。抓帧侧
（`extract_frames_at`）不认识"模式"，只按给定时间点取帧。

七种模式（可多选，一次运行每种各出一张图）：
- even      均匀分布：dur/(n+1)*i，避开片头黑帧，覆盖全片（旧行为，默认）
- trim      跳过片头片尾：在 [dur*head%, dur*(1-tail%)] 内均匀（避开 OP/ED）
- interval  固定间隔：每 N 秒一帧，列数固定、行数按片长自动算
- range     自定义时间段：只在 [start, end] 内均匀
- random    随机抽帧：全片随机取点并升序；可要多份结果（每份换种子）
- exact     精确时间点：指定点必入选且走精确 seek，其余用均匀点补满，全部升序
- cover     封面单帧：从列表缩略图的同一位置（VIDEO_SEEK_FRACTION）起算

拼接：每帧缩放到统一 cell（保持宽高比 + 黑边填充，网格整齐），
PIL 先拼行再叠行成大图，JPG/PNG 输出。
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .thumbs import VIDEO_SEEK_FRACTION, MpvGrabber

# 模式标识（存进 settings，故必须稳定）
MODE_EVEN = "even"
MODE_TRIM = "trim"
MODE_INTERVAL = "interval"
MODE_RANGE = "range"
MODE_RANDOM = "random"
MODE_EXACT = "exact"
MODE_COVER = "cover"

ALL_MODES = (MODE_EVEN, MODE_TRIM, MODE_INTERVAL, MODE_RANGE,
             MODE_RANDOM, MODE_EXACT, MODE_COVER)

# 固定间隔模式的格数上限：1 小时片按每 10 秒一帧就是 360 格，
# 拼出来是几亿像素的图（PIL 直接爆内存）。封顶后自动放大间隔。
MAX_CELLS = 200

# 随机模式两点之间的最小间隔（占片长比例）：纯随机会挑出两张几乎
# 一样的邻近帧，网格看着像重复。
RANDOM_MIN_GAP_RATIO = 0.01


class PlanError(ValueError):
    """参数不成立（如起点≥止点）：调用方按"该视频该模式失败"处理。"""


class DecodeFailed(RuntimeError):
    """时长探测不到：损坏文件或没有视频轨。"""


class NoPlans(RuntimeError):
    """所选抓帧方式的参数一个都不成立——与"无法解码"是两回事，提示不同。"""


@dataclass
class GridOptions:
    """一次生成请求的全部抓帧参数（UI 与生成逻辑之间的契约）。"""

    rows: int = 5
    cols: int = 5
    modes: tuple[str, ...] = (MODE_EVEN,)
    trim_head_pct: float = 5.0        # 跳过片头百分比
    trim_tail_pct: float = 5.0        # 跳过片尾百分比
    interval_secs: float = 30.0       # 固定间隔秒数
    range_start: float = 0.0          # 自定义时间段起（秒）
    range_end: float = 0.0            # 止（秒）；<=0 表示到片尾
    random_count: int = 1             # 随机模式产出几张结果图
    exact_times: tuple[float, ...] = ()   # 精确时间点（秒）
    seed: int | None = None           # 仅测试用：固定随机序列


@dataclass
class CapturePlan:
    """一张待生成的网格图：时间点已升序排好。"""

    mode: str
    rows: int
    cols: int
    times: tuple[float, ...]
    precise: frozenset[int] = frozenset()   # 需精确 seek 的时间点下标
    suffix: str = ""                        # 文件名后缀（空=沿用旧命名）
    label: str = ""                         # 日志/UI 用的短标识

    @property
    def count(self) -> int:
        return len(self.times)


# ---------------------------------------------------------------- 时间解析
_TIME_RE = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)\s*$")


def parse_time(text: str) -> float | None:
    """把 "90" / "1:30" / "1:02:03" / "90.5" 解析成秒；无法解析返回 None。

    用户手填时间点/时间段的唯一入口——三种写法都常见，只认纯秒数会让
    "1:30" 变成 1 秒（静默错到看不出来）。
    """
    m = _TIME_RE.match(str(text or ""))
    if m is None:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    secs = float(c)
    if b is not None:          # a:b:c → 时:分:秒
        secs += int(b) * 60 + int(a) * 3600
    elif a is not None:        # a:c → 分:秒
        secs += int(a) * 60
    return secs


def parse_time_list(text: str) -> list[float]:
    """解析逗号/中文逗号/分号/空白分隔的时间点列表（跳过解析不了的项）。"""
    out: list[float] = []
    for token in re.split(r"[,，;；\s]+", str(text or "")):
        if not token:
            continue
        v = parse_time(token)
        if v is not None and v >= 0:
            out.append(v)
    return out


def format_time(secs: float) -> str:
    """秒 → mm:ss / h:mm:ss（回填输入框、日志展示用）。"""
    secs = max(0.0, float(secs))
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------- 计划构建
def _even_times(start: float, end: float, n: int) -> list[float]:
    """在 (start, end) 内取 n 个均匀点：start + span/(n+1)*i。

    不含端点——首帧常是黑场/台标，末帧常是版权卡，两头都不值得占格子。
    """
    if n <= 0:
        return []
    span = max(0.0, end - start)
    step = span / (n + 1)
    return [start + step * (i + 1) for i in range(n)]


def _clamp_window(duration: float, start: float, end: float) -> tuple[float, float]:
    """把时间窗夹到 [0, duration]；窗口塌缩（起≥止）时抛 PlanError。"""
    lo = min(max(0.0, float(start)), duration)
    hi = float(end) if end and end > 0 else duration
    hi = min(max(0.0, hi), duration)
    if hi - lo <= 0.01:
        raise PlanError("time window is empty")
    return lo, hi


def _random_times(duration: float, n: int, rng: random.Random) -> list[float]:
    """全片随机取 n 点并升序；强制最小间隔，避免抽出几乎相同的邻近帧。

    间隔约束靠"分桶后桶内随机"实现：把片长切成 n 段，每段里随机取一点。
    这样既保证分散（不会两张挨在一起），又保证每次结果都不同。
    分桶本身天然递增，最后仍显式排序——桶策略以后若改成纯随机取样，
    这行是"时间顺序不能乱"的唯一保障。
    """
    if n <= 0 or duration <= 0:
        return []
    seg = duration / n
    gap = duration * RANDOM_MIN_GAP_RATIO
    times: list[float] = []
    for i in rng.sample(range(n), n):   # 打乱产出顺序，逼排序真正生效
        lo = i * seg
        hi = lo + seg
        # 段内留出 gap/2 边距，让相邻段的取点也不会贴在分界线两侧
        lo2 = min(lo + gap / 2, hi)
        hi2 = max(hi - gap / 2, lo2)
        times.append(rng.uniform(lo2, hi2) if hi2 > lo2 else lo2)
    return sorted(times)


def _cells_for_interval(duration: float, interval: float, cols: int
                        ) -> tuple[int, int, float]:
    """固定间隔：返回 (格数, 列数, 实际间隔)。

    格数由片长决定，列数沿用用户设置——长片自动出更高的图，不再被 R×C
    锁死。行数由调用方从最终时间点数反算（只有一处算法，避免两处不一致）。
    总格数封顶 MAX_CELLS（超了就放大间隔），否则长片会拼出几亿像素的
    大图直接把 PIL 打爆。
    """
    interval = max(0.1, float(interval))
    cols = max(1, int(cols))
    n = max(1, int(duration // interval))
    if n > MAX_CELLS:
        n = MAX_CELLS
        interval = duration / (n + 1)
    return n, cols, interval


def build_plans(duration: float, opts: GridOptions) -> list[CapturePlan]:
    """按选中的模式生成抓帧计划（每个模式一张图，随机模式 N 张）。

    duration <= 0（探测失败）时返回空表——调用方据此计"解码失败"。
    单个模式参数不成立（如时间段起≥止）时跳过该模式并不影响其他模式；
    全部模式都不成立时返回空表。
    """
    if not duration or duration <= 0:
        return []
    rows = max(1, int(opts.rows))
    cols = max(1, int(opts.cols))
    n = rows * cols
    modes = tuple(opts.modes) or (MODE_EVEN,)
    plans: list[CapturePlan] = []
    rng = random.Random(opts.seed)

    for mode in ALL_MODES:            # 固定顺序输出，与勾选顺序无关
        if mode not in modes:
            continue
        try:
            plans.extend(_plans_for_mode(mode, duration, rows, cols, n, opts, rng))
        except PlanError:
            continue
    return plans


def _plans_for_mode(mode: str, duration: float, rows: int, cols: int, n: int,
                    opts: GridOptions, rng: random.Random) -> list[CapturePlan]:
    if mode == MODE_EVEN:
        return [CapturePlan(mode, rows, cols,
                            tuple(_even_times(0.0, duration, n)),
                            suffix="", label="even")]

    if mode == MODE_TRIM:
        head = min(max(0.0, float(opts.trim_head_pct)), 45.0) / 100.0
        tail = min(max(0.0, float(opts.trim_tail_pct)), 45.0) / 100.0
        lo, hi = _clamp_window(duration, duration * head,
                               duration * (1.0 - tail))
        return [CapturePlan(mode, rows, cols, tuple(_even_times(lo, hi, n)),
                            suffix="_trim", label="trim")]

    if mode == MODE_INTERVAL:
        cells, c, step = _cells_for_interval(duration, opts.interval_secs, cols)
        times = [step * (i + 1) for i in range(cells)]
        times = [tm for tm in times if tm < duration] or [duration / 2.0]
        # 行数从最终时间点数反算：写死行数会让短片多出整行空白、长片被截断
        r = max(1, -(-len(times) // c))
        return [CapturePlan(mode, r, c, tuple(times),
                            suffix="_interval", label="interval")]

    if mode == MODE_RANGE:
        lo, hi = _clamp_window(duration, opts.range_start, opts.range_end)
        return [CapturePlan(mode, rows, cols, tuple(_even_times(lo, hi, n)),
                            suffix="_range", label="range")]

    if mode == MODE_RANDOM:
        count = min(max(1, int(opts.random_count)), 10)
        out = []
        for k in range(count):
            times = _random_times(duration, n, rng)
            suffix = f"_random{k + 1}" if count > 1 else "_random"
            out.append(CapturePlan(mode, rows, cols, tuple(times),
                                   suffix=suffix, label=f"random{k + 1}"))
        return out

    if mode == MODE_EXACT:
        picked = sorted({min(max(0.0, t), duration) for t in opts.exact_times})
        if not picked:
            raise PlanError("no exact times given")
        picked = picked[:n]
        # 指定点必定入选；剩余格子用均匀点补满，且不与指定点太近
        fill_needed = n - len(picked)
        fill: list[float] = []
        if fill_needed > 0:
            gap = duration * RANDOM_MIN_GAP_RATIO
            for t in _even_times(0.0, duration, fill_needed * 3):
                if all(abs(t - p) > gap for p in picked + fill):
                    fill.append(t)
                if len(fill) >= fill_needed:
                    break
            while len(fill) < fill_needed:     # 极短片：补不出来就重复中点
                fill.append(duration / 2.0)
        times = sorted(picked + fill)          # 时间顺序不能乱
        exact_at = frozenset(i for i, t in enumerate(times) if t in set(picked))
        return [CapturePlan(mode, rows, cols, tuple(times), precise=exact_at,
                            suffix="_exact", label="exact")]

    if mode == MODE_COVER:
        # 封面帧：与列表缩略图同一位置，"把缩略图拿来用"
        base = duration * VIDEO_SEEK_FRACTION
        if n == 1:
            times = [base]
        else:
            times = _even_times(base, duration, n)
        return [CapturePlan(mode, rows, cols, tuple(times),
                            suffix="_cover", label="cover")]

    return []


# ---------------------------------------------------------------- 抓帧
def extract_frames_at(
    grabber: MpvGrabber,
    path: str,
    times,
    precise=frozenset(),
    timeout: float = 8.0,
) -> list[Image.Image]:
    """按给定时间点抓帧；失败的位置用深色占位图补上（不打断整张图）。

    precise 里的下标走精确 seek（时间点严格对齐，慢约 20 倍），其余走
    关键帧 seek——只有用户明确指定的时间点才值得付这个代价。
    """
    frames: list[Image.Image] = []
    for i, ts in enumerate(times):
        exact = i in precise
        img = grabber.frame_at(ts, timeout=timeout * (3 if exact else 1),
                               precise=exact)
        if img is None:
            frames.append(Image.new("RGB", (160, 90), (10, 10, 10)))
        else:
            frames.append(img.convert("RGB"))
    return frames


def extract_grid_frames(
    grabber: MpvGrabber,
    path: str,
    rows: int,
    cols: int,
    duration: float | None = None,
) -> tuple[list[Image.Image], float | None]:
    """均匀抽取 rows*cols 帧并返回 (帧列表, 实际时长)。

    旧接口，等价于只选 MODE_EVEN 的一张图；保留给不关心模式的调用方。
    duration 可传入已知值（省一次 open）；None 时由 grabber 探测。
    """
    if duration is None:
        dur, _w, _h = grabber.open(path)
        duration = dur
    if not duration or duration <= 0:
        return [], duration
    plans = build_plans(duration, GridOptions(rows=rows, cols=cols,
                                              modes=(MODE_EVEN,)))
    if not plans:
        return [], duration
    return extract_frames_at(grabber, path, plans[0].times), duration


# ---------------------------------------------------------------- 拼接
def compose_grid(
    frames: list[Image.Image],
    rows: int,
    cols: int,
    cell_width: int = 160,
    bg: tuple = (10, 10, 12),
    quality: int = 88,
) -> Image.Image:
    """把帧列表拼成网格大图（先行后列）。

    每帧等比缩放到 cell_width 宽，cell 高取全体帧的最大高（网格整齐）；
    不足处黑边填充（pad 居中）。行间/列间留 4px 缝隙；1×1 时无缝隙，
    输出就是单张截图。
    """
    if not frames:
        raise ValueError("no frames")
    cells: list[Image.Image] = []
    cell_h = 0
    for f in frames:
        w, h = f.size
        if w == 0 or h == 0:
            continue
        nh = max(1, round(h * cell_width / w))
        cell_h = max(cell_h, nh)
    cell_h = cell_h or round(cell_width * 9 / 16)
    for f in frames:
        w, h = f.size
        if w == 0 or h == 0:
            cells.append(Image.new("RGB", (cell_width, cell_h), bg))
            continue
        nh = max(1, round(h * cell_width / w))
        scaled = f.resize((cell_width, nh), Image.LANCZOS)
        if nh == cell_h:
            cells.append(scaled)
            continue
        cell = Image.new("RGB", (cell_width, cell_h), bg)
        cell.paste(scaled, (0, (cell_h - nh) // 2))
        cells.append(cell)

    gap = 4
    out_w = cols * cell_width + max(0, cols - 1) * gap
    out_h = rows * cell_h + max(0, rows - 1) * gap
    sheet = Image.new("RGB", (out_w, out_h), bg)
    for idx, cell in enumerate(cells[: rows * cols]):
        r, c = divmod(idx, cols)
        sheet.paste(cell, (c * (cell_width + gap), r * (cell_h + gap)))
    return sheet


def generate_sheets(
    grabber: MpvGrabber,
    video: Path,
    out_dir: Path,
    opts: GridOptions,
    cell_width: int = 160,
    fmt: str = "jpg",
    quality: int = 88,
    on_exists: str = "rename",
    duration: float | None = None,
    should_cancel=None,
) -> list[tuple[CapturePlan, Path]]:
    """一个视频 → 按计划产出 1..N 张网格图，返回 [(计划, 输出路径)]。

    与 UI 无关（进度窗只负责报进度和写日志），所以能用假 grabber 单测：
    「勾了 3 种模式是不是真出 3 张」「模式后缀有没有落到文件名」
    「精确时间点有没有走精确 seek」都在这里定。

    duration 为 None 时由 grabber 探测；探测不到（损坏/无视频轨）时抛
    DecodeFailed，计划为空时抛 NoPlans——调用方据此给出不同的提示，
    别把「时间段填反了」说成「无法解码」。
    """
    if duration is None:
        dur, _w, _h = grabber.open(str(video))
        duration = dur
    if not duration or duration <= 0:
        raise DecodeFailed(str(video))
    plans = build_plans(duration, opts)
    if not plans:
        raise NoPlans(str(video))
    made: list[tuple[CapturePlan, Path]] = []
    for plan in plans:
        if should_cancel is not None and should_cancel():
            break
        frames = extract_frames_at(grabber, str(video), plan.times,
                                   precise=plan.precise)
        if not frames:
            continue
        sheet = compose_grid(frames, plan.rows, plan.cols,
                             cell_width=cell_width, quality=quality)
        out = save_sheet(sheet, video, out_dir, fmt=fmt, quality=quality,
                         on_exists=on_exists, suffix=plan.suffix)
        made.append((plan, out))
    return made


def save_sheet(
    sheet: Image.Image,
    video_path: Path,
    out_dir: Path,
    fmt: str = "jpg",
    quality: int = 88,
    on_exists: str = "rename",
    suffix: str = "",
) -> Path:
    """保存网格图：命名 [stem]_thumb[suffix].[ext]，冲突时按策略处理。

    suffix 区分抓帧模式（_trim/_random1/...），否则一次运行里多个模式的
    结果会互相覆盖（或全被 rename 成 (1)(2)，事后分不清哪张是哪种）。

    on_exists: skip（返回原路径表示已存在，调用方计跳过）/ overwrite /
    rename（追加 (1)、(2)…）
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if fmt == "jpg" else "png"
    target = out_dir / f"{video_path.stem}_thumb{suffix}.{ext}"
    if target.exists():
        if on_exists == "skip":
            return target
        if on_exists == "rename":
            i = 1
            while True:
                target = out_dir / f"{video_path.stem}_thumb{suffix}({i}).{ext}"
                if not target.exists():
                    break
                i += 1
    if ext == "jpg":
        # PNG 无损：quality 对它无意义，故只在 JPEG 分支使用——不要为了
        # "统一"把 quality 归零写回设置（那会静默清掉用户调的 JPG 质量）
        sheet.save(target, "JPEG", quality=max(1, min(100, int(quality))),
                   optimize=True)
    else:
        sheet.save(target, "PNG", optimize=True)
    return target
