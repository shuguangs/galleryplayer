"""Thumbnail engine.

Images go through Pillow (HEIC/AVIF included). Video frames come from headless
libmpv instances -- one per worker thread, kept alive and re-used, so there is no
dependency on an external ffmpeg.exe. Results are cached on disk keyed by
path+size+mtime, and duration/resolution discovered along the way is folded back
into the shared metadata cache.
"""
from __future__ import annotations

import heapq
import queue
import sys
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from . import netpath
from .media import MediaItem, metadata
from .runtime import USERDATA_DIR

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional codec
    pass

Image.MAX_IMAGE_PIXELS = None  # we control the source; avoid decompression-bomb aborts

THUMB_DIR = USERDATA_DIR / "thumbs"
GRID_THUMB_MAX = 420          # long edge of a cached grid thumbnail
VIDEO_SEEK_FRACTION = 0.18    # grab the cover this far into the video
IMAGE_WORKERS = 2
# 视频解码线程布局：
#   - VIDEO_WORKERS 个通用线程（当前 1）：按优先级取任意视频单；
#   - 1 个视口专用线程（只取 prio < WARMUP_PRIO）：预热期间空挂，
#     滚动视口才醒；
#   - 1 个 gated 通用线程：播放器可见时挂起，隐藏后由
#     set_video_headroom(True) 放开——此时滚动视口最多三路解码，
#     纯预热两路；正在播放时保持通用单路，不抢播放的 CPU。
# 空闲线程只是停在一个 Event.wait 上，且首次取到活之前不会创建
# mpv 实例，几乎零成本。每个并发视频 grab 持有一个 libmpv 实例
# （~55 MB），文件夹安定后由 15s sweep 释放。远程路径另由
# _REMOTE_GATE 串行，与此数无关。
VIDEO_WORKERS = 1
# Hard ceiling on decoded thumbnails held in RAM. At ~420x420x3 bytes each this
# caps the cache near 100 MB; past that, re-reading the small disk JPEG is cheap,
# so an unbounded cache would only trade a browsable folder size for memory.
MEMORY_CACHE_MAX = 200
# Ceiling on jobs accepted but not yet finished. The tile view asks for a thumbnail for
# every tile it paints, so flinging the scrollbar through a folder of twenty thousand
# files used to queue a decode for every tile that flashed past — thousands of jobs for
# images nobody is looking at any more, all of which still had to be worked through
# before the ones actually on screen got a turn. Refusing work past this point is safe
# precisely because painting is what asks: whatever is still visible is requested again
# on the next repaint, so the queue naturally refills with what the user can see.
MAX_QUEUED_JOBS = 256
# How many times a file may fail before it is left alone for the rest of the session.
MAX_ATTEMPTS = 2
# 预热/侧栏等非视口请求的优先级：恒排在视口请求（priority=行号）之后，
# 视口专用 worker 以此为界区分"视口单"和"预热单"。
WARMUP_PRIO = 1e17


def pil_to_qimage(img: Image.Image) -> QImage:
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    fmt = QImage.Format_RGBA8888 if img.mode == "RGBA" else QImage.Format_RGB888
    data = img.tobytes()
    # copy() so the QImage owns its buffer once `data` is garbage collected
    return QImage(data, img.width, img.height, img.width * len(img.mode), fmt).copy()


# --------------------------------------------------------------------------- mpv


# Every grabber is registered so the app can terminate them deliberately at exit.
# Letting libmpv instances be finalised by the interpreter — from whichever worker
# thread happens to own them — makes the process die with a non-zero status.
_GRABBERS: "list[MpvGrabber]" = []
_GRABBERS_LOCK = threading.Lock()
_STOPPING = threading.Event()

# Several workers seeking around different files on one network share just thrash the
# link and make every thumbnail slower. Remote extraction is therefore serialised.
_REMOTE_GATE = threading.Semaphore(1)
REMOTE_LOAD_TIMEOUT = 60.0
REMOTE_SEEK_TIMEOUT = 25.0

# mpv's MPV_END_FILE_REASON_ERROR: the file could not be played at all.
_END_FILE_ERROR = 4
# How long a file may sit in mpv's idle state before it is assumed unloadable. Only a
# backstop -- the end-file event normally settles it in milliseconds.
IDLE_GRACE = 2.5


