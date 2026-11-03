"""Paths and persistent settings.

Settings live in %APPDATA%\\AIPCAssistant\\config.json on Windows
(~/.config/AIPCAssistant on other systems, which only matters for development).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from assistant import APP_NAME

log = logging.getLogger(__name__)


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def app_root() -> Path:
    """Folder that contains the bundled `plugins/` directory."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_plugins_dir() -> Path:
    path = app_data_dir() / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def plugin_search_dirs() -> list[Path]:
    return [app_root() / "plugins", user_plugins_dir()]


DEFAULTS: dict[str, Any] = {
    "hotkey": "F9",
    "debounce_ms": 120,
    "max_results": 8,
    "disabled_plugins": [],
    "plugins": {},  # per-plugin settings, keyed by plugin id
}


class Settings:
    """Small JSON-backed settings store. Thread-safe; writes are atomic."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (app_data_dir() / "config.json")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            self._save()
            return
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("top-level JSON value must be an object")
            self._data.update(loaded)
        except (OSError, ValueError) as exc:
            backup = self._path.with_suffix(".json.bad")
            log.warning("Could not read %s (%s); using defaults, saved copy as %s",
                        self._path, exc, backup)
            try:
                os.replace(self._path, backup)
            except OSError:
                pass
            self._save()

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log.error("Could not save settings to %s: %s", self._path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    def plugin(self, plugin_id: str) -> "PluginSettings":
        return PluginSettings(self, plugin_id)


class PluginSettings:
    """A plugin's private slice of the settings file."""

    def __init__(self, settings: Settings, plugin_id: str) -> None:
        self._settings = settings
        self._id = plugin_id

    def get(self, key: str, default: Any = None) -> Any:
        return self._settings.get("plugins", {}).get(self._id, {}).get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._settings._lock:
            plugins = self._settings.get("plugins", {})
            plugins.setdefault(self._id, {})[key] = value
            self._settings.set("plugins", plugins)
