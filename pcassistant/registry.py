"""Plugin registry and bounded execution.

Timeouts use a thread pool.  Python cannot forcibly kill a thread, so the
contract is two-layered:

1. The host returns a timeout result to the user at ``timeout_seconds``.
2. Plugins check ``ctx.deadline`` inside long loops and stop themselves.

A future version can move untrusted plugins to a subprocess for hard
isolation without changing the public plugin API.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import Config
from .loader import discover_builtin, discover_external
from .models import Health, PluginResult, Query
from .plugin import Plugin, PluginContext


@dataclass(slots=True)
class LoadReport:
    loaded: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"loaded": list(self.loaded), "failed": list(self.failed)}


class PluginRegistry:
    def __init__(self, config: Config, ctx: PluginContext, logger: logging.Logger) -> None:
        self.config = config
        self.ctx = ctx
        self.logger = logger
        self._plugins: dict[str, Plugin] = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pcassistant-plugin")
        self.report = LoadReport()

    # ------------------------------------------------------------------- load

    def load(self, *, extra_paths: Iterable[Path] = ()) -> LoadReport:
        classes, errors = discover_builtin(self.logger)
        self.report.failed.extend(errors)

        external, external_errors = discover_external(extra_paths, self.logger)
        self.report.failed.extend(external_errors)
        classes.extend(external)

        seen: set[str] = set()
        for plugin_class in classes:
            identifier = plugin_class.id
            if identifier in seen:
                self.report.failed.append(
                    {"module": plugin_class.__module__, "error": f"duplicate plugin id {identifier!r}"}
                )
                continue
            try:
                plugin = plugin_class(self.config)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Plugin %s failed to construct", identifier)
                self.report.failed.append(
                    {"module": plugin_class.__module__, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            seen.add(identifier)
            self._plugins[identifier] = plugin
            self.report.loaded.append(identifier)

        self.logger.info(
            "Plugins loaded=%s failed=%d", ",".join(self.report.loaded), len(self.report.failed)
        )
        return self.report

    # ---------------------------------------------------------------- queries

    def __len__(self) -> int:
        return len(self._plugins)

    def all(self) -> list[Plugin]:
        return list(self._plugins.values())

    def enabled(self) -> list[Plugin]:
        return [plugin for plugin in self._plugins.values() if self.is_enabled(plugin)]

    def is_enabled(self, plugin: Plugin) -> bool:
        return bool(self.config.get(f"plugins.{plugin.id}.enabled", plugin.enabled_by_default))

    def get(self, identifier: str) -> Plugin | None:
        if not identifier:
            return None
        key = identifier.strip().lower().replace("-", "_")
        if key in self._plugins:
            return self._plugins[key]
        for plugin in self._plugins.values():
            aliases = {alias.lower().replace("-", "_") for alias in plugin.aliases}
            if key in aliases or key == plugin.name.lower().replace(" ", "_"):
                return plugin
        return None

    def resolve(self, identifier: str) -> Plugin | None:
        """Alias of ``get`` with a clearer name for routing code."""

        return self.get(identifier)

    # --------------------------------------------------------------- execute

    def execute(
        self,
        plugin: Plugin | str,
        query: Query,
        *,
        timeout: float | None = None,
    ) -> PluginResult:
        instance = self.get(plugin) if isinstance(plugin, str) else plugin
        if instance is None:
            return PluginResult.failure("assistant", f"Unknown plugin: {plugin}")

        if not self.is_enabled(instance):
            return PluginResult.failure(
                instance.id,
                f"{instance.name} is disabled in config.json.",
                title=f"{instance.name} is disabled",
            )

        seconds = float(
            timeout
            if timeout is not None
            else self.config.number(f"plugins.{instance.id}.timeout_seconds", 30)
        )
        child_ctx = dataclasses.replace(
            self.ctx, deadline=time.monotonic() + max(1.0, seconds)
        )

        future = self._executor.submit(instance.run, query, child_ctx)
        try:
            return future.result(timeout=seconds + 0.75)
        except FutureTimeout:
            future.cancel()
            self.logger.warning("Plugin %s exceeded %.1fs", instance.id, seconds)
            return PluginResult.failure(
                instance.id,
                f"{instance.name} took longer than {seconds:.0f} seconds and was stopped. "
                "Try a narrower request.",
                title=f"{instance.name} timed out",
            )
        except Exception as exc:  # noqa: BLE001 - futures boundary
            self.logger.exception("Plugin %s raised outside its handler", instance.id)
            return PluginResult.failure(
                instance.id,
                f"{instance.name} failed unexpectedly. See the assistant log.",
            )

    # ---------------------------------------------------------------- health

    def health(self, include_disabled: bool = True) -> dict[str, dict]:
        report: dict[str, dict] = {}
        for plugin in self.all():
            if not include_disabled and not self.is_enabled(plugin):
                continue
            try:
                health: Health = plugin.health(self.ctx)
            except Exception as exc:  # noqa: BLE001
                health = Health(False, f"health check failed: {type(exc).__name__}")
            report[plugin.id] = {
                "name": plugin.name,
                "enabled": self.is_enabled(plugin),
                **health.to_dict(),
            }
        return report

    def describe(self) -> list[dict]:
        return [plugin.describe(self.ctx) for plugin in self.all()]

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