class MpvGrabber:
    """A headless libmpv instance used purely to pull decoded frames.

    Each live instance costs roughly 55 MB, so idle ones are released rather than
    kept for the lifetime of the process; re-creating one costs a few hundred ms.
    """

    def __init__(self, tag: str = "thumb") -> None:
        import mpv

        self._mpv_mod = mpv
        self._m = None
        self._current: str | None = None
        self.tag = tag
        # Held for the duration of a grab. The idle sweep runs on the GUI thread and
        # must never block on it, so it only reclaims grabbers it can take without
        # waiting -- terminating an instance out from under the worker using it is how
        # a perfectly good file ends up with no thumbnail.
        self._busy = threading.Lock()
        self._load_failed = threading.Event()
        self._load_started = threading.Event()
        with _GRABBERS_LOCK:
            _GRABBERS.append(self)

    @property
    def live(self) -> bool:
        return self._m is not None

    def _instance(self):
        if self._m is None:
            self._m = self._mpv_mod.MPV(
                vo="null",
                ao="null",
                audio="no",
                sub="no",
                keep_open="yes",
                idle="yes",
                cache="no",
                terminal="no",
                **{
                    "vd-lavc-fast": "yes",
                    "vd-lavc-skiploopfilter": "all",
                    "vd-lavc-threads": "2",
                    "sws-scaler": "fast-bilinear",
                    "hwdec": "auto-safe",
                    # single frames only: no point buffering ahead
                    "demuxer-max-bytes": "16MiB",
                    "demuxer-readahead-secs": "0",
                    "load-scripts": "no",
                    "osc": "no",
                    "input-default-bindings": "no",
                },
            )

            # An unplayable file otherwise shows up only as "nothing ever started
            # playing", which is indistinguishable from a slow one -- so the code sat
            # out the entire load timeout on it. mpv knows within milliseconds.
            failed, started = self._load_failed, self._load_started

            @self._m.event_callback("start-file")
            def _on_start_file(_event, _started=started):  # noqa: ANN001
                _started.set()

            @self._m.event_callback("end-file")
            def _on_end_file(event, _failed=failed, _started=started):  # noqa: ANN001
                # Loading a new file ends the previous one, and that end-file lands
                # after the new load has been asked for. Only an end-file that follows
                # *our* start-file is about the file we are waiting on -- without this
                # gate a stale event from the file before it failed a perfectly good
                # one, intermittently and only when grabs came back to back.
                if not _started.is_set():
                    return
                try:
                    reason = int(getattr(event.data, "reason", -1))
                except (TypeError, ValueError):
                    reason = -1
                if reason == _END_FILE_ERROR:
                    _failed.set()

            self._event_cbs = (_on_start_file, _on_end_file)  # keep them referenced
        return self._m

    def _wait_loaded(self, m, timeout: float) -> bool:
        """Block until the file is decodable, or until it is clear it never will be.

        Replaces mpv's `wait_until_playing`, which only ever waits for success: a file
        with a damaged header (a half-finished download, say) left it waiting out the
        full timeout -- 25 s locally, 60 s on a share, per file, on a worker thread that
        could have been building the thumbnails the user is actually looking at.
        """
        began = time.monotonic()
        deadline = began + timeout
        while time.monotonic() < deadline:
            if self._load_failed.is_set():
                return False   # end-file said "error": definitive, and usually instant
            try:
                if self._load_started.is_set():
                    # Only meaningful once mpv has begun *this* file. The instance is
                    # reused across files, so before start-file these still hold the
                    # previous one's values -- reading them too early declared the load
                    # complete while mpv was still between files, and the seek that
                    # followed then failed with "error running command".
                    if m.width or m.duration is not None:
                        return True
                elif time.monotonic() - began > IDLE_GRACE:
                    # Backstop for a file mpv drops without ever starting it.
                    # Deliberately generous: mpv handles loadfile on its own thread,
                    # and with a full thumbnail queue that can lag, so a tight bound
                    # here would fail good files whenever the machine is busy.
                    return False
            except Exception:
                pass
            time.sleep(0.02)
        return False

    def _reset(self) -> None:
        if self._m is not None:
            try:
                self._m.terminate()
            except Exception:
                pass
        self._m = None
        self._current = None

    def open(self, path: str, timeout: float = 25.0) -> tuple[float | None, int | None, int | None]:
        """Load `path` (reusing the instance) and return (duration, width, height)."""
        m = self._instance()
        if self._current != path:
            self._load_failed.clear()
            self._load_started.clear()
            # pause MUST be cleared before loading: a paused core never leaves the idle
            # state, so a reused instance (we always leave it paused after grabbing)
            # would look unloadable on every subsequent file.
            m.pause = False
            m.play(path)
            if not self._wait_loaded(m, timeout):
                raise RuntimeError(f"mpv could not load {path}")
            self._current = path
        m.pause = True
        return m.duration, m.width, m.height

    def frame_at(self, seconds: float, timeout: float = 6.0,
                 precise: bool = False) -> Image.Image | None:
        """Seek to roughly `seconds` and return the frame the decoder lands on.

        Deliberately a keyframe seek by default, not an exact one. An exact seek
        decodes forward from the preceding keyframe to the requested timestamp, which
        on a long H.264 file with sparse keyframes is seconds of work -- often more
        than the timeout, and then the file got no thumbnail at all. Landing on the
        nearest keyframe is ~20x faster (measured: 391 ms -> 21 ms on a 143 MB clip)
        and for a cover frame the difference is invisible.

        `precise=True` opts into the exact seek for the callers that need the frame to
        be the one at that timestamp -- the thumbnail-grid "exact timestamps" mode,
        where the user typed the position and a keyframe several seconds away is the
        wrong frame. Pass a longer timeout with it; the caller decides that trade.

        mpv's seek is asynchronous, so polling is what makes the returned frame belong
        to the new position rather than the old one.
        """
        import time

        m = self._instance()
        target = max(0.0, seconds)
        m.command("seek", target, "absolute+exact" if precise else "absolute+keyframes")
        deadline = time.monotonic() + timeout
        settled_since: float | None = None
        while time.monotonic() < deadline:
            time.sleep(0.02)
            try:
                if m.seeking:
                    settled_since = None
                    continue
                pos = m.time_pos
            except Exception:
                break
            if pos is None:
                continue
            # A keyframe seek can land seconds away from the request, so "close to the
            # target" is not the test -- "stopped moving" is.
            now = time.monotonic()
            settled_since = settled_since or now
            if now - settled_since > 0.15:
                break
        return m.screenshot_raw(includes="video")

    def grab(self, path: str, fraction: float) -> tuple[Image.Image | None, float | None, int | None, int | None]:
        remote = netpath.is_remote(path)
        gate = _REMOTE_GATE if remote else None
        if gate is not None:
            gate.acquire()
        self._busy.acquire()
        try:
            dur, w, h = self.open(
                path, timeout=REMOTE_LOAD_TIMEOUT if remote else 25.0
            )
            ts = (dur or 0.0) * fraction
            if dur and dur > 4:
                ts = min(max(ts, 0.5), dur - 0.5)
            return self.frame_at(ts, REMOTE_SEEK_TIMEOUT if remote else 6.0), dur, w, h
        except Exception:
            self._reset()
            return None, None, None, None
        finally:
            self._busy.release()
            if gate is not None:
                gate.release()

    def close(self) -> None:
        self._reset()
        with _GRABBERS_LOCK:
            if self in _GRABBERS:
                _GRABBERS.remove(self)


