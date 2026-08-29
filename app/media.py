"""Media discovery: what counts as media, how folders are scanned, sorted, filtered."""
from __future__ import annotations

import hashlib
import json
import os
import random
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
import threading

from natsort import natsort_keygen, ns

from . import dircache, netpath
from .runtime import USERDATA_DIR

# Everything libmpv/ffmpeg can realistically hand us, plus the odd legacy container.
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".f4v",
    ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".m2v", ".vob", ".3gp", ".3g2",
    ".rm", ".rmvb", ".asf", ".ogv", ".divx", ".mxf", ".dv", ".amv", ".nsv",
}
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".webp", ".tif",
    ".tiff", ".ico", ".avif", ".heic", ".heif", ".jxl", ".tga", ".ppm", ".pgm",
    ".pbm", ".dib", ".apng",
}
# Compound suffixes (".tar.gz" etc.) are checked by endswith in is_archive_name().
ARCHIVE_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2",
}
_ARCHIVE_COMPOUND = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2")


def is_archive_name(name: str) -> bool:
    """True for .zip/.rar/.7z/.tar(.gz|.bz2|.xz) — cheap suffix check, no I/O."""
    n = name.lower()
    return n.endswith(_ARCHIVE_COMPOUND) or n.endswith(tuple(ARCHIVE_EXTS))
# Animated images are shown in the image pane but need frame timing.
ANIMATED_EXTS = {".gif", ".webp", ".apng", ".png"}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS

# Values are i18n keys; consumers must render them via t() (see main_window.py).
SORT_LABELS = {
    "name": "media.sort_name",
    "mtime": "media.sort_mtime",
    "size": "media.sort_size",
    "duration": "media.sort_duration",
    "random": "media.sort_random",
}
FILTER_LABELS = {
    "all": "media.filter_all",
    "image": "media.filter_image",
    "video": "media.filter_video",
    "archive": "media.filter_archive",
}

_natkey = natsort_keygen(alg=ns.IGNORECASE | ns.PATH)


@dataclass
class MediaItem:
    path: Path
    is_video: bool
    size: int
    mtime: float
    # Filled in lazily by the metadata cache / thumbnail worker.
    duration: float | None = None
    is_archive: bool = False
    width: int | None = None
    height: int | None = None
    # Memoised derived values; see the properties below for why they are worth keeping.
    _sort_key: tuple | None = field(default=None, repr=False, compare=False)
    _cache_key: str | None = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def sort_key(self) -> tuple:
        """Natural-order key for this item's full path, computed at most once.

        Building one costs about 65 us -- trivial per item, ruinous in bulk: a 50 000
        item listing spent 3.3 s of pure keygen on every single sort, and the browser
        re-sorts the whole list each time a streaming scan reports progress. Holding on
        to the key turns that into 40 ms.

        `prime_sort_key()` fills this in on the scanning thread so the GUI thread never
        pays for it at all.
        """
        if self._sort_key is None:
            self._sort_key = _natkey(str(self.path))
        return self._sort_key

    def prime_sort_key(self) -> None:
        if self._sort_key is None:
            self._sort_key = _natkey(str(self.path))

    def retarget(self, path: Path) -> None:
        """Point this item at a new path (a rename), dropping what was derived from the old one."""
        self.path = path
        self._sort_key = None
        self._cache_key = None

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    @property
    def is_animated(self) -> bool:
        return not self.is_video and self.suffix in ANIMATED_EXTS

    @property
    def exists(self) -> bool:
        """Album entries can outlive their files (unplugged drive, moved folder)."""
        try:
            return self.path.exists()
        except OSError:
            return False

    @property
    def cache_key(self) -> str:
        """Identity of this exact revision of the file, for the thumbnail/metadata caches.

        Memoised because the tile view asks for it once per painted tile per repaint,
        and the model asks for every item's key on each reset.
        """
        if self._cache_key is None:
            raw = f"{str(self.path).lower()}|{self.size}|{int(self.mtime)}"
            self._cache_key = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return self._cache_key

    @property
    def aspect(self) -> float:
        if self.width and self.height:
            return self.width / self.height
        return 16 / 9 if self.is_video else 1.0

    def size_text(self) -> str:
        n = float(self.size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return ""

    def duration_text(self) -> str:
        return format_duration(self.duration) if self.duration else ""

    def resolution_text(self) -> str:
        return f"{self.width}×{self.height}" if self.width and self.height else ""


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class MetadataCache:
    """Persistent duration/resolution cache so sorting by duration isn't a re-probe."""

    def __init__(self) -> None:
        self._path = USERDATA_DIR / "metadata.json"
        try:
            self._data: dict[str, list] = json.loads(self._path.read_text("utf-8"))
        except Exception:
            self._data = {}
        self._dirty = False
        # apply/store 在扫描与缩略图线程调用，save 在 GUI 线程 dump——
        # 无锁的话 dump 期间字典扩容会抛 "dictionary changed size"
        self._lock = threading.Lock()

    def apply(self, item: MediaItem) -> bool:
        with self._lock:
            rec = self._data.get(item.cache_key)
        if not rec:
            return False
        item.duration, item.width, item.height = rec[0], rec[1], rec[2]
        return True

    def store(self, item: MediaItem) -> None:
        with self._lock:
            self._data[item.cache_key] = [item.duration, item.width, item.height]
            self._dirty = True

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps(self._data), encoding="utf-8")
                self._dirty = False
            except OSError:
                pass


