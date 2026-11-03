"""Configuration loading and path resolution.

Resolution order (later wins):

1. Built-in defaults.
2. ``%LOCALAPPDATA%\\PCAssistant\\config.json`` (or ``$PCASSISTANT_CONFIG``).
3. Environment overrides.
4. A read-only Gemini key/model borrowed from the sibling ``palette`` config
   when the assistant has not been configured yet.

Nothing is written back automatically.  This keeps upgrades predictable and
avoids clobbering hand-edited settings.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

APP_NAME = "PCAssistant"

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "llm": {
        "provider": "gemini",
        "enabled": True,
        "api_key": "",
        "model": "gemini-2.5-flash",
        "fallback_models": ["gemini-2.0-flash", "gemini-flash-latest"],
        "timeout_seconds": 45,
        "max_retries": 2,
        "cache_ttl_seconds": 86400,
        "max_output_tokens": 900,
        "temperature": 0.3,
    },
    "paths": {
        "gp7_root": "",
        "homework_roots": [],
        "file_search_roots": [],
        "downloads": "",
    },
    "file_search": {
        "max_entries": 20000,
        "max_depth": 6,
        "cache_ttl_seconds": 3600,
        "scan_budget_seconds": 2.5,
        "default_limit": 12,
        "skip_names": [
            "AppData",
            "node_modules",
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            "$Recycle.Bin",
            "System Volume Information",
            "Windows",
            "Program Files",
            "Program Files (x86)",
            "OneDriveTemp",
        ],
    },
    "plugins": {
        "grammar_fixer": {
            "enabled": True,
            "allow_network": True,
            "tone": "neutral",
            "max_chars": 8000,
            "timeout_seconds": 60,
        },
        "gp7_search": {
            "enabled": True,
            "max_files": 1500,
            "max_results": 8,
            "timeout_seconds": 20,
        },
        "homework_search": {
            "enabled": True,
            "synthesize": True,
            "max_files": 1500,
            "max_file_bytes": 5_000_000,
            "max_results": 6,
            "timeout_seconds": 60,
        },
        "file_search": {
            "enabled": True,
            "timeout_seconds": 20,
        },
        "downloads_summarizer": {
            "enabled": True,
            "days": 7,
            "limit": 8,
            "max_chars": 12000,
            "summary_ttl_days": 30,
            "timeout_seconds": 90,
        },
    },
}


def _platform_home() -> Path:
    override = os.environ.get("PCASSISTANT_HOME", "").strip()
    if override:
        return Path(_expand(override))
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / "pcassistant"


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(str(value)))


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""

    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


class Config:
    """Typed accessors over a plain dictionary.

    Kept deliberately forgiving: a malformed plugin setting should not stop
    the assistant from launching.
    """

    def __init__(self, data: dict[str, Any] | None = None, *, path: Path | None = None) -> None:
        self.data = deep_merge(DEFAULTS, data or {})
        self.path = path
        self.home = _platform_home()
        self.data["home"] = str(self.home)

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        home = _platform_home()
        candidate = Path(path).expanduser() if path else Path(
            os.environ.get("PCASSISTANT_CONFIG", "") or (home / "config.json")
        )
        file_data = _read_json(candidate) if candidate.is_file() else {}
        merged = deep_merge(DEFAULTS, file_data)

        cls._apply_environment(merged)
        cls._borrow_palette_settings(merged)

        config = cls.__new__(cls)
        config.data = merged
        config.path = candidate if candidate.is_file() else None
        config.home = _platform_home()
        config.data["home"] = str(config.home)
        return config

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None = None) -> "Config":
        """Test/embedding helper: build from an in-memory mapping."""

        return cls(data)

    @staticmethod
    def _apply_environment(data: dict[str, Any]) -> None:
        llm = data.setdefault("llm", {})
        paths = data.setdefault("paths", {})

        key = os.environ.get("PCASSISTANT_GEMINI_KEY", "").strip()
        if key:
            llm["api_key"] = key
        model = os.environ.get("PCASSISTANT_MODEL", "").strip()
        if model:
            llm["model"] = model
        if os.environ.get("PCASSISTANT_DISABLE_AI", "").strip():
            llm["enabled"] = False

        gp7 = os.environ.get("PCASSISTANT_GP7_ROOT", "").strip()
        if gp7:
            paths["gp7_root"] = gp7
        downloads = os.environ.get("PCASSISTANT_DOWNLOADS", "").strip()
        if downloads:
            paths["downloads"] = downloads
        homework = os.environ.get("PCASSISTANT_HOMEWORK_ROOTS", "").strip()
        if homework:
            paths["homework_roots"] = [p for p in homework.split(os.pathsep) if p.strip()]

    @staticmethod
    def _borrow_palette_settings(data: dict[str, Any]) -> None:
        """Reuse the palette's existing Gemini key instead of asking twice."""

        llm = data.setdefault("llm", {})
        if str(llm.get("api_key", "")).strip():
            return

        palette_config = Path(__file__).resolve().parent.parent / "palette" / "config.json"
        if not palette_config.is_file():
            return
        borrowed = _read_json(palette_config)
        key = str(borrowed.get("geminiKey", "")).strip()
        if key:
            llm["api_key"] = key
            if borrowed.get("model"):
                llm["model"] = borrowed["model"]
            fallback = str(borrowed.get("fallbackModel", "")).strip()
            if fallback:
                models = [fallback]
                models.extend(llm.get("fallback_models", []))
                llm["fallback_models"] = list(dict.fromkeys(models))

    # --------------------------------------------------------------- access

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    def bool(self, dotted: str, default: bool = False) -> bool:
        return _truthy(self.get(dotted), default)

    def number(self, dotted: str, default: float) -> float:
        try:
            return float(self.get(dotted, default))
        except (TypeError, ValueError):
            return default

    def path_value(self, dotted: str, default: str = "") -> Path | None:
        raw = self.get(dotted, default)
        if raw is None or str(raw).strip() == "":
            return None
        return Path(_expand(str(raw)))

    def paths(self, dotted: str) -> list[Path]:
        raw = self.get(dotted, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, Iterable):
            return []
        result: list[Path] = []
        for item in raw:
            if str(item).strip():
                result.append(Path(_expand(str(item))))
        return result

    def plugin_settings(self, plugin_id: str) -> dict[str, Any]:
        value = self.get(f"plugins.{plugin_id}", {})
        return value if isinstance(value, dict) else {}

    # ---------------------------------------------------------------- paths

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def downloads_dir(self) -> Path:
        configured = self.path_value("paths.downloads")
        return configured or (Path.home() / "Downloads")

    def ensure_home(self) -> None:
        for directory in (self.home, self.cache_dir, self.log_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                # A read-only profile should not prevent a search from running.
                pass

    def warnings(self) -> list[str]:
        notes: list[str] = []
        if not self.bool("llm.enabled", True):
            notes.append("AI features are disabled (llm.enabled=false).")
        elif not str(self.get("llm.api_key", "")).strip():
            notes.append("No Gemini API key configured; AI features will use fallbacks.")
        return notes
