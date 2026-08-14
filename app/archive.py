"""Archive support: list members of a compressed archive and extract one media file.

Zip / tar (incl. .tar.gz, .tar.bz2, .tar.xz) are handled by Python's stdlib.
RAR and 7z need 7-Zip's 7z.exe (searched in the usual install locations and PATH)
because Python has no built-in decoder for them.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import media

# 7-Zip is the only external dependency, and only for .rar / .7z.
_7Z_CANDIDATES = (
    Path(r"C:\Program Files\7-Zip\7z.exe"),
    Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
)

ARCHIVE_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2",
}
# compound suffixes like "name.tar.gz"
_COMPOUND = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2")


@dataclass
class ArchiveEntry:
    name: str
    size: int
    is_dir: bool = False

    @property
    def is_media(self) -> bool:
        return not self.is_dir and media.classify_name(Path(self.name).name) is not None


def is_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(s) for s in _COMPOUND) or name.endswith(tuple(ARCHIVE_EXTS))


def _kind_of(path: Path) -> str:
    name = path.name.lower()
    if name.endswith((".zip",)):
        return "zip"
    if name.endswith((".rar",)):
        return "rar"
    if name.endswith((".7z",)):
        return "7z"
    if name.endswith((".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2")):
        return "tar"
    return ""


def find_7z() -> Path | None:
    for p in _7Z_CANDIDATES:
        if p.is_file():
            return p
    found = shutil.which("7z")
    return Path(found) if found else None


def cache_dir() -> Path:
    """Root directory for extracted archive members (configurable in Settings)."""
    from .config import settings

    custom = str(settings["archive_cache"] or "").strip()
    if custom:
        base = Path(custom)
    else:
        base = Path(tempfile.gettempdir()) / "GalleryPlayer"
    return base / "archive-cache"


def _check_password_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "password" in msg or "encrypted" in msg or "bad password" in msg


def list_archive(path: Path, password: str | None = None) -> tuple[list[ArchiveEntry], str | None]:
    """Return (entries, error). `error` is "password" when the archive is encrypted
    and the password was wrong / missing — the caller should ask for one."""
    kind = _kind_of(path)
    try:
        if kind == "zip":
            return _list_zip(path, password)
        if kind == "tar":
            return _list_tar(path), None
        if kind in ("rar", "7z"):
            return _list_7z(path, password)
        return [], f"unsupported: {path.name}"
    except zipfile.BadZipFile as exc:
        return [], str(exc)
    except RuntimeError as exc:
        if _check_password_error(exc):
            return [], "password"
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        return [], str(exc)


def _list_zip(path: Path, password: str | None) -> tuple[list[ArchiveEntry], str | None]:
    try:
        with zipfile.ZipFile(path) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
            entries = [
                ArchiveEntry(info.filename, info.file_size, info.is_dir())
                for info in zf.infolist()
            ]
        return entries, None
    except RuntimeError as exc:
        if _check_password_error(exc):
            return [], "password"
        raise


def _list_tar(path: Path) -> list[ArchiveEntry]:
    import tarfile

    with tarfile.open(path) as tf:
        return [
            ArchiveEntry(m.name, m.size, m.isdir())
            for m in tf.getmembers()
        ]


def _list_7z(path: Path, password: str | None) -> tuple[list[ArchiveEntry], str | None]:
    sz = find_7z()
    if sz is None:
        return [], "no7z"
    cmd = [str(sz), "l", "-slt", str(path)]
    if password:
        cmd.append(f"-p{password}")
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
    if proc.returncode != 0:
        err = (proc.stdout + proc.stderr).lower()
        if any(k in err for k in ("wrong password", "break signaled", "can not open encrypted")):
            return [], "password"
        return [], proc.stderr.strip() or proc.stdout.strip() or f"7z exit {proc.returncode}"
    entries: list[ArchiveEntry] = []
    cur: dict[str, str] = {}
    has_encrypted = False
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("Path ="):
            if cur:
                if cur.get("Path", "").lower() != str(path).lower():
                    _append_7z(entries, cur)
                    if cur.get("Encrypted", "").strip().lower() == "+":
                        has_encrypted = True
            cur = {"Path": line.split("=", 1)[1].strip()}
        elif line.startswith("Size ="):
            cur["Size"] = line.split("=", 1)[1].strip()
        elif line.startswith("Folder ="):
            cur["Folder"] = line.split("=", 1)[1].strip()
        elif line.startswith("Encrypted ="):
            cur["Encrypted"] = line.split("=", 1)[1].strip()
    if cur and cur.get("Path", "").lower() != str(path).lower():
        _append_7z(entries, cur)
        if cur.get("Encrypted", "").strip().lower() == "+":
            has_encrypted = True
    # without a password 7z may still list names; force a password prompt
    if not password and has_encrypted:
        return [], "password"
    return entries, None


def _append_7z(entries: list[ArchiveEntry], cur: dict[str, str]) -> None:
    try:
        size = int(cur.get("Size", "0") or 0)
    except ValueError:
        size = 0
    entries.append(
        ArchiveEntry(
            cur.get("Path", ""),
            size,
            cur.get("Folder", "").strip().lower() == "+",
        )
    )


def extract_member(
    path: Path, member: str, dest_dir: Path, password: str | None = None
) -> Path:
    """Extract a single member into `dest_dir`, preserving its basename. Returns
    the extracted file's path. Raises RuntimeError("password") on a wrong password."""
    kind = _kind_of(path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / Path(member).name
    if kind == "zip":
        with zipfile.ZipFile(path) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
            try:
                with zf.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            except RuntimeError as exc:
                if _check_password_error(exc):
                    raise RuntimeError("password") from exc
                raise
    elif kind == "tar":
        import tarfile

        with tarfile.open(path) as tf:
            m = tf.getmember(member)
            src = tf.extractfile(m)
            if src is None:
                raise RuntimeError(f"cannot extract {member}")
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    else:  # rar / 7z
        sz = find_7z()
        if sz is None:
            raise RuntimeError("no7z")
        dest_dir.mkdir(parents=True, exist_ok=True)
        cmd = [str(sz), "e", str(path), member, f"-o{dest_dir}", "-y"]
        if password:
            cmd.append(f"-p{password}")
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=300)
        if proc.returncode != 0:
            err = (proc.stdout + proc.stderr).lower()
            if any(k in err for k in ("wrong password", "break signaled", "can not open encrypted")):
                raise RuntimeError("password")
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    if not target.exists():
        raise RuntimeError(f"extracted file missing: {target}")
    return target
