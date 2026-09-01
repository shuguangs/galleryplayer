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

# 自动化/测试模式：置 PLAYER_AUTOMATION=1 后，所有"等用户点一下"的模态
# 询问框都不再弹出，而是走各自的安全默认分支（见 automation_mode 的调用点）。
# 起因：截图/回归脚本每次启动都被"上次未正常退出，是否恢复播放列表"和
# 退出时的"实时字幕模型是否保留"挡住，必须手点才能继续。
_AUTOMATION_ENV = "PLAYER_AUTOMATION"


def automation_mode() -> bool:
    """当前是否为自动化模式（不弹任何需要用户确认的模态框）。

    每次都读环境变量而不是缓存成模块常量：测试可以在导入之后再打开它。
    """
    return os.environ.get(_AUTOMATION_ENV, "") not in ("", "0", "false", "False")


def set_automation_mode(enabled: bool) -> None:
    """供测试/脚本在进程内开关自动化模式。"""
    if enabled:
        os.environ[_AUTOMATION_ENV] = "1"
    else:
        os.environ.pop(_AUTOMATION_ENV, None)


def init_libmpv() -> None:
    """Make the bundled libmpv discoverable. Raises with a clear message if absent."""
    from .i18n import t  # deferred: i18n -> config -> runtime would be circular

    dll = VENDOR_DIR / "libmpv-2.dll"
    if not dll.exists():
        raise RuntimeError(
            t("runtime.libmpv_missing").format(dll=dll)
            + "\n"
            + t("runtime.libmpv_vendor")
            + "\n"
            + t("runtime.libmpv_download")
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
