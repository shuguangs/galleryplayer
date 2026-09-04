"""Single-instance support.

Who is the one instance is decided by a Windows named mutex
(acquire_single_instance): a kernel object, so exactly one process can own
it, a hard kill releases it automatically, and there is no stale-file
recovery to get wrong.  QLocalServer.listen() cannot be that oracle -- Qt on
Windows happily lets several servers listen on one pipe name.

The mutex owner then listens on a named pipe and receives payloads from later
launches: file paths (forwarded and played immediately) or a bare "raise"
marker (a double-clicked exe waking the running window).  Either way the
late process exits.

A client may write its payload and leave before the owner's event loop ever
accepts the connection (e.g. waking an instance that is still booting).  A
QLocalSocket accepted in UnconnectedState never emits `disconnected` -- the
signal only fires on a state *change* -- so the accept path checks the state
and finishes such connections on the spot.  Payloads that arrive before the
main window exists are buffered and drained by main() once it is ready.
"""
from __future__ import annotations

import sys
import time

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from . import startup_log

SERVER_NAME = "LocalMediaPlayer.SingleInstance"
MUTEX_NAME = "Local\\LocalMediaPlayer.SingleInstance"
# Bare launches (no file args) send this instead of a path list: the running
# instance raises its window.  A lone NUL byte cannot appear in a real payload.
RAISE_PAYLOAD = b"\x00"

_mutex_handle = None


def acquire_single_instance() -> bool:
    """Try to become the one instance.  True = we own it.

    Windows named mutex: exactly one creator per name, released by the kernel
    when the process dies (including hard kills and crashes) -- nothing to
    clean up, no race, no way for two processes to both think they are first.
    Returns True unconditionally on non-Windows or on any failure, because a
    broken lock must not stop the player from starting.
    """
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(handle)
            return False
        _mutex_handle = handle  # held for the process lifetime; kernel reclaims
        return True
    except Exception:
        return True


def forward_to_running(paths: list[str]) -> bool:
    """Hand `paths` to the running instance (or just ask it to raise, if no
    paths).  Return True if a live instance took the payload.

    The peer may not be pumping events yet (still booting): the write then
    lingers in this side's Qt buffer, and destroying the socket on return
    discards it.  So pump until the write really lands (instant in the common
    case of a running peer; at most ~1.5s for a booting one), and only then
    disconnect.  The single-instance mutex already guarantees we exit either
    way, so this wait cannot produce a second window.
    """
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(500):
        return False
    # Windows 前台授权：本进程刚被资源管理器启动，持有"设置前台"的权利
    # ——转手授予任意进程，持有窗口的实例才能真的弹到前台，否则只闪
    # 任务栏（实测）。非 Windows / 失败静默。
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
        except Exception:
            pass
    payload = "\n".join(paths).encode("utf-8") if paths else RAISE_PAYLOAD
    sock.write(payload)
    sock.flush()
    # 给对端留一点接受时间：对端还在启动时写入会滞留本端缓冲，过早销毁
    # socket 载荷才会真丢；waitForBytesWritten 对管道完成信号不可靠
    # （实测误报），只当回旋时间用，不当失败判据。
    deadline = time.monotonic() + 1.5
    while not sock.waitForBytesWritten(100):
        if sock.state() == QLocalSocket.UnconnectedState or time.monotonic() >= deadline:
            break
        QCoreApplication.processEvents()
    sock.disconnectFromServer()
    if sock.state() == QLocalSocket.ConnectedState:
        sock.waitForDisconnected(300)
    startup_log.stage(
        "single-inst",
        f"{'转发文件' if paths else '裸启动唤醒'}已投递给已有实例"
        f"{f': {paths[:3]}' if paths else ''}")
    return True


class InstanceServer(QObject):
    """Owns the local server that receives forwarded paths from later launches."""

    paths_received = Signal(list)
    raise_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        self._conns: dict = {}
        # 启动早期（主窗口还没建好、信号还没接消费者）到达的转发：
        # 信号发了没人收等于丢，先缓冲，窗口就绪后由 take_early() 补投
        self._early_paths: list[str] = []
        self._early_raise = False

    def listen(self) -> bool:
        """Start listening.  Only the mutex owner ever calls this, so a taken
        name means crash leftovers, not a live peer -- safe to reclaim."""
        if self._server.listen(SERVER_NAME):
            startup_log.stage("single-inst", "命名管道已注册（互斥体持有者）")
            return True
        startup_log.stage("single-inst", "清理崩溃残留后重新注册")
        self._server.removeServer(SERVER_NAME)
        return self._server.listen(SERVER_NAME)

    def take_early(self) -> tuple[list[str], bool]:
        """Drain payloads that arrived before the main window existed."""
        paths, self._early_paths = self._early_paths, []
        raised, self._early_raise = self._early_raise, False
        return paths, raised

    # ------------------------------------------------------------- transport

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            self._conns[conn] = b""
            conn.readyRead.connect(lambda c=conn: self._gather(c))
            conn.disconnected.connect(lambda c=conn: self._finish(c))
            # 客户端可能写完就走（唤醒一个还在启动中的实例正是这种时序）：
            # 接到手上时已非 Connected 的连接不会再有 disconnected 信号
            # （该信号只在状态变化时发），当场收尾，否则载荷永远挂着
            if conn.state() != QLocalSocket.ConnectedState:
                self._finish(conn)

    def _gather(self, conn) -> None:
        if conn not in self._conns:
            return
        self._conns[conn] += bytes(conn.readAll())

    def _finish(self, conn) -> None:
        # Read whatever is still buffered: with a fast disconnect the
        # readyRead may never fire before the socket goes away, so the
        # disconnect handler must drain the pipe itself.
        data = self._conns.pop(conn, b"") + bytes(conn.readAll())
        conn.deleteLater()
        if not data:
            return
        if data == RAISE_PAYLOAD:
            startup_log.stage("single-inst", "收到裸启动唤醒：主窗口前台化")
            self._early_raise = True
            self.raise_requested.emit()
            return
        text = data.decode("utf-8", errors="replace")
        paths = [ln for ln in text.splitlines() if ln.strip()]
        if paths:
            startup_log.stage("single-inst", f"收到转发 {len(paths)} 个路径")
            self._early_paths.extend(paths)
            self.paths_received.emit(paths)
