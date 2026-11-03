"""Global hotkey using the Win32 RegisterHotKey API (no keyboard hook, no extra packages)."""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

_MODIFIERS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
              "shift": MOD_SHIFT, "win": MOD_WIN, "super": MOD_WIN}
_NAMED_KEYS = {"space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "escape": 0x1B}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'F9' -> (0, 0x78);  'Ctrl+Alt+Space' -> (MOD_CONTROL|MOD_ALT, 0x20)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mod_names, key = parts
    modifiers = 0
    for name in mod_names:
        if name not in _MODIFIERS:
            raise ValueError(f"unknown modifier '{name}'")
        modifiers |= _MODIFIERS[name]
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    elif len(key) == 1 and key.isalnum() and key.isascii():
        vk = ord(key.upper())
    else:
        raise ValueError(f"unknown key '{key}'")
    return modifiers, vk


class _HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, hotkey_id: int, callback) -> None:
        super().__init__()
        self._id = hotkey_id
        self._callback = callback
        if IS_WINDOWS:
            import ctypes.wintypes
            self._msg_type = ctypes.wintypes.MSG

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt naming)
        if IS_WINDOWS and bytes(event_type) == b"windows_generic_MSG":
            msg = self._msg_type.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self._id:
                self._callback()
                return True, 0
        return False, 0


class HotkeyManager(QObject):
    triggered = Signal()

    _HOTKEY_ID = 0xA11C

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._filter = _HotkeyFilter(self._HOTKEY_ID, self._on_hotkey)
        self._registered = False
        self._app = None

    def _on_hotkey(self) -> None:
        # Leave the native event filter before touching any UI.
        QTimer.singleShot(0, self.triggered.emit)

    def register(self, spec: str, app) -> bool:
        """Register the hotkey. Returns False (and logs why) if that is not possible."""
        self.unregister()
        if not IS_WINDOWS:
            log.warning("Global hotkeys are only implemented on Windows; skipping '%s'", spec)
            return False
        try:
            modifiers, vk = parse_hotkey(spec)
        except ValueError as exc:
            log.error("Invalid hotkey '%s': %s", spec, exc)
            return False

        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        if not user32.RegisterHotKey(None, self._HOTKEY_ID, modifiers | MOD_NOREPEAT, vk):
            log.error("RegisterHotKey('%s') failed, Windows error %d (key already in use?)",
                      spec, ctypes.get_last_error())
            return False

        app.installNativeEventFilter(self._filter)
        self._app = app
        self._registered = True
        log.info("Global hotkey '%s' registered", spec)
        return True

    def unregister(self) -> None:
        if not self._registered:
            return
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey(None, self._HOTKEY_ID)
        if self._app is not None:
            self._app.removeNativeEventFilter(self._filter)
        self._registered = False
        log.info("Global hotkey unregistered")
