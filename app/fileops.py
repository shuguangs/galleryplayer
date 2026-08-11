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

from .i18n import t

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
        return 0, t("fileops.gone")
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
        return 0, t("fileops.recycle_fail").format(err=exc)
    if rc != 0:
        return 0, t("fileops.recycle_error").format(code=rc)
    if op.fAnyOperationsAborted:
        # partial: whatever is gone is gone, the rest the user cancelled
        return sum(1 for p in real if not p.exists()), t("fileops.cancelled")
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


def copy_files_to_clipboard(paths: list[Path]) -> None:
    """Put real file references on the clipboard.

    Pasting in Explorer copies the files (Windows CF_HDROP via QMimeData urls).
    """
    from PySide6.QtCore import QMimeData, QUrl

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    cb = QApplication.clipboard()
    if cb is not None:
        cb.setMimeData(mime)


def copy_image_to_clipboard(path: Path) -> bool:
    """Copy the image itself (pixels) to the clipboard, e.g. to paste into a chat.

    Uses Pillow so HEIC / AVIF / JXL work too. Returns False if the file cannot
    be decoded. Animated images paste as their first frame.
    """
    from PIL import Image

    from .thumbs import pil_to_qimage  # registers HEIF/AVIF openers on import

    try:
        with Image.open(path) as im:
            mode = im.mode
            img = im.convert("RGBA") if mode in ("RGBA", "LA", "P") else im.convert("RGB")
            qimg = pil_to_qimage(img)
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setImage(qimg)
        return True
    except Exception:
        return False


# --- renaming --------------------------------------------------------------------

_INVALID = set('<>:"/\\|?*')


def rename(parent, path: Path, is_dir: bool = False) -> Path | None:
    """Ask for a new name and apply it. Returns the new path, or None if nothing changed.

    Only the name is editable — the dialog cannot be used to move a file elsewhere,
    which is why separators are rejected rather than resolved.
    """
    label = t("fileops.folder_name") if is_dir else t("fileops.file_name")
    new_name, ok = QInputDialog.getText(parent, t("fileops.rename"), label, text=path.name)
    if not ok:
        return None
    new_name = new_name.strip().rstrip(".")
    if not new_name or new_name == path.name:
        return None
    if _INVALID & set(new_name):
        QMessageBox.warning(parent, t("fileops.rename"), t("fileops.invalid_chars"))
        return None

    target = path.with_name(new_name)
    if target.exists():
        QMessageBox.warning(parent, t("fileops.rename"), t("fileops.exists").format(name=new_name))
        return None
    try:
        os.rename(path, target)
    except OSError as exc:
        QMessageBox.warning(
            parent, t("fileops.rename"), t("fileops.rename_fail").format(err=exc.strerror or exc)
        )
        return None
    return target


def confirm_recycle(parent, paths: list[Path]) -> bool:
    if not paths:
        return False
    if len(paths) == 1:
        text = t("fileops.recycle_one").format(name=paths[0].name)
    else:
        text = t("fileops.recycle_many").format(n=len(paths))
    box = QMessageBox(parent)
    box.setWindowTitle(t("fileops.delete"))
    box.setText(text)
    box.setInformativeText(t("fileops.restorable"))
    box.setIcon(QMessageBox.Question)
    yes = box.addButton(t("fileops.recycle_btn"), QMessageBox.AcceptRole)
    box.addButton(t("fileops.cancel"), QMessageBox.RejectRole)
    box.setDefaultButton(yes)
    box.exec()
    return box.clickedButton() is yes
