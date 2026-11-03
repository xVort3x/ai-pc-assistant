"""Intent routing.

Three layers, in order:

1. Explicit prefix: ``@files budget``, ``/grammar ...``, ``grammar: ...``.
2. Scored plugin intent (``can_handle``), with priority as the tie-breaker.
3. No match: the assistant falls back to a general answer or help text.

Explicit invocation always wins.  A user who types ``@grammar`` should never
be second-guessed by a keyword score.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from .models import Query
from .plugin import Plugin
from .registry import PluginRegistry
from .text import tokenize

_PREFIX_RE = re.compile(r"^\s*[@/](?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*[:\-]?\s*(?P<rest>.*)$", re.DOTALL)
_COLON_RE = re.compile(r"^\s*(?P<name>[A-Za-z][A-Za-z0-9_-]{1,30})\s*:\s*(?P<rest>.*)$", re.DOTALL)
_OPTION_RE = re.compile(r"(?<!\S)--(?P<key>[A-Za-z][A-Za-z0-9_-]*)(?:=(?P<value>[^\s]+))?")

MIN_ROUTE_SCORE = 0.30
TIE_MARGIN = 0.08


@dataclass(slots=True)
class Route:
    plugin: Plugin | None
    query: Query
    score: float = 0.0
    reason: str = ""
    alternatives: list[dict] = field(default_factory=list)


def parse_query(
    raw: str,
    *,
    hint: str | None = None,
    resolve: Callable[[str], Plugin | None] | None = None,
) -> Query:
    original = (raw or "").strip()
    text = original
    explicit: str | None = hint.strip() if hint else None

    prefix = _PREFIX_RE.match(text)
    if prefix:
        candidate = prefix.group("name")
        plugin = resolve(candidate) if resolve else None
        if plugin is not None:
            explicit = plugin.id
            text = prefix.group("rest").strip()
        elif candidate.lower() in {"ask", "ai"}:
            text = prefix.group("rest").strip()
    else:
        colon = _COLON_RE.match(text)
        if colon:
            candidate = colon.group("name")
            plugin = resolve(candidate) if resolve else None
            if plugin is not None:
                explicit = plugin.id
                text = colon.group("rest").strip()

    text, options = _extract_options(text)
    return Query(
        raw=original,
        text=text.strip(),
        hint=explicit,
        options=options,
        tokens=tokenize(text, keep_stopwords=False),
    )


def _extract_options(text: str) -> tuple[str, dict[str, str]]:
    options: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    for match in _OPTION_RE.finditer(text):
        key = match.group("key").lower().replace("-", "_")
        value = match.group("value")
        options[key] = value if value is not None else "true"
        spans.append((match.start(), match.end()))

    if not spans:
        return text, options

    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    cleaned = "".join(pieces)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, options


class Router:
    def __init__(self, registry: PluginRegistry, logger: logging.Logger | None = None) -> None:
        self.registry = registry
        self.logger = logger or logging.getLogger("pcassistant.router")

    def route(self, raw: str, *, hint: str | None = None) -> Route:
        query = parse_query(raw, hint=hint, resolve=self.registry.resolve)

        if query.hint:
            plugin = self.registry.resolve(query.hint)
            if plugin is not None and self.registry.is_enabled(plugin):
                return Route(plugin, query, 1.0, "explicit")

        if not query.text and not query.options:
            return Route(None, query, 0.0, "empty query")

        scored: list[tuple[float, int, Plugin]] = []
        for plugin in self.registry.enabled():
            try:
                score = float(plugin.can_handle(query, self.registry.ctx))
            except Exception:  # noqa: BLE001 - a bad plugin must not break routing
                self.logger.exception("can_handle failed for %s", plugin.id)
                continue
            if score > 0:
                scored.append((score, plugin.priority, plugin))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        alternatives = [
            {"plugin": plugin.id, "score": round(score, 3)}
            for score, _, plugin in scored[:3]
            if score > 0
        ]

        if not scored or scored[0][0] < MIN_ROUTE_SCORE:
            return Route(None, query, scored[0][0] if scored else 0.0, "no confident match", alternatives)

        best_score, best_priority, best = scored[0]
        # A near-tie between different plugins is resolved by priority, not
        # by string length or load order.
        for score, priority, plugin in scored[1:]:
            if best_score - score > TIE_MARGIN:
                break
            if priority > best_priority:
                best, best_score, best_priority = plugin, score, priority

        return Route(best, query, best_score, "scored intent", alternatives)
