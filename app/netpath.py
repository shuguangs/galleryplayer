"""Detect network locations (UNC paths and mapped network drives).

Loopback SMB is fast, but a real NAS over Wi-Fi has latency measured in tens of
milliseconds per request. Knowing a path is remote lets the player buffer much more
aggressively and stop several thumbnail workers from fighting over one link.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

DRIVE_REMOTE = 4

_cache: dict[str, bool] = {}


def _drive_is_remote(root: str) -> bool:
    cached = _cache.get(root)
    if cached is not None:
        return cached
    try:
        remote = ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_REMOTE
    except Exception:
        remote = False
    _cache[root] = remote
    return remote


def is_remote(path: str | Path | None) -> bool:
    """True for \\\\server\\share paths and for drive letters mapped to a network share."""
    if not path:
        return False
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    drive, _ = os.path.splitdrive(text)
    if not drive or len(drive) != 2 or drive[1] != ":":
        return False
    return _drive_is_remote(drive + "\\")


def describe(path: str | Path | None) -> str:
    return "网络位置" if is_remote(path) else "本地磁盘"
