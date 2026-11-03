"""Allow only one running copy per user; a second launch just asks the first to show itself."""
from __future__ import annotations

import getpass
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)


class SingleInstance(QObject):
    show_requested = Signal()

    def __init__(self, key: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001
            user = "user"
        self._name = f"{key}-{user}"
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        """True if this process is the only instance. False if another is already running
        (in which case it has been told to show its window)."""
        probe = QLocalSocket()
        probe.connectToServer(self._name)
        if probe.waitForConnected(300):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
            return False

        QLocalServer.removeServer(self._name)   # clear a stale socket from a crash
        server = QLocalServer(self)
        if not server.listen(self._name):
            log.warning("Single-instance server could not listen: %s", server.errorString())
            return True                         # better to run twice than not at all
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def _on_connection(self) -> None:
        assert self._server is not None
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            conn.waitForReadyRead(100)
            if bytes(conn.readAll()).startswith(b"show"):
                self.show_requested.emit()
            conn.disconnectFromServer()
            conn.deleteLater()
