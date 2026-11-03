"""The facade used by the CLI and any future tray application."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .llm import LLMClient, LLMError
from .logging_setup import setup_logging
from .models import PluginResult
from .plugin import PluginContext
from .registry import LoadReport, PluginRegistry
from .router import Route, Router, parse_query

GENERAL_SYSTEM_PROMPT = (
    "You are the AI PC assistant on a Windows machine. Be direct and practical. "
    "Answer in plain text without markdown headings. If the request is really about "
    "local files, recordings, study notes, downloads, or proofreading, tell the user "
    "which command to use: @files, @gp7, @homework, @downloads, or @grammar."
)


class Assistant:
    def __init__(
        self,
        config: Config | None = None,
        *,
        config_path: str | Path | None = None,
        plugin_paths: Iterable[Path] = (),
    ) -> None:
        self.config = config or Config.load(config_path)
        self.config.ensure_home()
        self.logger: logging.Logger = setup_logging(self.config.log_dir)
        self.llm = LLMClient(self.config, self.logger)
        self.ctx = PluginContext(
            config=self.config,
            llm=self.llm,
            logger=self.logger,
            cache_dir=self.config.cache_dir,
        )
        self.registry = PluginRegistry(self.config, self.ctx, self.logger)
        self.report: LoadReport = self.registry.load(extra_paths=plugin_paths)
        self.router = Router(self.registry, self.logger)

    # ------------------------------------------------------------------- ask

    def ask(self, raw: str, *, hint: str | None = None, timeout: float | None = None) -> PluginResult:
        started = time.perf_counter()
        route = self.router.route(raw, hint=hint)
        if route.plugin is not None:
            result = self.registry.execute(route.plugin, route.query, timeout=timeout)
        else:
            result = self._fallback(route)
        result.data.setdefault("routing", _route_payload(route))
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.logger.info(
            "ask chars=%d routed=%s ok=%s ms=%d",
            len(raw or ""),
            result.plugin,
            result.ok,
            result.elapsed_ms,
        )
        return result

    answer = ask

    def execute(self, plugin_id: str, raw: str, *, timeout: float | None = None) -> PluginResult:
        plugin = self.registry.resolve(plugin_id)
        if plugin is None:
            return PluginResult.failure("assistant", f"Unknown plugin: {plugin_id}")
        query = parse_query(raw, hint=plugin.id, resolve=self.registry.resolve)
        result = self.registry.execute(plugin, query, timeout=timeout)
        result.data.setdefault("routing", {"plugin": plugin.id, "score": 1.0, "reason": "forced"})
        return result

    # --------------------------------------------------------------- fallback

    def _fallback(self, route: Route) -> PluginResult:
        if not route.query.text and not route.query.options:
            return self._help("AI PC Assistant")

        if self.llm.available and route.query.text:
            try:
                text = self.llm.complete(
                    route.query.text,
                    system=GENERAL_SYSTEM_PROMPT,
                    max_tokens=700,
                    temperature=0.4,
                )
                return PluginResult(
                    plugin="assistant",
                    ok=True,
                    title="AI answer",
                    text=text,
                    confidence=0.55,
                    mode="general",
                    data={"model": self.llm.last_model},
                )
            except LLMError as exc:
                return self._help("AI unavailable", detail=str(exc))

        return self._help("AI PC Assistant")

    def _help(self, title: str, *, detail: str = "") -> PluginResult:
        lines = ["Try one of these:"]
        for plugin in self.registry.all():
            aliases = ", ".join(f"@{alias}" for alias in plugin.aliases[:3])
            lines.append(f"  {plugin.name} — {plugin.description}")
            if aliases:
                lines.append(f"    {aliases}")
        if detail:
            lines.extend(["", detail])
        return PluginResult(
            plugin="assistant",
            ok=True,
            title=title,
            text="\n".join(lines),
            confidence=0.0,
            mode="help",
            data={"warnings": self.config.warnings()},
        )

    # ---------------------------------------------------------------- reports

    def list_plugins(self) -> list[dict[str, Any]]:
        return self.registry.describe()

    def health(self) -> dict[str, Any]:
        return {
            "plugins": self.registry.health(),
            "llm": {
                "available": self.llm.available,
                "provider": self.llm.provider,
                "models": self.llm.models(),
            },
            "load": self.report.to_dict(),
            "warnings": self.config.warnings(),
        }

    def close(self) -> None:
        self.registry.close()


def _route_payload(route: Route) -> dict[str, Any]:
    return {
        "plugin": route.plugin.id if route.plugin else None,
        "score": round(route.score, 3),
        "reason": route.reason,
        "alternatives": route.alternatives,
    }
