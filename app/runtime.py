"""Runtime bootstrap. MUST be imported before anything imports `mpv`.

Puts the bundled libmpv-2.dll on the DLL search path so the app stays portable
(no system-wide mpv install required) and sets up the OpenGL surface format
that libmpv's render API expects.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_dir() -> Path:
    """Directory the app lives in, whether run from source or frozen by PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


APP_DIR = app_dir()
VENDOR_DIR = APP_DIR / "vendor"
USERDATA_DIR = APP_DIR / "userdata"


def init_libmpv() -> None:
    """Make the bundled libmpv discoverable. Raises with a clear message if absent."""
    dll = VENDOR_DIR / "libmpv-2.dll"
    if not dll.exists():
        raise RuntimeError(
            f"找不到 {dll}\n"
            "请把 libmpv-2.dll 放到 vendor 目录下。\n"
            "下载地址：https://github.com/shinchiro/mpv-winbuild-cmake/releases "
            "（选 mpv-dev-x86_64-*.7z，解压后取 libmpv-2.dll）"
        )
    os.environ["PATH"] = str(VENDOR_DIR) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(VENDOR_DIR))


def init_gl_format() -> None:
    """Must run before QApplication is constructed."""
    from PySide6.QtGui import QSurfaceFormat

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setSwapBehavior(QSurfaceFormat.DoubleBuffer)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    QSurfaceFormat.setDefaultFormat(fmt)


def userdata(*parts: str) -> Path:
    p = USERDATA_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