metadata = MetadataCache()


def classify(path: Path) -> bool | None:
    """True = video, False = image, None = not media."""
    return classify_name(path.name)


def classify_name(name: str) -> bool | None:
    """Same as classify() but avoids building a Path per directory entry."""
    dot = name.rfind(".")
    if dot < 0:
        return None
    ext = name[dot:].lower()
    if ext in VIDEO_EXTS:
        return True
    if ext in IMAGE_EXTS:
        return False
    return None


@dataclass
class ScanStats:
    """How much of a scan came from cache — surfaced in the status bar."""

    dirs_read: int = 0
    dirs_reused: int = 0
    dirs_found: int = 0   # directories discovered so far, including ones not yet opened
    levels: int = 0       # how many levels of the tree the scan has descended
    from_cache: bool = False

    @property
    def dirs_total(self) -> int:
        return self.dirs_read + self.dirs_reused


# Enumerating a directory is latency-bound rather than CPU-bound, so a level of the
# tree is read by several threads at once. A NAS over Wi-Fi answers in tens of ms per
# request and benefits from far more overlap than a local SSD does.
SCAN_WORKERS_LOCAL = 6
SCAN_WORKERS_REMOTE = 16


def _enumerate(d: Path) -> tuple[list, list[str]]:
    """One scandir pass over `d`: its media files and its subdirectory names."""
    files: list = []
    subdirs: list[str] = []
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not e.name.startswith("."):
                            subdirs.append(e.name)
                        continue
                    kind = classify_name(e.name)
                    if kind is None:
                        kind = dircache.KIND_ARCHIVE if is_archive_name(e.name) else None
                        if kind is None:
                            continue
                    st = e.stat()
                    files.append(
                        [e.name, st.st_size, st.st_mtime,
                         dircache.KIND_VIDEO if kind is True else
                         (dircache.KIND_IMAGE if kind is False else kind)]
                    )
                except OSError:
                    continue
    except OSError:
        pass
    return files, subdirs


def _dir_mtime(d: Path) -> float | None:
    """Authoritative modification time of a directory.

    Deliberately *not* taken from the parent's `DirEntry.stat()`. Windows serves
    directory enumeration out of the metadata cached in the parent's directory entry,
    and NTFS flushes a child's timestamp there lazily — so a subdirectory that was just
    written to can keep reporting its previous mtime through `os.scandir(parent)` for
    seconds. Comparing that stale value against the cache made it match, and the
    directory was skipped: newly added files simply never showed up until some later
    scan happened to see a refreshed value. `os.stat()` on the directory itself queries
    the file system and does not have that problem.
    """
    try:
        return os.stat(d).st_mtime
    except OSError:
        return None


def _items_from_records(folder: Path, files: list) -> list[MediaItem]:
    """Turn one directory's cached records into items, ready to sort.

    Always called off the GUI thread, which is the point of priming the sort key here:
    it is by far the most expensive thing about a listing, and doing it as each
    directory lands spreads the cost over the scan instead of dropping it on the first
    repaint.
    """
    out = []
    for name, size, mtime, kind in files:
        if kind == dircache.KIND_ARCHIVE:
            item = MediaItem(folder / name, False, size, mtime, is_archive=True)
        else:
            item = MediaItem(folder / name, kind == dircache.KIND_VIDEO, size, mtime)
        metadata.apply(item)
        item.prime_sort_key()
        out.append(item)
    return out


