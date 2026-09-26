"""Detect network locations (UNC paths, mapped network drives, cloud-drive mounts).

Loopback SMB is fast, but a real NAS over Wi-Fi has latency measured in tens of
milliseconds per request. Knowing a path is remote lets the player buffer much more
aggressively and stop several thumbnail workers from fighting over one link.

网盘挂载（RaiDrive / CloudDrive / rclone mount / WinFsp 系）会把网盘伪装成
本地固定盘：GetDriveType 报 DRIVE_FIXED、文件系统报 NTFS。只看驱动器类型
会把它们判成本地，网络盘保护全部失效（实测每张视频缩略图 80~100MB 流量）。
区分依据：真实磁盘（NVMe/SATA/USB/虚拟磁盘 VHD）都有底层存储设备，
IOCTL_STORAGE_QUERY_PROPERTY 能查到总线；虚拟文件系统挂载没有，查询失败，
且其设备名是 \\Device\\Volume{GUID} 而非 \\Device\\HarddiskVolumeN。
两个条件同时满足才判远程，避免误伤真实磁盘。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as W
import os
from pathlib import Path

from .i18n import t

DRIVE_REMOTE = 4
DRIVE_FIXED = 3

_cache: dict[str, bool] = {}


# ------------------------------------------------------------- 系统探测（可测替身）
def _drive_type(root: str) -> int:
    return int(ctypes.windll.kernel32.GetDriveTypeW(root))


def _dos_device(letter: str) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.QueryDosDeviceW(f"{letter}:", buf, 1024):
        raise OSError(ctypes.GetLastError(), "QueryDosDevice failed")
    return buf.value


_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400


def _has_storage_bus(letter: str) -> bool:
    """卷背后有没有真实存储设备（能查到总线类型）。只开设备、不碰文件内容。"""
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = W.HANDLE
    k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID,
                                W.DWORD, W.DWORD, W.HANDLE]
    k32.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, W.LPVOID, W.DWORD,
                                    W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD),
                                    W.LPVOID]
    h = k32.CreateFileW(f"\\\\.\\{letter}:", 0, 3, None, 3, 0, None)
    if h in (None, W.HANDLE(-1).value):
        # 打不开卷设备：权限不足等。不能据此断定是虚拟盘
        return True
    try:
        query = (ctypes.c_uint32 * 3)(0, 0, 0)  # StorageDeviceProperty / Standard
        out = ctypes.create_string_buffer(512)
        n = W.DWORD()
        return bool(k32.DeviceIoControl(h, _IOCTL_STORAGE_QUERY_PROPERTY, query, 12,
                                        out, 512, ctypes.byref(n), None))
    finally:
        k32.CloseHandle(h)


def _is_virtual_mount(letter: str) -> bool:
    dev = _dos_device(letter)
    if "\\HarddiskVolume" in dev or "\\CdRom" in dev or "\\Floppy" in dev:
        return False
    return not _has_storage_bus(letter)


def _drive_is_remote(root: str) -> bool:
    cached = _cache.get(root)
    if cached is not None:
        return cached
    try:
        dt = _drive_type(root)
        remote = dt == DRIVE_REMOTE or (dt == DRIVE_FIXED and _is_virtual_mount(root[0]))
    except Exception:
        remote = False
    _cache[root] = remote
    return remote


def is_remote(path: str | Path | None) -> bool:
    """True for \\\\server\\share paths, mapped network drives and cloud-drive mounts."""
    if not path:
        return False
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    drive, _ = os.path.splitdrive(text)
    if not drive or len(drive) != 2 or drive[1] != ":":
        return False
    return _drive_is_remote(drive.upper() + "\\")


def describe(path: str | Path | None) -> str:
    return t("netpath.remote") if is_remote(path) else t("netpath.local")
