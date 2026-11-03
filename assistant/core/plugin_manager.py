"""Discovers, loads, unloads and reloads plugins. A broken plugin never stops the app."""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QObject, Signal

from assistant.core.plugin_api import API_VERSION, Plugin, PluginContext, SearchResult

log = logging.getLogger(__name__)

ContextFactory = Callable[[str], PluginContext]


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str = "0.0.0"
    api_version: int = API_VERSION
    entry: str = "main.py"
    keyword: Optional[str] = None
    description: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "PluginManifest":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("plugin.json must contain an object")
        plugin_id = str(raw.get("id", "")).strip()
        if not plugin_id or not all(c.isalnum() or c in "-_." for c in plugin_id):
            raise ValueError("'id' is required and may only contain letters, digits, - _ .")
        manifest = cls(
            id=plugin_id,
            name=str(raw.get("name") or plugin_id),
            version=str(raw.get("version", "0.0.0")),
            api_version=int(raw.get("api_version", API_VERSION)),
            entry=str(raw.get("entry", "main.py")),
            keyword=(str(raw["keyword"]).strip() or None) if raw.get("keyword") else None,
            description=str(raw.get("description", "")),
        )
        if manifest.keyword and " " in manifest.keyword:
            raise ValueError("'keyword' must not contain spaces")
        return manifest


class PluginManager(QObject):
    plugins_changed = Signal()

    def __init__(
        self,
        search_dirs: Iterable[Path],
        context_factory: ContextFactory,
        disabled: Iterable[str] = (),
    ) -> None:
        super().__init__()
        self._dirs = [Path(d) for d in search_dirs]
        self._context_factory = context_factory
        self._disabled = set(disabled)
        self._plugins: dict[str, Plugin] = {}
        self._modules: dict[str, str] = {}      # plugin id -> module name
        self.errors: dict[str, str] = {}        # folder name -> message

    # ---------------------------------------------------------------- queries
    def active_plugins(self) -> list[Plugin]:
        return list(self._plugins.values())

    def get(self, plugin_id: str) -> Optional[Plugin]:
        return self._plugins.get(plugin_id)

    @property
    def search_dirs(self) -> list[Path]:
        return list(self._dirs)

    # ---------------------------------------------------------------- loading
    def load_all(self) -> None:
        self.errors.clear()
        for directory in self._dirs:
            if not directory.is_dir():
                continue
            for folder in sorted(directory.iterdir()):
                if not folder.is_dir() or folder.name.startswith(("_", ".")):
                    continue
                if not (folder / "plugin.json").exists():
                    continue
                self._load_one(folder)
        log.info("Loaded %d plugin(s), %d error(s)", len(self._plugins), len(self.errors))
        self.plugins_changed.emit()

    def _load_one(self, folder: Path) -> None:
        try:
            manifest = PluginManifest.from_file(folder / "plugin.json")
            if manifest.api_version != API_VERSION:
                raise ValueError(
                    f"needs plugin API {manifest.api_version}, app provides {API_VERSION}")
            if manifest.id in self._disabled:
                log.info("Plugin %s is disabled", manifest.id)
                return
            if manifest.id in self._plugins:
                raise ValueError(f"duplicate plugin id '{manifest.id}'")

            entry = (folder / manifest.entry).resolve()
            if folder.resolve() not in entry.parents:
                raise ValueError("'entry' must be inside the plugin folder")
            if not entry.is_file():
                raise ValueError(f"entry file '{manifest.entry}' not found")

            module_name = f"aipc_plugin_{manifest.id.replace('.', '_').replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(
                module_name, entry, submodule_search_locations=[str(folder)])
            if spec is None or spec.loader is None:
                raise ValueError("could not create import spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
                factory = getattr(module, "create_plugin", None)
                if not callable(factory):
                    raise ValueError("entry module has no create_plugin(ctx) function")
                instance = factory(self._context_factory(manifest.id))
                if not isinstance(instance, Plugin):
                    raise ValueError("create_plugin() must return a Plugin")
                instance.id = manifest.id
                instance.name = manifest.name
                instance.version = manifest.version
                if manifest.keyword:
                    instance.keyword = manifest.keyword
                instance.on_load()
            except Exception:
                self._purge_module(module_name)
                raise
            self._plugins[manifest.id] = instance
            self._modules[manifest.id] = module_name
            log.info("Loaded plugin %s v%s", manifest.id, manifest.version)
        except Exception as exc:  # noqa: BLE001 - isolate every plugin failure
            self.errors[folder.name] = f"{type(exc).__name__}: {exc}"
            log.exception("Failed to load plugin folder %s", folder)

    @staticmethod
    def _purge_module(module_name: str) -> None:
        for name in [n for n in sys.modules if n == module_name or n.startswith(module_name + ".")]:
            del sys.modules[name]

    # -------------------------------------------------------------- unloading
    def unload_all(self) -> None:
        for plugin_id, plugin in list(self._plugins.items()):
            try:
                plugin.on_unload()
            except Exception:  # noqa: BLE001
                log.exception("Plugin %s failed in on_unload", plugin_id)
            self._purge_module(self._modules.pop(plugin_id, ""))
        self._plugins.clear()
        self.plugins_changed.emit()

    def reload(self) -> None:
        self.unload_all()
        self.load_all()

    # ------------------------------------------------------------- activation
    def activate(self, result: SearchResult) -> bool:
        """Run a result's action on the UI thread. Returns False if it failed."""
        plugin = self._plugins.get(result.plugin_id)
        if plugin is None:
            log.warning("Result from unknown plugin '%s'", result.plugin_id)
            return False
        try:
            plugin.activate(result)
            return True
        except Exception:  # noqa: BLE001
            log.exception("Plugin %s failed while activating '%s'", result.plugin_id, result.title)
            return False