def item_for_path(path: Path) -> MediaItem | None:
    """Build a MediaItem for a single file, tolerating one that no longer exists.

    Album entries are stored as plain paths, so this is how they become playable
    items again — including entries whose file is currently unreachable, which are
    surfaced greyed out rather than silently vanishing.
    """
    kind = classify(path)
    if kind is None and is_archive_name(path.name):
        kind = dircache.KIND_ARCHIVE
    if kind is None:
        return None
    try:
        st = path.stat()
        if kind == dircache.KIND_ARCHIVE:
            return MediaItem(path, False, st.st_size, st.st_mtime, is_archive=True)
        item = MediaItem(path, kind, st.st_size, st.st_mtime)
    except OSError:
        if kind == dircache.KIND_ARCHIVE:
            return MediaItem(path, False, 0, 0.0, is_archive=True)
        item = MediaItem(path, kind, 0, 0.0)
    metadata.apply(item)
    return item


def scan_from_cache(folder: Path, recursive: bool) -> list[MediaItem] | None:
    """Rebuild the listing purely from cache, touching the filesystem zero times.

    Level by level, like the real scan, so the instant first paint is in the same order
    the authoritative pass will produce and the list does not visibly reshuffle when it
    lands.

    Returns None when the folder has never been scanned, so the caller can fall back
    to a normal scan instead of showing an empty view.
    """
    root = dircache.cache.get(folder)
    if not root:
        return None
    items: list[MediaItem] = []
    queue: deque[tuple[Path, list]] = deque([(Path(folder), root)])
    seen: set[str] = set()

    while queue:
        d, record = queue.popleft()
        if not record or len(record) < 3:
            continue
        items.extend(_items_from_records(d, record[1]))
        if not recursive:
            break
        for sub in record[2]:
            child = d / sub
            key = dircache.DirCache.key(child)
            if key in seen:
                continue
            seen.add(key)
            child_record = dircache.cache.get(child)
            if child_record:
                queue.append((child, child_record))
    return items


def _read_dir(d: Path, use_cache: bool) -> tuple[Path, list, list[str], bool]:
    """Read one directory, on a worker thread. Returns (dir, files, subdirs, reused).

    The mtime is taken *before* enumerating: a file that lands between the two reads
    then leaves the cache looking older than the directory, so the next scan re-reads
    it. Stamping it afterwards would bake the miss in permanently.
    """
    record = dircache.cache.get(d) if use_cache else None
    cached = record if (record and len(record) >= 3) else None
    mtime: float | None = None

    # `os.stat` on Windows is a real open-and-query, so it is worth not paying for one
    # where the answer cannot be used. A directory known to have children is going to
    # be enumerated no matter what its own mtime says, so it is not stat'ed at all.
    if cached is None or not cached[2]:
        mtime = _dir_mtime(d)
        # Only a leaf may be trusted on mtime alone: adding a file deep inside a
        # directory does not change the mtime of anything above it.
        if cached is not None and mtime is not None and abs(float(cached[0]) - mtime) < 1e-6:
            return d, cached[1], cached[2], True

    files, subdirs = _enumerate(d)
    # A directory with children is never skipped on mtime, so it does not need a usable
    # one. 0.0 also covers the directory that just lost its last subdirectory: it is
    # simply enumerated once more next time, and cached properly from then on.
    stored = 0.0 if (subdirs or mtime is None) else mtime
    dircache.cache.put(d, stored, files, subdirs)
    return d, files, subdirs, False


