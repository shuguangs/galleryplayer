"""Single-instance support.

A second launch (e.g. double-clicking a video whose "Open with" association
points at this app) must not open a second window.  Instead it forwards the
file paths to the already-running instance over a local socket and exits; the
running window then plays the file immediately.

Windows named pipes are cleaned up by the OS when a process dies, so a stale
socket after a crash cannot fool us: `listen()` failing means another live
instance owns the name (or, in a tiny race window, an instance that is still
starting up, which `forward_to_running`'s connect timeout handles).
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "LocalMediaPlayer.SingleInstance"


def forward_to_running(paths: list[str]) -> bool:
    """Hand `paths` to an existing instance.  Return True if the caller should exit.

    Blocks briefly (well under a second) so the payload is out of the pipe
    before this process quits.
    """
    if not paths:
        return False
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(500):
        return False
    payload = "\n".join(paths).encode("utf-8")
    sock.write(payload)
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    sock.waitForDisconnected(100)
    return True


class InstanceServer(QObject):
    """Owns the local server that receives forwarded paths from later launches."""

    paths_received = Signal(list)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        self._conns: dict = {}

    def listen(self) -> bool:
        """Start listening; return False only if the name is still taken."""
        if self._server.listen(SERVER_NAME):
            return True
        # Name taken: a live instance raced us, or a stale socket remained.
        # removeServer + retry once covers the latter.
        self._server.removeServer(SERVER_NAME)
        return self._server.listen(SERVER_NAME)

    # ------------------------------------------------------------- transport

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            self._conns[conn] = b""
            conn.readyRead.connect(lambda c=conn: self._gather(c))
            conn.disconnected.connect(lambda c=conn: self._finish(c))

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
        text = data.decode("utf-8", errors="replace")
        paths = [ln for ln in text.splitlines() if ln.strip()]
        if paths:
            self.paths_received.emit(paths)
