"""Persistent directory listing cache with incremental revalidation.

Re-opening a tree of several hundred folders should not mean re-enumerating all of
them. Each directory is cached with its own mtime plus its file and subdirectory
lists, which buys two things:

* an **instant** first paint straight from the cache, with no filesystem I/O at all;
* a **cheap** revalidation pass, because enumerating a directory hands back every
  child's mtime for free -- so an unchanged leaf directory never has to be opened.

A directory that contains subdirectories still has to be enumerated even when its own
mtime matches, since a change deep inside it does not touch its ancestors' mtimes.
Skipping unchanged *leaves* is where the saving comes from, and for the usual layout
(one folder per album or series) that is nearly all of them.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .runtime import USERDATA_DIR

KIND_IMAGE = 0
KIND_VIDEO = 1
KIND_ARCHIVE = 2

MAX_DIRS = 20000  # keep the cache file bounded


class DirCache:
    """dir path -> [dir_mtime, [[name, size, mtime, kind], ...], [subdir name, ...]]"""

    def __init__(self) -> None:
        self._path = USERDATA_DIR / "dircache.json"
        self._lock = threading.Lock()
        self._dirty = False
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data: dict[str, list] = raw if isinstance(raw, dict) else {}
        except Exception:
            self._data = {}

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def key(path: str | Path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def get(self, folder: str | Path) -> list | None:
        with self._lock:
            return self._data.get(self.key(folder))

    def put(self, folder: str | Path, mtime: float, files: list, subdirs: list) -> None:
        with self._lock:
            self._data[self.key(folder)] = [mtime, files, subdirs]
            self._dirty = True

    def forget(self, folder: str | Path) -> None:
        with self._lock:
            if self._data.pop(self.key(folder), None) is not None:
                self._dirty = True

    def has(self, folder: str | Path) -> bool:
        return self.get(folder) is not None

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            if len(self._data) > MAX_DIRS:
                for k in list(self._data)[: len(self._data) - MAX_DIRS]:
                    del self._data[k]
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self._path)
                self._dirty = False
            except OSError:
                pass

    def stats(self) -> tuple[int, int]:
        with self._lock:
            dirs = len(self._data)
            files = sum(len(v[1]) for v in self._data.values() if len(v) > 1)
        return dirs, files


cache = DirCache()