def scan_folder_streaming(
    folder: Path,
    recursive: bool,
    use_cache: bool = True,
    emit=None,
    should_stop=None,
    stats: ScanStats | None = None,
    on_progress=None,
) -> None:
    """Level-order scan that reports each directory's media the moment it is read.

    The tree is walked breadth-first, one level fully resolved before the next begins:

    1. the opened folder is enumerated, which yields both its own media *and* the
       complete list of its subfolders in a single pass;
    2. that whole set of subfolders is then read — concurrently — and each one's media
       is handed to `emit` as it arrives;
    3. their subfolders become the next level, and so on.

    So the user sees the folder they actually opened first, then everything one level
    down, rather than one arbitrary branch followed all the way to the bottom. Note the
    subfolder list for a level is always known before any file inside it is touched:
    one scandir call returns files and subdirectories together, so this ordering costs
    nothing extra — splitting it into a separate "find the folders" pass would only
    enumerate every directory twice.

    Reading a directory is dominated by waiting on the file system, so a level is split
    across a small thread pool. That is where the speed-up on a network share comes
    from: 200 folders at 20 ms of latency each is 4 s serially and about 250 ms with 16
    requests in flight.

    `on_progress(stats)` is called as the scan advances, for callers that want to show
    how many folders have been discovered versus opened.
    """
    stats = stats if stats is not None else ScanStats()
    root = Path(folder)
    workers = SCAN_WORKERS_REMOTE if netpath.is_remote(root) else SCAN_WORKERS_LOCAL

    frontier: list[Path] = [root]
    # Junctions are not followed (`follow_symlinks=False`), but a tree reachable twice
    # by other means must still not be scanned twice.
    seen: set[str] = {dircache.DirCache.key(root)}
    stats.dirs_found = 1

    stopped = lambda: should_stop is not None and should_stop()  # noqa: E731
    pool: ThreadPoolExecutor | None = None
    next_level: list[Path] = []

    def absorb(result: tuple[Path, list, list[str], bool]) -> None:
        d, files, subdirs, reused = result
        if reused:
            stats.dirs_reused += 1
        else:
            stats.dirs_read += 1
        if emit is not None and files:
            emit(_items_from_records(d, files))
        if not recursive:
            return
        for sub in subdirs:
            child = d / sub
            key = dircache.DirCache.key(child)
            if key in seen:
                continue
            seen.add(key)
            next_level.append(child)

    try:
        while frontier and not stopped():
            stats.levels += 1
            next_level = []

            if len(frontier) == 1:
                # The common case at the top of every scan, and the whole of a
                # non-recursive one. Handing a single directory to a pool would cost
                # more in scheduling than the read itself.
                absorb(_read_dir(frontier[0], use_cache))
                if on_progress is not None:
                    on_progress(stats)
            else:
                if pool is None:
                    pool = ThreadPoolExecutor(
                        max_workers=workers, thread_name_prefix="scan"
                    )
                futures = [pool.submit(_read_dir, d, use_cache) for d in frontier]
                # Absorb each directory as it lands rather than waiting for the slowest
                # one in the level; a single unresponsive folder must not stall the UI.
                for fut in as_completed(futures):
                    if stopped():
                        for f in futures:
                            f.cancel()
                        return
                    try:
                        absorb(fut.result())
                    except Exception:
                        continue
                    if on_progress is not None:
                        on_progress(stats)

            if not recursive:
                break
            stats.dirs_found += len(next_level)
            frontier = next_level
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def scan_folder(
    folder: Path, recursive: bool, use_cache: bool = True, stats: ScanStats | None = None
) -> list[MediaItem]:
    """Collect the whole listing at once (thin wrapper over the streaming scan).

    `emit` is only ever called from the thread driving the scan, never from a pool
    worker, so no synchronisation is needed here.
    """
    items: list[MediaItem] = []
    scan_folder_streaming(folder, recursive, use_cache, items.extend, None, stats)
    return items


def apply_filter(items: list[MediaItem], flags: set[str], search: str = "") -> list[MediaItem]:
    """Filter by kind. `flags` is a subset of {"image", "video", "archive"};
    an empty set (or the full set) shows everything."""
    out = items
    if flags and flags != {"image", "video", "archive"}:
        out = [
            i for i in out
            if ("image" in flags and not i.is_video and not i.is_archive)
            or ("video" in flags and i.is_video)
            or ("archive" in flags and i.is_archive)
        ]
    if search:
        needle = search.lower()
        out = [i for i in out if needle in i.name.lower()]
    return out


def sort_items(
    items: list[MediaItem],
    key: str,
    desc: bool,
    seed: int = 0,
    manual_order: list[str] | None = None,
) -> list[MediaItem]:
    if key == "random":
        out = list(items)
        random.Random(seed).shuffle(out)
        return out
    if key == "custom":
        # Files the saved order does not mention (added since) go last, by name.
        rank = {name: i for i, name in enumerate(manual_order or [])}
        fallback = len(rank)
        ordered = sorted(
            items,
            key=lambda i: (rank.get(i.name, fallback), i.sort_key),
        )
        return list(reversed(ordered)) if desc else ordered
    if key == "mtime":
        keyfn = lambda i: i.mtime  # noqa: E731
    elif key == "size":
        keyfn = lambda i: i.size  # noqa: E731
    elif key == "duration":
        keyfn = lambda i: (i.duration if i.duration is not None else -1.0)  # noqa: E731
    else:
        keyfn = lambda i: i.sort_key  # noqa: E731
    return sorted(items, key=keyfn, reverse=desc)
