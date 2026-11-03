"""Application wiring and lifecycle."""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QMetaObject, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from assistant import APP_DISPLAY_NAME, APP_NAME, __version__
from assistant.config import (Settings, app_data_dir, plugin_search_dirs, user_plugins_dir)
from assistant.core.plugin_api import PluginContext, SearchResult
from assistant.core.plugin_manager import PluginManager
from assistant.core.search_service import SearchService
from assistant.logging_setup import setup_logging
from assistant.system.hotkey import HotkeyManager
from assistant.system.single_instance import SingleInstance
from assistant.ui.main_window import SearchWindow
from assistant.ui.tray import TrayController, make_app_icon

log = logging.getLogger(__name__)


class Assistant:
    """Owns every long-lived object and connects them."""

    def __init__(self, qt_app: QApplication, settings: Settings) -> None:
        self.qt_app = qt_app
        self.settings = settings
        self.hotkey_spec: str = settings.get("hotkey", "F9")

        self.window = SearchWindow()
        self.window.max_results = int(settings.get("max_results", 8))
        self.tray = TrayController(self.hotkey_spec)

        self.manager = PluginManager(
            plugin_search_dirs(), self._make_context, disabled=settings.get("disabled_plugins", []))
        self.search = SearchService(self.manager, debounce_ms=int(settings.get("debounce_ms", 120)))
        self.hotkey = HotkeyManager()

        self._connect()

    # ------------------------------------------------------------- plumbing
    def _make_context(self, plugin_id: str) -> PluginContext:
        data_dir = app_data_dir() / "plugin_data" / plugin_id
        data_dir.mkdir(parents=True, exist_ok=True)
        return PluginContext(
            plugin_id=plugin_id,
            logger=logging.getLogger(f"plugin.{plugin_id}"),
            data_dir=data_dir,
            settings=self.settings.plugin(plugin_id),
            notify=self.tray.notify,
            # Plugins may call this from any thread, so queue it onto the UI thread.
            hide_window=lambda: QMetaObject.invokeMethod(
                self.window, "hide", Qt.ConnectionType.QueuedConnection),
        )

    def _connect(self) -> None:
        w, s = self.window, self.search
        w.query_changed.connect(s.query)
        w.result_activated.connect(self._on_result_activated)
        s.search_started.connect(w.begin_search)
        s.results_ready.connect(w.add_results)
        s.search_finished.connect(w.finish_search)
        s.cleared.connect(w.clear_results)

        self.hotkey.triggered.connect(w.toggle)
        self.tray.show_requested.connect(w.summon)
        self.tray.reload_requested.connect(self.reload_plugins)
        self.tray.open_plugins_requested.connect(self._open_plugins_folder)
        self.tray.quit_requested.connect(self.qt_app.quit)
        self.manager.plugins_changed.connect(self._refresh_hint)
        self.qt_app.aboutToQuit.connect(self.shutdown)

    # ----------------------------------------------------------------- life
    def start(self, show_window: bool = False) -> None:
        self.tray.show()
        if not self.tray.available:
            log.warning("No system tray available; use --show or the hotkey to reach the window")
        if not self.hotkey.register(self.hotkey_spec, self.qt_app):
            self.tray.notify(
                f"{APP_DISPLAY_NAME}: hotkey unavailable",
                f"Could not register {self.hotkey_spec}. Another app may be using it. "
                f"Change 'hotkey' in {self.settings.path.name} or use the tray icon.")
        self.manager.load_all()
        self._report_plugin_errors()
        if show_window:
            self.window.summon()

    def shutdown(self) -> None:
        log.info("Shutting down")
        self.hotkey.unregister()
        self.search.shutdown()
        self.manager.unload_all()
        self.tray.hide()

    # -------------------------------------------------------------- actions
    def reload_plugins(self) -> None:
        self.manager.reload()
        self._report_plugin_errors()
        self.tray.notify(APP_DISPLAY_NAME, f"{len(self.manager.active_plugins())} plugin(s) loaded")

    def _report_plugin_errors(self) -> None:
        if self.manager.errors:
            names = ", ".join(self.manager.errors)
            self.tray.notify(f"{APP_DISPLAY_NAME}: plugin problems",
                             f"Could not load: {names}. See assistant.log for details.")

    def _open_plugins_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(user_plugins_dir())))

    def _refresh_hint(self) -> None:
        if self.manager.active_plugins():
            self.window.set_idle_hint("Type to search · Esc to close")
        else:
            self.window.set_idle_hint("No plugins installed yet. Tray menu > Open plugins folder")

    def _on_result_activated(self, result: SearchResult) -> None:
        if not self.manager.activate(result):
            self.tray.notify(APP_DISPLAY_NAME, f"Could not run '{result.title}'. See the log.")


def run(argv: list[str], show_window: bool = False, verbose: bool = False) -> int:
    settings = Settings()
    setup_logging(app_data_dir(), verbose)
    log.info("Starting %s %s on %s", APP_DISPLAY_NAME, __version__, sys.platform)

    qt_app = QApplication(argv)
    qt_app.setApplicationName(APP_DISPLAY_NAME)
    qt_app.setWindowIcon(make_app_icon())
    qt_app.setQuitOnLastWindowClosed(False)     # the tray keeps us alive

    guard = SingleInstance(APP_NAME)
    if not guard.acquire():
        log.info("Another instance is running; asked it to show its window")
        return 0

    assistant = Assistant(qt_app, settings)
    guard.show_requested.connect(assistant.window.summon)
    assistant.start(show_window=show_window)
    return qt_app.exec()
