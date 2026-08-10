"""Shell actions shared by every context menu: reveal, open, rename, recycle.

All of these are per-file operations the user could equally do in Explorer, so they
live in one place rather than being re-implemented slightly differently in the browser,
the folder tree and the playlist panel.

Nothing here ever deletes permanently. `recycle()` goes through the shell so the
Recycle Bin gets the file and the action stays undoable; if that call is unavailable or
fails, the answer is "it did not happen", never a fallback to `os.remove`.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from pathlib import Path

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

# --- SHFileOperationW ------------------------------------------------------------

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040        # this is the flag that means "Recycle Bin"
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000  # still warn when a file is too big to be recycled


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle(paths: list[Path]) -> tuple[int, str]:
    """Send `paths` to the Recycle Bin. Returns (how many went, error message).

    The shell takes a double-null-terminated list, so the whole selection is one call —
    which also means one undo entry rather than one per file.
    """
    real = [p for p in paths if p.exists()]
    if not real:
        return 0, "文件已不存在"
    try:
        buf = "\0".join(str(p.resolve()) for p in real) + "\0\0"
        op = _SHFILEOPSTRUCTW(
            hwnd=None,
            wFunc=FO_DELETE,
            pFrom=buf,
            pTo=None,
            fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_WANTNUKEWARNING,
            fAnyOperationsAborted=False,
            hNameMappings=None,
            lpszProgressTitle=None,
        )
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as exc:  # no shell32, or the call could not be made at all
        return 0, f"无法调用回收站：{exc}"
    if rc != 0:
        return 0, f"回收站操作失败（代码 {rc}）"
    if op.fAnyOperationsAborted:
        # partial: whatever is gone is gone, the rest the user cancelled
        return sum(1 for p in real if not p.exists()), "已取消"
    return sum(1 for p in real if not p.exists()), ""


# --- opening ---------------------------------------------------------------------


def reveal(path: Path) -> None:
    """Open Explorer with `path` selected."""
    try:
        subprocess.Popen(["explorer", "/select,", str(path)])
    except Exception:
        pass


def open_folder(folder: Path) -> None:
    try:
        os.startfile(str(folder))  # noqa: S606 - this is the point of the action
    except Exception:
        pass


def open_default(path: Path) -> None:
    """Hand the file to whatever application Windows associates with it."""
    try:
        os.startfile(str(path))  # noqa: S606
    except Exception:
        pass


def copy_to_clipboard(text: str) -> None:
    cb = QApplication.clipboard()
    if cb is not None:
        cb.setText(text)


# --- renaming --------------------------------------------------------------------

_INVALID = set('<>:"/\\|?*')


def rename(parent, path: Path, is_dir: bool = False) -> Path | None:
    """Ask for a new name and apply it. Returns the new path, or None if nothing changed.

    Only the name is editable — the dialog cannot be used to move a file elsewhere,
    which is why separators are rejected rather than resolved.
    """
    label = "文件夹新名称：" if is_dir else "文件新名称："
    new_name, ok = QInputDialog.getText(parent, "重命名", label, text=path.name)
    if not ok:
        return None
    new_name = new_name.strip().rstrip(".")
    if not new_name or new_name == path.name:
        return None
    if _INVALID & set(new_name):
        QMessageBox.warning(parent, "重命名", '名称不能包含 < > : " / \\ | ? * 这些字符。')
        return None

    target = path.with_name(new_name)
    if target.exists():
        QMessageBox.warning(parent, "重命名", f"「{new_name}」已经存在。")
        return None
    try:
        os.rename(path, target)
    except OSError as exc:
        QMessageBox.warning(parent, "重命名", f"重命名失败：{exc.strerror or exc}")
        return None
    return target


def confirm_recycle(parent, paths: list[Path]) -> bool:
    if not paths:
        return False
    if len(paths) == 1:
        text = f"把「{paths[0].name}」放入回收站？"
    else:
        text = f"把选中的 {len(paths)} 个文件放入回收站？"
    box = QMessageBox(parent)
    box.setWindowTitle("删除")
    box.setText(text)
    box.setInformativeText("可以在回收站中还原。")
    box.setIcon(QMessageBox.Question)
    yes = box.addButton("放入回收站", QMessageBox.AcceptRole)
    box.addButton("取消", QMessageBox.RejectRole)
    box.setDefaultButton(yes)
    box.exec()
    return box.clickedButton() is yes
