"""Register this app in Windows' "打开方式 / Open with" list (per-user, no admin).

Setting an app as the *default* handler for an extension is deliberately gated by
Windows behind a user-confirmed picker (the UserChoice hash), so we don't try to
force it. What we can do without admin rights is add ourselves as a ProgID under
`HKCU\\Software\\Classes` and list that ProgID in each extension's OpenWithProgids,
which makes the app show up in the right-click "打开方式" menu. The user picks it
once from there (optionally ticking "always use this app").
"""
from __future__ import annotations

import sys
from pathlib import Path

PROGID = "MediaPlayer.PortableMedia"
APP_NAME = "媒体播放器"

_VIDEO = [
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".ts", ".mpg", ".mpeg", ".rmvb", ".rm", ".3gp", ".m2ts", ".vob",
]
_IMAGE = [
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".ico", ".jfif", ".avif",
]
EXTENSIONS = _VIDEO + _IMAGE


def is_supported() -> bool:
    return sys.platform.startswith("win")


def _launch_command() -> str:
    """The command Windows should run, with '%1' standing in for the file."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}" "%1"'
    py = Path(sys.executable)
    main = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{py}" "{main}" "%1"'


def _icon_ref() -> str:
    return f"{Path(sys.executable)},0"


def _exe_name() -> str:
    return Path(sys.executable).name


def register() -> None:
    """Add the app to the Open-With menu for every supported extension."""
    if not is_supported():
        raise RuntimeError("文件关联仅支持 Windows")
    import winreg

    root = winreg.HKEY_CURRENT_USER
    cmd = _launch_command()
    exe = _exe_name()

    def _set(path: str, value: str = "", name: str = "", vtype=winreg.REG_SZ) -> None:
        with winreg.CreateKey(root, path) as key:
            winreg.SetValueEx(key, name, 0, vtype, value)

    # The ProgID that describes "a media file opened by us".
    _set(rf"Software\Classes\{PROGID}", f"{APP_NAME}媒体文件")
    _set(rf"Software\Classes\{PROGID}\DefaultIcon", _icon_ref())
    _set(rf"Software\Classes\{PROGID}\shell\open\command", cmd)

    # Friendly name + command under Applications, so the entry reads nicely.
    _set(rf"Software\Classes\Applications\{exe}", APP_NAME, name="FriendlyAppName")
    _set(rf"Software\Classes\Applications\{exe}\shell\open\command", cmd)

    # List us in each extension's Open-With menu (does not steal the default).
    for ext in EXTENSIONS:
        with winreg.CreateKey(root, rf"Software\Classes\{ext}\OpenWithProgids") as key:
            winreg.SetValueEx(key, PROGID, 0, winreg.REG_NONE, b"")
        with winreg.CreateKey(root, rf"Software\Classes\Applications\{exe}\SupportedTypes") as key:
            winreg.SetValueEx(key, ext, 0, winreg.REG_SZ, "")

    _notify_shell()


def unregister() -> None:
    """Remove everything register() added."""
    if not is_supported():
        raise RuntimeError("文件关联仅支持 Windows")
    import winreg

    root = winreg.HKEY_CURRENT_USER
    exe = _exe_name()

    for ext in EXTENSIONS:
        _del_value(root, rf"Software\Classes\{ext}\OpenWithProgids", PROGID)
    _del_tree(root, rf"Software\Classes\{PROGID}")
    _del_tree(root, rf"Software\Classes\Applications\{exe}")

    _notify_shell()


def is_registered() -> bool:
    if not is_supported():
        return False
    import winreg

    try:
        winreg.CloseKey(
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROGID}")
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- helpers

def _del_value(root, path: str, name: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass


def _del_tree(root, path: str) -> None:
    """Recursively delete a registry key and its subkeys (HKCU only)."""
    import winreg

    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    sub = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _del_tree(root, path + "\\" + sub)
        winreg.DeleteKey(root, path)
    except OSError:
        pass


def _notify_shell() -> None:
    """Tell Explorer the associations changed so the menu refreshes without a reboot."""
    try:
        import ctypes

        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except Exception:
        pass