def release_idle_grabbers(tag: str = "thumb") -> int:
    """Terminate the libmpv instance inside every *idle* grabber with `tag`, keeping the
    (cheap) wrapper registered so later work transparently spins a new one up.

    A grabber that is mid-extraction is skipped rather than waited for: this is called
    from a timer on the GUI thread, so blocking is not an option, and tearing an
    instance down under the worker using it silently cost that file its thumbnail.
    """
    with _GRABBERS_LOCK:
        targets = [g for g in _GRABBERS if g.tag == tag and g.live]
    freed = 0
    for g in targets:
        if not g._busy.acquire(blocking=False):
            continue
        try:
            g._reset()
            freed += 1
        except Exception:
            pass
        finally:
            g._busy.release()
    return freed


def shutdown_grabbers() -> None:
    """Terminate every headless libmpv instance. Call once, at exit."""
    _STOPPING.set()
    with _GRABBERS_LOCK:
        pending = list(_GRABBERS)
        _GRABBERS.clear()
    for g in pending:
        try:
            g._reset()
        except Exception:
            pass


_tls = threading.local()


def _thread_grabber() -> MpvGrabber:
    g = getattr(_tls, "grabber", None)
    if g is None:
        g = MpvGrabber()
        _tls.grabber = g
    return g


def _yield_thread_priority(t: threading.Thread) -> None:
    """Windows：把缩略图 worker 线程降到 BELOW_NORMAL。

    Pillow 的图片解码跑在本线程内，降级后调度上让路给播放/UI 线程——
    播放期间浏览器图片缩略图照常出但不再抢 CPU。抓帧用的 libmpv 内部
    线程不受此影响，那部分由 set_playback_active 在播放期间整体让路。
    非 Windows / 权限不足时静默跳过。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenThread(0x0002, False, t.native_id)  # THREAD_SET_INFORMATION
        if handle:
            k32.SetThreadPriority(handle, -1)                 # BELOW_NORMAL
            k32.CloseHandle(handle)
    except Exception:
        pass


def _log_giveup(item: MediaItem) -> None:
    """一个文件彻底解码失败（本会话内放弃）：留痕，"格子怎么是空的"类
    bug 的第一手证据。worker 线程调用，startup_log 的写入自身带兜底。"""
    try:
        from . import startup_log

        startup_log.stage(
            "thumb-fail",
            f"放弃解码（{MAX_ATTEMPTS} 次尝试）: {item.path}")
    except Exception:
        pass


# ------------------------------------------------------------------------ cache


class ThumbnailCache(QObject):
    """Async thumbnail provider. `ready` fires on the GUI thread."""

    ready = Signal(str, QImage)        # cache_key, thumbnail
    meta_ready = Signal(str, object)   # cache_key, (duration, width, height)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # A previous cache's shutdown() latches the module-level stop flag; clear it or
        # this new cache would silently discard every job it is ever given.
        _STOPPING.clear()
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        # 视口优先调度：请求入优先级堆（新者优先），worker 常驻取件。
        # 视频线程只处理视频项（解码重、单独限流），图片线程处理其余。
        self._prio_heap: list = []
        self._prio_seq = 0
        self._prio_wake = threading.Event()
        self._closed = False
        self._workers: list[threading.Thread] = []
        self._memory: "OrderedDict[str, QImage]" = OrderedDict()
        self._pending: set[str] = set()
        # pending 的入队顺序（FIFO）：队列满时按序驱逐最旧的——滚出视口
        # 的请求先让位，正在绘制的新视口请求优先进（防优先级反转）
        self._pending_order: "deque[str]" = deque()
        # key → 当前生效的最高优先级：同 key 重复请求且优先级更高时，
        # 堆里追加新条目并把旧条目标记为过期（出堆时惰性丢弃）——paint
        # 每帧都会请求可见项，滚动后行号变化要能"升级"已排队请求
        self._key_prio: dict[str, float] = {}
        # key -> attempts. Not a plain blacklist: a file that failed once because a
        # worker was interrupted, or because it was still being copied in, deserves
        # another go. Only after MAX_ATTEMPTS is it given up on for this session.
        self._failed: dict[str, int] = {}
        self._lock = threading.Lock()
        # Separate lock for the LRU: the GUI thread reorders it on every painted tile
        # while worker threads insert and evict, and OrderedDict is not thread-safe.
        self._mem_lock = threading.Lock()
        self._generation = 0
        self._video_inflight = 0
        # 播放器不可见时置 True（set_video_headroom）：放开 gated 的第 2 路
        # 通用视频线程；正在播放时保持 False，缩略图解码不与播放抢 CPU
        self._vid_extra_enabled = False
        # 视频正在播放时置 True（set_playback_active）：所有视频抓帧 worker
        # 停止取件、request 拒收视频单——抓帧（mpv open+seek+解码）是播放
        # 期间最重的负载，实测多路并发直接造成播放卡顿
        self._playback_active = False

        # worker 线程必须最后起：它们立刻访问 _lock/_prio_heap 等属性
        # thumb-vid-vp 是视口专用线程：只取视口请求（prio < WARMUP_PRIO），
        # 预热期间空挂——滚动视口时才醒，视口填充翻倍。
        # thumb-vid-x 是 gated 的第 2 路通用视频线程：播放器可见或正在
        # 播放时挂起，播放器隐藏且空闲后由 set_video_headroom 放开。
        for name, want_video, n, vp_only, gated in (
            ("thumb-vid", True, VIDEO_WORKERS, False, False),
            ("thumb-vid-vp", True, 1, True, False),
            ("thumb-vid-x", True, 1, False, True),
            ("thumb-img", False, IMAGE_WORKERS, False, False),
        ):
            for i in range(n):
                t = threading.Thread(
                    target=self._prio_worker_loop,
                    args=(want_video, vp_only, gated), daemon=True,
                    name=f"{name}-{i}")
                t.start()
                _yield_thread_priority(t)
                self._workers.append(t)

        # Reclaim the ~55 MB each idle video grabber holds once a folder is scanned.
        self._idle_sweep = QTimer(self)
        self._idle_sweep.setInterval(15_000)
        self._idle_sweep.timeout.connect(self._sweep_idle_grabbers)
        self._idle_sweep.start()

    def _sweep_idle_grabbers(self) -> None:
        # 顺带把脏了的元数据落盘。metadata 只在正常退出时 save，强杀/崩溃
        # 会丢整段会话的记录，造成"缩略图在、时长没了"的缓存漂移——漂移项
        # 走磁盘命中路径时得内联 mpv open 补时长（最坏 25s/远程 60s），把
        # 唯一的通用视频 worker 串行拖住。15s 一存，崩溃最多丢最近 15s。
        # 后台线程执行：十万级条目的 json.dumps 在 GUI 线程实测可达上百
        # ms，播放中每 15s 卡一下的来源之一。耗时打点（>100ms 才记，
        # 免得 15s 一条刷屏）。
        def _save_meta() -> None:
            t0 = time.monotonic()
            metadata.save()
            elapsed = time.monotonic() - t0
            if elapsed > 0.1:
                try:
                    from . import startup_log

                    startup_log.stage(
                        "meta-save", f"元数据落盘 {elapsed * 1000:.0f}ms（后台线程）")
                except Exception:
                    pass

        threading.Thread(target=_save_meta, daemon=True,
                         name="meta-save").start()
        with self._lock:
            busy = self._video_inflight or any(True for _ in self._pending)
        if busy:
            return
        release_idle_grabbers("thumb")

    # -- public API -------------------------------------------------------

    def peek(self, item: MediaItem) -> QImage | None:
        """Look up a decoded thumbnail, refreshing its position in the LRU."""
        key = item.cache_key
        with self._mem_lock:
            img = self._memory.get(key)
            if img is not None:
                self._memory.move_to_end(key)
        return img

    def request(self, item: MediaItem, priority: float | None = None) -> QImage | None:
        """Return a cached thumbnail immediately, or schedule generation.

        priority：渲染优先级，小者先（网格 paint 传行号——视口内从上往下、
        行内从左往右的阅读顺序）。None=低优先（列表侧栏等非视口请求）。
        同优先级内新请求优先（快速滚动后停住时视口重请求插队）。

        队列满时驱逐最旧的 pending 项腾位（而不是拒绝新请求）：paint 是
        请求方，被拒的项只有等下一次重绘——快速滚动时旧视口的请求占满
        队列，当前视口反而拿不到坑位（优先级反转）。驱逐最旧 = 滚出
        视口的先让位，正在绘制的（新视口）优先进。
        """
        key = item.cache_key
        with self._mem_lock:
            cached = self._memory.get(key)
            if cached is not None:
                self._memory.move_to_end(key)
        if cached is not None:
            return cached
        prio = 1e18 if priority is None else priority
        with self._lock:
            if self._failed.get(key, 0) >= MAX_ATTEMPTS:
                return None
            if self._playback_active and item.is_video:
                # 播放让路：视频单不入队——入了也没 worker 取，只会占满
                # 队列水位，把图片预热/视口请求饿死。paint 是请求方，
                # 播放结束后的下一帧（或预热的下一轮）会重新请求。
                return None
            if key in self._pending:
                # 已排队：优先级更高时升级（堆内追加新条目，旧条目出堆时
                # 惰性丢弃）。paint 每帧重请求可见项——滚动后行号变小
                # （项进入视口上部）要能插队。
                if prio < self._key_prio.get(key, 1e18):
                    self._key_prio[key] = prio
                    self._prio_seq += 1
                    heapq.heappush(self._prio_heap,
                                   (prio, -self._prio_seq, key, item,
                                    self._generation))
                    self._prio_wake.set()
                return None
            if len(self._pending) >= MAX_QUEUED_JOBS:
                # 驱逐仍 pending 的最旧项（按入队序；order 里可能混有已
                # 完成/已失效的 key，跳过）。被驱逐者已 submit 的工作仍
                # 会执行并缓存（结果不浪费），但它让出"等待坑位"给正在
                # 绘制的新视口——重复解码一次是小代价。
                while len(self._pending) >= MAX_QUEUED_JOBS and self._pending_order:
                    oldest = self._pending_order.popleft()
                    if oldest in self._pending:
                        self._pending.discard(oldest)
                        self._key_prio.pop(oldest, None)
                if len(self._pending) >= MAX_QUEUED_JOBS:
                    return None
            self._pending.add(key)
            self._pending_order.append(key)
            gen = self._generation
            self._key_prio[key] = prio
            # (priority, -seq)：priority 小者先（阅读顺序）；同级新者先。
            self._prio_seq += 1
            heapq.heappush(self._prio_heap,
                           (prio, -self._prio_seq, key, item, gen))
            self._prio_wake.set()
        return None

    def focus(self, visible_keys: "set[str]") -> None:
        """只保留当前视口的排队请求，其余请出队列（"清废单"）。

        滚动/重排/随机洗牌后，堆里会残留按旧行号入队的小优先级请求（项
        已滚出视口）——它们的优先级比当前视口请求还小，会把解码能力耗在
        没人看的地方，预热（WARMUP_PRIO）也永远排不上。TileView 每次绘制
        后用当前可见键调这里；被请出的项之后由预热或再次进入视口时的
        request 重新排队，在途任务照常完成并入缓存，不浪费已开始的解码。
        """
        dropped = False
        with self._lock:
            for key in [k for k, p in self._key_prio.items()
                        if p < WARMUP_PRIO]:
                if key not in visible_keys:
                    self._pending.discard(key)
                    self._key_prio.pop(key, None)
                    dropped = True
            if dropped:
                # 重建入队序：驱逐按此序取最旧，顺序本身只影响驱逐公平性
                self._pending_order.clear()
                self._pending_order.extend(self._pending)

    def queued_count(self) -> int:
        """排队（尚未开始解码）的请求数。

        预热用它做水位节流：把队列补到目标水位为止，吞吐自动等于
        worker 的实际消化速度，而不是按固定批量盲喂。"""
        with self._lock:
            return len(self._pending)

    def set_video_headroom(self, enabled: bool) -> None:
        """播放器不可见时放开 gated 的第 2 路通用视频解码线程。

        视频抓帧是最重的解码负载：正在播放时保持通用线程单路，不与播放
        抢 CPU；播放器隐藏/关闭后放开，滚动视口最多三路解码
        （通用×2 + 视口专用×1），纯预热也有两路。gated 线程空挂时不占
        CPU、不持有 mpv 实例，开关本身零成本。
        """
        with self._lock:
            if self._vid_extra_enabled == enabled:
                return
            self._vid_extra_enabled = enabled
            self._prio_wake.set()

    def set_playback_active(self, active: bool) -> None:
        """视频正在播放 → 后台视频抓帧整体让路。

        每路抓帧是一个 mpv 实例（硬解 + 解码线程），播放期间多路并发
        与播放的解码抢 GPU/CPU——实测播放卡顿的主因。置 True 后：
        - worker 不再取视频单，出队后、开解码前再查一次（可中断第二张）；
        - request() 拒收新的视频单（见其注释，防队列水位被占死）；
        - 图片缩略图（Pillow，线程已降 BELOW_NORMAL）照常出。
        暂停/停止/关闭后置 False，排队的视频单自动恢复消化。
        """
        with self._lock:
            if self._playback_active == active:
                return
            self._playback_active = active
            self._prio_wake.set()

    def invalidate_queue(self) -> None:
        """Drop interest in everything queued so far (folder changed).

        Also forgets past failures, so a transient problem (a file still being copied,
        a network hiccup) is retried on the next visit or on F5 rather than leaving a
        permanently blank tile for the rest of the session.
        """
        with self._lock:
            self._generation += 1
            self._pending.clear()
            self._pending_order.clear()
            self._key_prio.clear()
            self._failed.clear()
            # 清空待取件堆：换文件夹后旧请求（即便未被驱逐）不再解码
            self._prio_heap.clear()

    def trim_memory(self, keep: int = MEMORY_CACHE_MAX) -> None:
        """Evict least-recently-used thumbnails down to `keep`."""
        with self._mem_lock:
            while len(self._memory) > max(0, keep):
                self._memory.popitem(last=False)

    def _store(self, key: str, image: QImage) -> None:
        with self._mem_lock:
            self._memory[key] = image
            self._memory.move_to_end(key)
            while len(self._memory) > MEMORY_CACHE_MAX:
                self._memory.popitem(last=False)

    def shutdown(self) -> None:
        # Cancel what is queued, let in-flight grabs finish (they are sub-second), then
        # tear the libmpv instances down from here rather than at interpreter exit.
        _STOPPING.set()
        self._closed = True
        self._prio_wake.set()
        for t in self._workers:
            t.join(timeout=2.0)
        shutdown_grabbers()
        metadata.save()

    # -- worker -----------------------------------------------------------

    def _prio_worker_loop(self, want_video: bool,
                          viewport_only: bool = False,
                          gated: bool = False) -> None:
        """常驻取件循环：按 (优先级, 新旧) 取请求解码。

        堆序：(priority, -seq)——priority 小者先（视口内自上而下的
        阅读顺序），同级最新请求优先（快速滚动后停住时视口插队）。
        视频线程只取视频项（解码重、通用视频线程 VIDEO_WORKERS=1 限流），
        图片线程跳过视频项。viewport_only 线程再跳过预热单
        （prio >= WARMUP_PRIO）——没有视口请求就一直空挂。
        gated 线程受 _vid_extra_enabled 门控：关闭时只空挂等待开关。
        _playback_active 时视频线程全体空挂（播放让路，见 set_playback_active）。
        """
        while not self._closed and not _STOPPING.is_set():
            job = None
            with self._lock:
                heap = self._prio_heap
                while heap and job is None and not (
                        gated and not self._vid_extra_enabled):
                    if want_video and self._playback_active:
                        break  # 播放中：视频单整体让路（图片单照常）
                    # 找堆里类型匹配且 (priority, -seq) 最小的项。
                    best_i = -1
                    best_key = None
                    for i, entry in enumerate(heap):
                        _prio, _neg_seq, _k, item, _gen = entry
                        if bool(item.is_video) != want_video:
                            continue
                        if viewport_only and _prio >= WARMUP_PRIO:
                            continue
                        k = (entry[0], entry[1])
                        if best_key is None or k < best_key:
                            best_key = k
                            best_i = i
                    if best_i < 0:
                        break
                    entry = heap[best_i]
                    heap[best_i] = heap[-1]
                    heap.pop()
                    if best_i < len(heap):
                        heapq.heapify(heap)
                    key, item, gen = entry[2], entry[3], entry[4]
                    # 惰性过期：该 key 已被更高优先级条目替代（或已完成/
                    # 失效）——丢弃此旧条目，继续取下一个
                    if key not in self._pending or \
                            self._key_prio.get(key) != entry[0]:
                        continue
                    job = (key, item, gen)
                if job is None:
                    self._prio_wake.clear()
            if job is None:
                self._prio_wake.wait(timeout=0.5)
                continue
            key, item, gen = job
            self._work(item, gen)

    def _disk_path(self, key: str) -> Path:
        return THUMB_DIR / key[:2] / f"{key}.jpg"

    def _work(self, item: MediaItem, gen: int) -> None:
        key = item.cache_key
        if _STOPPING.is_set():
            return
        with self._lock:
            # The folder changed while this sat in the queue. Bail before decoding
            # rather than after: the old code did the whole extraction and only then
            # noticed the result was for a folder the user had already left, which on a
            # video-heavy folder meant minutes of libmpv work for nothing.
            if gen != self._generation:
                self._pending.discard(key)
                self._key_prio.pop(key, None)
                return
            # 播放让路的第二道闸：出队到真正开始解码之间隔着前一张的
            # 1-3s，播放开始后才轮到的任务在这里放弃——不再对大文件启动
            # 深度 seek/硬解码与起播抢 I/O（实测长视频打开卡顿数秒的来源；
            # 在途那张无法中断，做完即止）。播放结束后的视口重绘和预热
            # 轮询会把它重新排队，不丢。
            if item.is_video and self._playback_active:
                self._pending.discard(key)
                self._key_prio.pop(key, None)
                return
        if item.is_video:
            with self._lock:
                self._video_inflight += 1
        try:
            self._work_inner(item, gen, key)
        finally:
            if item.is_video:
                with self._lock:
                    self._video_inflight = max(0, self._video_inflight - 1)

    def _backfill_metadata(self, item: MediaItem) -> bool:
        """Learn duration/resolution for an item whose thumbnail was already cached.

        The thumbnail and metadata caches are separate files and can drift apart (one
        deleted, one kept). Without this, such a file keeps its cover but shows no
        duration badge and sorts as zero-length forever.
        """
        if item.is_video:
            if item.duration is not None:
                return False
            remote = netpath.is_remote(item.path)
            gate = _REMOTE_GATE if remote else None
            if gate is not None:
                gate.acquire()
            try:
                dur, w, h = _thread_grabber().open(
                    str(item.path), timeout=REMOTE_LOAD_TIMEOUT if remote else 25.0
                )
            except Exception:
                _thread_grabber()._reset()
                return False
            finally:
                if gate is not None:
                    gate.release()
            if dur is None and w is None:
                return False
            item.duration, item.width, item.height = dur, w, h
        else:
            if item.width is not None:
                return False
            try:
                # PIL reads only the header here, so this costs almost nothing
                with Image.open(item.path) as im:
                    item.width, item.height = im.size
            except Exception:
                return False
        metadata.store(item)
        return True

    def _work_inner(self, item: MediaItem, gen: int, key: str) -> None:
        try:
            img = self._load_from_disk(key)
            meta_found = False
            if img is not None:
                meta_found = self._backfill_metadata(item)
            if img is None:
                img, dur, w, h = self._generate(item)
                if img is not None:
                    self._save_to_disk(key, img)
                    if item.is_video:
                        item.duration, item.width, item.height = dur, w, h
                    else:
                        item.width, item.height = w, h
                    metadata.store(item)
                    meta_found = True
            if img is None:
                with self._lock:
                    self._pending.discard(key)
                    self._key_prio.pop(key, None)
                    self._failed[key] = self._failed.get(key, 0) + 1
                    gave_up = self._failed[key] >= MAX_ATTEMPTS
                if gave_up:
                    _log_giveup(item)
                return
            qimg = pil_to_qimage(img)
            with self._lock:
                stale = gen != self._generation
                self._pending.discard(key)
                self._key_prio.pop(key, None)
            self._store(key, qimg)
            if meta_found:
                self.meta_ready.emit(key, (item.duration, item.width, item.height))
            if not stale:
                self.ready.emit(key, qimg)
        except Exception:
            with self._lock:
                self._pending.discard(key)
                self._key_prio.pop(key, None)
                self._failed[key] = self._failed.get(key, 0) + 1
                gave_up = self._failed[key] >= MAX_ATTEMPTS
            if gave_up:
                _log_giveup(item)

    def _load_from_disk(self, key: str) -> Image.Image | None:
        p = self._disk_path(key)
        if not p.exists():
            return None
        try:
            with Image.open(p) as im:
                return im.convert("RGB")
        except Exception:
            try:
                p.unlink()
            except OSError:
                pass
            return None

    def _save_to_disk(self, key: str, img: Image.Image) -> None:
        p = self._disk_path(key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            img.convert("RGB").save(p, "JPEG", quality=84, optimize=False)
        except Exception:
            pass

    def _generate(
        self, item: MediaItem
    ) -> tuple[Image.Image | None, float | None, int | None, int | None]:
        if item.is_video:
            frame, dur, w, h = _thread_grabber().grab(str(item.path), VIDEO_SEEK_FRACTION)
            if frame is None:
                return None, None, None, None
            return self._fit(frame), dur, w, h
        try:
            with Image.open(item.path) as im:
                im = ImageOps.exif_transpose(im)
                w, h = im.size
                im = im.convert("RGB")
                return self._fit(im), None, w, h
        except Exception:
            return None, None, None, None

    @staticmethod
    def _fit(img: Image.Image) -> Image.Image:
        out = img.copy()
        out.thumbnail((GRID_THUMB_MAX, GRID_THUMB_MAX), Image.LANCZOS)
        return out


# --------------------------------------------------------- seek-bar previewer


class FramePreviewer(QObject):
    """On-demand frame extraction for seek-bar hover, latest-request-wins."""

    frame_ready = Signal(float, QImage)  # timestamp, small frame
    PREVIEW_HEIGHT = 128

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._q: queue.Queue = queue.Queue()
        self._path: str | None = None
        self._cache: dict[int, QImage] = {}
        self._alive = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="seek-preview")
        self._thread.start()

    def set_media(self, path: str | Path | None) -> None:
        path = str(path) if path else None
        if path != self._path:
            self._path = path
            self._cache.clear()
            # flush stale requests
            while not self._q.empty():
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break

    def cached(self, seconds: float) -> QImage | None:
        return self._cache.get(self._bucket(seconds))

    def request(self, seconds: float) -> None:
        if not self._path:
            return
        if self._bucket(seconds) in self._cache:
            return
        self._q.put((self._path, seconds))

    def stop(self) -> None:
        self._alive = False
        self._q.put(None)
        self._thread.join(timeout=4.0)

    @staticmethod
    def _bucket(seconds: float) -> int:
        """Quantise to 5s so nearby hovers reuse one frame."""
        return int(seconds // 5)

    IDLE_RELEASE_SECS = 25.0

    def _loop(self) -> None:
        grabber = MpvGrabber(tag="preview")
        while self._alive:
            try:
                job = self._q.get(timeout=self.IDLE_RELEASE_SECS)
            except queue.Empty:
                # nobody has hovered the seek bar for a while: give the memory back
                if grabber.live:
                    grabber._reset()
                continue
            if job is None:
                break
            # keep only the newest pending request
            while not self._q.empty():
                nxt = self._q.get_nowait()
                if nxt is None:
                    self._alive = False
                    break
                job = nxt
            if not self._alive or job is None:
                break
            path, seconds = job
            if path != self._path:
                continue
            bucket = self._bucket(seconds)
            if bucket in self._cache:
                continue
            remote = netpath.is_remote(path)
            try:
                grabber.open(path, timeout=REMOTE_LOAD_TIMEOUT if remote else 25.0)
                frame = grabber.frame_at(
                    bucket * 5 + 2.5, REMOTE_SEEK_TIMEOUT if remote else 6.0
                )
            except Exception:
                # _reset(), not close(): close() unregisters the grabber, so later idle
                # sweeps and the shutdown path would lose track of the instance that
                # _instance() then transparently re-creates.
                grabber._reset()
                continue
            if frame is None or path != self._path:
                continue
            frame.thumbnail((self.PREVIEW_HEIGHT * 3, self.PREVIEW_HEIGHT), Image.BILINEAR)
            qimg = pil_to_qimage(frame.convert("RGB"))
            self._cache[bucket] = qimg
            if len(self._cache) > 400:
                self._cache.pop(next(iter(self._cache)))
            self.frame_ready.emit(float(bucket * 5), qimg)
        grabber.close()
