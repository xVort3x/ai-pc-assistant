"""System tray icon, menu and notifications."""
from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from assistant import APP_DISPLAY_NAME
from assistant.ui import theme


def make_app_icon() -> QIcon:
    """Draw the icon in code so there is no asset file to ship or lose."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.ACCENT))
    painter.drawRoundedRect(QRectF(2, 2, size - 4, size - 4), 14, 14)
    font = QFont(theme.FONT_FAMILY)
    font.setPixelSize(30)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "AI")
    painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    show_requested = Signal()
    reload_requested = Signal()
    open_plugins_requested = Signal()
    quit_requested = Signal()

    _notify = Signal(str, str)   # lets any thread post a notification safely

    def __init__(self, hotkey_label: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self._tray = QSystemTrayIcon(make_app_icon(), self)
        self._tray.setToolTip(f"{APP_DISPLAY_NAME} ({hotkey_label})")

        menu = QMenu()
        self._add(menu, f"Show ({hotkey_label})", self.show_requested)
        menu.addSeparator()
        self._add(menu, "Reload plugins", self.reload_requested)
        self._add(menu, "Open plugins folder", self.open_plugins_requested)
        menu.addSeparator()
        self._add(menu, "Quit", self.quit_requested)
        self._menu = menu                       # keep a reference
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)
        self._notify.connect(self._show_message)

    @staticmethod
    def _add(menu: QMenu, text: str, signal: Signal) -> None:
        action = QAction(text, menu)
        action.triggered.connect(signal)
        menu.addAction(action)

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def notify(self, title: str, message: str) -> None:
        """Safe to call from any thread."""
        self._notify.emit(title, message)

    def _show_message(self, title: str, message: str) -> None:
        self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_requested.emit()
