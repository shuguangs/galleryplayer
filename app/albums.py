"""User-created albums: cross-folder collections persisted in userdata/albums.json.

An album is just an ordered list of file paths, so the same file can appear in several
albums and nothing on disk is touched. Entries whose file has since disappeared are
kept (not silently dropped) and shown greyed out, because a missing file is usually an
unplugged drive rather than a deletion.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .runtime import USERDATA_DIR
from .i18n import t

DEFAULT_ALBUM = t("albums.default")


class AlbumStore:
    def __init__(self) -> None:
        self._path = USERDATA_DIR / "albums.json"
        self._lock = threading.Lock()
        self._dirty = False
        self._albums: list[dict] = []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            albums = raw.get("albums") if isinstance(raw, dict) else None
            if isinstance(albums, list):
                for a in albums:
                    if isinstance(a, dict) and isinstance(a.get("name"), str):
                        paths = [p for p in a.get("paths", []) if isinstance(p, str)]
                        self._albums.append({"name": a["name"], "paths": paths})
        except Exception:
            self._albums = []
        if not self._albums:
            self._albums.append({"name": DEFAULT_ALBUM, "paths": []})
            self._dirty = True

    # -- queries ----------------------------------------------------------

    def names(self) -> list[str]:
        with self._lock:
            return [a["name"] for a in self._albums]

    def paths(self, name: str) -> list[str]:
        with self._lock:
            for a in self._albums:
                if a["name"] == name:
                    return list(a["paths"])
        return []

    def _find(self, name: str) -> dict | None:
        for a in self._albums:
            if a["name"] == name:
                return a
        return None

    # -- mutations --------------------------------------------------------

    def create(self, base: str = "") -> str:
        base = base or t("albums.new")
        with self._lock:
            existing = {a["name"] for a in self._albums}
            name = base
            n = 2
            while name in existing:
                name = f"{base} {n}"
                n += 1
            self._albums.append({"name": name, "paths": []})
            self._dirty = True
        return name

    def rename(self, old: str, new: str) -> bool:
        new = new.strip()
        if not new:
            return False
        with self._lock:
            if any(a["name"] == new for a in self._albums if a["name"] != old):
                return False
            album = self._find(old)
            if album is None:
                return False
            album["name"] = new
            self._dirty = True
        return True

    def delete(self, name: str) -> None:
        with self._lock:
            self._albums = [a for a in self._albums if a["name"] != name]
            if not self._albums:
                self._albums.append({"name": DEFAULT_ALBUM, "paths": []})
            self._dirty = True

    def add(self, name: str, paths: list[str | Path]) -> int:
        """Append paths that are not already present. Returns how many were added."""
        with self._lock:
            album = self._find(name)
            if album is None:
                return 0
            seen = {os.path.normcase(p) for p in album["paths"]}
            added = 0
            for p in paths:
                text = str(p)
                key = os.path.normcase(text)
                if key in seen:
                    continue
                seen.add(key)
                album["paths"].append(text)
                added += 1
            if added:
                self._dirty = True
            return added

    def remove(self, name: str, paths: list[str | Path]) -> None:
        drop = {os.path.normcase(str(p)) for p in paths}
        with self._lock:
            album = self._find(name)
            if album is None:
                return
            before = len(album["paths"])
            album["paths"] = [
                p for p in album["paths"] if os.path.normcase(p) not in drop
            ]
            if len(album["paths"]) != before:
                self._dirty = True

    def set_order(self, name: str, paths: list[str]) -> None:
        with self._lock:
            album = self._find(name)
            if album is not None:
                album["paths"] = list(paths)
                self._dirty = True

    def prune_missing(self, name: str) -> int:
        with self._lock:
            album = self._find(name)
            if album is None:
                return 0
            keep = [p for p in album["paths"] if Path(p).exists()]
            removed = len(album["paths"]) - len(keep)
            if removed:
                album["paths"] = keep
                self._dirty = True
            return removed

    # -- persistence ------------------------------------------------------

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps({"albums": self._albums}, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
                tmp.replace(self._path)
                self._dirty = False
            except OSError:
                pass


albums = AlbumStore()


class OrderStore:
    """Manual playlist order per folder, so a hand-sorted folder survives a restart."""

    def __init__(self) -> None:
        self._path = USERDATA_DIR / "orders.json"
        self._lock = threading.Lock()
        self._dirty = False
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data: dict[str, list[str]] = raw if isinstance(raw, dict) else {}
        except Exception:
            self._data = {}

    @staticmethod
    def _key(folder: str | Path) -> str:
        return os.path.normcase(os.path.abspath(str(folder)))

    def get(self, folder: str | Path) -> list[str] | None:
        with self._lock:
            return self._data.get(self._key(folder))

    def set(self, folder: str | Path, names: list[str]) -> None:
        with self._lock:
            self._data[self._key(folder)] = list(names)
            self._dirty = True

    def clear(self, folder: str | Path) -> None:
        with self._lock:
            if self._data.pop(self._key(folder), None) is not None:
                self._dirty = True

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self._path)
                self._dirty = False
            except OSError:
                pass


orders = OrderStore()
