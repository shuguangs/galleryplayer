"""文件存在性的后台缓存：绘制路径只读缓存，绝不在 GUI 线程上 stat。

背景 bug（用户实测）：播放列表面板的行委托每画一行调一次 Path.exists()，
用来给"文件已丢失"的专辑条目标灰。本地盘上一次 stat 几十微秒无感；网络盘
（RaiDrive/CloudDrive 挂成本地盘符）上每次都是一次网络往返，面板一次重绘
几十行 → GUI 线程被连环阻塞几秒（py-spy：paint → MediaItem.exists →
Path.stat → GetFileInformationByName）。

做法：known_missing() 立即返回（未知按"存在"处理，不误标灰），未知/过期的
路径去重后交给后台线程查；结果变化时发 changed 信号让视图重画一次。
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Signal


class ExistsCache(QObject):
    changed = Signal()

    TTL_S = 60.0          # 结论有效期：过期后下次绘制再后台复查一次
    MAX_ENTRIES = 20000

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._known: dict[str, tuple[bool, float]] = {}
        self._pending: deque[str] = deque()
        self._queued: set[str] = set()
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="exists-cache",
                                        daemon=True)
        self._thread.start()

    def known_missing(self, path: Path) -> bool:
        """非阻塞：仅当后台已确认"不存在"时返回 True。"""
        key = str(path)
        now = time.monotonic()
        with self._lock:
            hit = self._known.get(key)
            if hit is not None and now - hit[1] < self.TTL_S:
                return not hit[0]
            if key not in self._queued:
                self._queued.add(key)
                self._pending.append(key)
                self._wake.set()
            return False if hit is None else not hit[0]

    def forget(self, path: Path) -> None:
        with self._lock:
            self._known.pop(str(path), None)

    def shutdown(self) -> None:
        self._stop = True
        self._wake.set()

    def _run(self) -> None:
        while not self._stop:
            self._wake.wait()
            flipped = False
            while not self._stop:
                with self._lock:
                    if not self._pending:
                        self._wake.clear()
                        break
                    key = self._pending.popleft()
                try:
                    ok = os.path.exists(key)
                except OSError:
                    ok = False
                with self._lock:
                    self._queued.discard(key)
                    prev = self._known.get(key)
                    if len(self._known) >= self.MAX_ENTRIES:
                        self._known.clear()
                    self._known[key] = (ok, time.monotonic())
                if prev is None and not ok or prev is not None and prev[0] != ok:
                    flipped = True
            if flipped and not self._stop:
                self.changed.emit()
