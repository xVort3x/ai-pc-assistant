"""Plugin base class and execution context.

A plugin is deliberately boring: metadata, a routing score, and one handler.
The host owns timing, error isolation, logging, and result normalisation.
"""

from __future__ import annotations

import abc
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .config import Config
from .llm import LLMClient
from .models import Health, PluginResult, Query, coerce_confidence


@dataclass(slots=True)
class PluginContext:
    config: Config
    llm: LLMClient
    logger: logging.Logger
    cache_dir: Path
    deadline: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    @property
    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0

    def check_deadline(self) -> None:
        if self.expired:
            raise TimeoutError("The request exceeded its time budget.")


class Plugin(abc.ABC):
    """Base class for every built-in and third-party plugin."""

    id: ClassVar[str] = ""
    name: ClassVar[str] = "Plugin"
    description: ClassVar[str] = ""
    aliases: ClassVar[tuple[str, ...]] = ()
    keywords: ClassVar[tuple[str, ...]] = ()
    priority: ClassVar[int] = 100
    enabled_by_default: ClassVar[bool] = True
    requires_network: ClassVar[bool] = False

    def __init__(self, config: Config) -> None:
        self.config = config
        self.settings = config.plugin_settings(self.id)

    # ---------------------------------------------------------------- routing

    def can_handle(self, query: Query, ctx: PluginContext) -> float:
        """Return 0..1 confidence that this plugin should answer ``query``.

        The default is keyword based.  Plugins with distinctive intent should
        override this with a tighter rule so ordinary questions do not get
        hijacked.
        """

        text = (query.text or "").lower()
        if not text.strip():
            return 0.0

        for alias in self.aliases:
            if _contains_phrase(text, alias):
                return 0.9
        hits = sum(1 for keyword in self.keywords if _contains_phrase(text, keyword))
        if hits:
            return min(0.85, 0.30 + 0.18 * hits)
        return 0.0

    # ---------------------------------------------------------------- lifecycle

    def health(self, ctx: PluginContext) -> Health:
        return Health(True, "Ready.")

    @abc.abstractmethod
    def handle(self, query: Query, ctx: PluginContext) -> PluginResult:
        """Do the work.  May raise; the host converts failures to results."""

    # ---------------------------------------------------------------- execution

    def run(self, query: Query, ctx: PluginContext) -> PluginResult:
        started = time.perf_counter()
        try:
            result = self.handle(query, ctx)
            if not isinstance(result, PluginResult):
                raise TypeError(
                    f"{self.id}.handle() returned {type(result).__name__}, expected PluginResult"
                )
        except TimeoutError as exc:
            ctx.logger.warning("Plugin %s timed out: %s", self.id, exc)
            result = PluginResult.failure(
                self.id,
                "That took too long, so I stopped. Try a narrower request.",
                title=f"{self.name} timed out",
            )
        except Exception as exc:  # noqa: BLE001 - isolation boundary by design
            ctx.logger.exception("Plugin %s failed", self.id)
            detail = f"{type(exc).__name__}: {exc}"[:300]
            result = PluginResult.failure(
                self.id,
                f"{self.name} could not complete the request. See the assistant log for details.",
                title=f"{self.name} failed",
            )
            result.data["detail"] = detail

        result.plugin = self.id
        result.confidence = coerce_confidence(result.confidence)
        if not isinstance(result.data, dict):
            result.data = {"value": result.data}
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    # ---------------------------------------------------------------- metadata

    def describe(self, ctx: PluginContext | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "aliases": list(self.aliases),
            "keywords": list(self.keywords),
            "priority": self.priority,
            "enabled_by_default": self.enabled_by_default,
            "requires_network": self.requires_network,
        }
        if ctx is not None:
            payload["health"] = self.health(ctx).to_dict()
        return payload


def _contains_phrase(text: str, phrase: str) -> bool:
    phrase = (phrase or "").strip().lower()
    if not phrase:
        return False
    if " " in phrase:
        return phrase in text
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None
