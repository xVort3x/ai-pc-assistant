"""The contract between the assistant and its plugins (API version 1).

A plugin is a folder containing:

    plugin.json   {"id": "...", "name": "...", "version": "1.0.0",
                   "api_version": 1, "entry": "main.py", "keyword": "f"}
    main.py       def create_plugin(ctx: PluginContext) -> Plugin

Threading rules:
  * Plugin.search() runs on a worker thread. Poll request.is_cancelled().
  * Plugin.activate() runs on the UI thread. Keep it quick; start your own
    thread for long work.
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from assistant.config import PluginSettings

API_VERSION = 1


@dataclass
class SearchResult:
    title: str
    subtitle: str = ""
    score: float = 0.0                 # higher sorts first
    id: str = ""                       # plugin-defined identifier
    plugin_id: str = ""                # filled in by the search service
    data: dict[str, Any] = field(default_factory=dict)   # plugin-private payload
    action: Optional[Callable[[], None]] = None          # default Plugin.activate runs this
    keep_open: bool = False            # leave the window open after activation


@dataclass
class SearchRequest:
    query: str                         # text after any keyword prefix
    raw: str                           # exactly what the user typed
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()


@dataclass
class PluginContext:
    plugin_id: str
    logger: logging.Logger
    data_dir: Path                     # private writable folder for this plugin
    settings: "PluginSettings"
    notify: Callable[[str, str], None]  # (title, message); safe from any thread
    hide_window: Callable[[], None]


class Plugin(ABC):
    # id, name, version and keyword are overwritten from plugin.json by the manager.
    id: str = ""
    name: str = ""
    version: str = "0.0.0"
    keyword: Optional[str] = None       # "f report" routes only to the plugin owning "f"
    include_in_global: bool = True      # appear in searches without a keyword

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    def on_load(self) -> None:
        """Called once after the plugin is created."""

    def on_unload(self) -> None:
        """Called before the plugin is discarded (reload or quit)."""

    @abstractmethod
    def search(self, request: SearchRequest) -> list[SearchResult]:
        """Return results for the query. Runs on a worker thread."""

    def activate(self, result: SearchResult) -> None:
        """Handle Enter on one of this plugin's results. Runs on the UI thread."""
        if result.action is not None:
            result.action()
