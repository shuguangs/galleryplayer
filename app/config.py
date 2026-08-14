"""Portable settings + per-file resume positions, stored next to the executable."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .runtime import USERDATA_DIR

DEFAULTS: dict[str, Any] = {
    "last_folder": "",
    "recent_folders": [],
    "recursive": False,
    "view_mode": "grid",          # grid | waterfall | list
    "grid_columns": 5,
    "sort_key": "name",           # name | mtime | size | duration | random
    "sort_desc": False,
    "filter_kind": "all",         # all | image | video
    "volume": 80,
    "muted": False,
    "speed": 1.0,
    "sub_font_size": 42,
    "sub_visible": True,
    "resume_enabled": True,
    "autoplay_next": False,
    "loop_mode": "off",            # off | list | one | shuffle
    # --- side panel
    "panel_visible": True,
    "panel_width": 300,
    "panel_tab": 0,                # 0 playlist, 1 albums, 2 browser
    "panel_thumb_mode": True,
    "window_geometry": "",
    "splitter_sizes": [],
    "tree_visible": True,
    "hwdec": "auto-safe",           # auto-safe | auto | auto-copy | no
    "open_native_size": True,       # 打开视频时按原始分辨率，不强制最大化
    "recent_files": [],             # 最近播放过的单个媒体文件
    # --- 截图 / GIF
    "gif_fps": 10,                  # GIF 采样帧率
    "gif_max_seconds": 15,          # 单段 GIF 最长秒数
    "gif_max_width": 480,           # GIF 缩放到的最大宽度（px）
    "capture_path": "",                # 截图/GIF 保存目录（空=自动：视频所在文件夹，不行则 exe 旁）
    "remember_scroll": True,           # 切换回之前访问过的文件夹时恢复滚动位置
    "language": "",                    # ""=未选择(首启弹窗) | zh | en
    "tree_sort_key": "name",           # 左侧目录树排序: name | mtime | size
    "tree_sort_desc": False,
    "archive_cache": "",               # 压缩包解压缓存目录（空=系统临时目录）
}

# Videos shorter than this are never resumed; nor are ones watched to the end.
RESUME_MIN_DURATION = 60.0
RESUME_MIN_POSITION = 15.0
RESUME_END_MARGIN = 15.0


class _JsonStore:
    def __init__(self, path: Path, default: Any):
        self._path = path
        self._lock = threading.Lock()
        self._dirty = False
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self._data, type(default)):
                self._data = json.loads(json.dumps(default))
        except Exception:
            self._data = json.loads(json.dumps(default))

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self._path)
            self._dirty = False


class Settings(_JsonStore):
    def __init__(self) -> None:
        super().__init__(USERDATA_DIR / "config.json", DEFAULTS)
        for k, v in DEFAULTS.items():
            self._data.setdefault(k, v)

    def __getitem__(self, key: str) -> Any:
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        if self._data.get(key) != value:
            self._data[key] = value
            self._dirty = True

    get = __getitem__


class ResumeStore(_JsonStore):
    """Maps file path -> last playback position in seconds."""

    MAX_ENTRIES = 4000

    def __init__(self) -> None:
        super().__init__(USERDATA_DIR / "resume.json", {})

    @staticmethod
    def _key(path: str | Path) -> str:
        # normcase(abspath) rather than resolve(): resolve() touches the filesystem,
        # which is a needless round trip on a network share and can fail outright when
        # the share is momentarily unreachable.
        return os.path.normcase(os.path.abspath(str(path)))

    def remember(self, path: str | Path, position: float, duration: float | None) -> None:
        key = self._key(path)
        keep = (
            duration is not None
            and duration >= RESUME_MIN_DURATION
            and position >= RESUME_MIN_POSITION
            and position <= duration - RESUME_END_MARGIN
        )
        if keep:
            self._data[key] = round(position, 2)
        elif key in self._data:
            del self._data[key]
        else:
            return
        self._dirty = True
        if len(self._data) > self.MAX_ENTRIES:
            for k in list(self._data)[: len(self._data) - self.MAX_ENTRIES]:
                del self._data[k]

    def lookup(self, path: str | Path) -> float | None:
        v = self._data.get(self._key(path))
        return float(v) if isinstance(v, (int, float)) else None

    def forget(self, path: str | Path) -> None:
        if self._data.pop(self._key(path), None) is not None:
            self._dirty = True


settings = Settings()
resume = ResumeStore()


def flush() -> None:
    settings.save()
    resume.save()
