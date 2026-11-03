"""Debounced, cancellable, multi-plugin search.

Flow: query() -> debounce timer -> _dispatch() -> worker threads -> _batch signal
(queued onto the UI thread) -> results_ready. Every dispatch gets a new generation
number; batches from older generations are ignored.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QTimer, Signal

from assistant.core.plugin_api import Plugin, SearchRequest
from assistant.core.plugin_manager import PluginManager

log = logging.getLogger(__name__)


class SearchService(QObject):
    search_started = Signal(int)            # generation
    results_ready = Signal(int, str, list)  # generation, plugin id, list[SearchResult]
    search_finished = Signal(int)           # generation
    cleared = Signal()                      # query became empty

    _batch = Signal(int, str, list, str)    # internal: generation, plugin id, results, error

    def __init__(self, manager: PluginManager, debounce_ms: int = 120,
                 max_workers: int = 4, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="search")
        self._generation = 0
        self._pending = 0
        self._cancel = threading.Event()
        self._latest = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._dispatch)
        self._batch.connect(self._on_batch)

    # ------------------------------------------------------------------ API
    def query(self, text: str) -> None:
        """Call on every keystroke."""
        self._latest = text
        self._cancel.set()          # tell running searches their query is stale
        self._timer.start()

    def shutdown(self) -> None:
        self._timer.stop()
        self._cancel.set()
        self._generation += 1       # drop anything still in flight
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------- internals
    def _route(self, text: str) -> tuple[list[Plugin], str]:
        plugins = self._manager.active_plugins()
        first, sep, rest = text.partition(" ")
        if sep:
            for plugin in plugins:
                if plugin.keyword and plugin.keyword.lower() == first.lower():
                    return [plugin], rest.strip()
        return [p for p in plugins if p.include_in_global], text

    def _dispatch(self) -> None:
        text = self._latest.strip()
        self._generation += 1
        generation = self._generation
        self._cancel = token = threading.Event()

        if not text:
            self._pending = 0
            self.cleared.emit()
            return

        targets, query = self._route(text)
        self._pending = len(targets)
        self.search_started.emit(generation)
        if not targets:
            self.search_finished.emit(generation)
            return
        for plugin in targets:
            request = SearchRequest(query=query, raw=text, _cancel=token)
            self._pool.submit(self._work, generation, plugin, request)

    def _work(self, generation: int, plugin: Plugin, request: SearchRequest) -> None:
        results: list = []
        error = ""
        try:
            results = list(plugin.search(request) or [])
        except Exception as exc:  # noqa: BLE001 - a plugin must never break search
            error = f"{type(exc).__name__}: {exc}"
            log.exception("Plugin %s failed in search()", plugin.id)
        for result in results:
            result.plugin_id = plugin.id
        self._batch.emit(generation, plugin.id, results, error)

    def _on_batch(self, generation: int, plugin_id: str, results: list, error: str) -> None:
        if generation != self._generation:
            return                  # stale
        if results:
            self.results_ready.emit(generation, plugin_id, results)
        self._pending -= 1
        if self._pending <= 0:
            self.search_finished.emit(generation)
