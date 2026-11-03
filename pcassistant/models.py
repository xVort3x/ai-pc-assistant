"""Data contracts shared by the host, the router, and every plugin.

These objects are intentionally small and serialisable.  The UI layer should
never need to know which plugin produced a result, only how to render
``title``, ``text``, ``citations`` and ``actions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ActionKind = Literal["open", "reveal", "copy", "run"]


@dataclass(frozen=True, slots=True)
class Citation:
    """A verifiable pointer back to the source of a claim.

    ``source`` is a file path or URL.  ``locator`` is a human-readable
    position inside that source (timestamp, section, page, line).
    """

    title: str
    source: str
    locator: str = ""
    snippet: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "source": self.source,
            "locator": self.locator,
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class Action:
    """Something the caller can do with a result.

    ``run`` exists for future expansion but hosts must whitelist it; the
    built-in plugins only emit ``open``, ``reveal`` and ``copy``.
    """

    kind: ActionKind
    label: str
    target: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "label": self.label, "target": self.target}


@dataclass(frozen=True, slots=True)
class Health:
    """Cheap readiness report.  Must not perform expensive indexing."""

    ok: bool
    detail: str = ""
    checked: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail, "checked": self.checked}


@dataclass(slots=True)
class PluginResult:
    """The only thing a plugin is allowed to return."""

    plugin: str
    ok: bool = True
    title: str = ""
    text: str = ""
    citations: list[Citation] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    confidence: float = 0.0
    mode: str = "default"
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    elapsed_ms: int = 0

    @classmethod
    def failure(cls, plugin: str, error: str, *, title: str = "Something went wrong") -> "PluginResult":
        return cls(plugin=plugin, ok=False, title=title, text=error, error=error)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.citations and not self.actions

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "ok": self.ok,
            "title": self.title,
            "text": self.text,
            "citations": [c.to_dict() for c in self.citations],
            "actions": [a.to_dict() for a in self.actions],
            "confidence": round(float(self.confidence), 3),
            "mode": self.mode,
            "data": self.data,
            "error": self.error,
            "elapsed_ms": int(self.elapsed_ms),
        }


@dataclass(frozen=True, slots=True)
class Query:
    """A parsed user request.

    ``raw`` keeps the original input (useful for logging and for the
    grammar plugin, which must not lose the author's exact text).
    ``text`` has the plugin prefix and CLI-style options removed.
    """

    raw: str
    text: str = ""
    hint: str | None = None
    options: dict[str, str] = field(default_factory=dict)
    tokens: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()

    def option(self, name: str, default: str = "") -> str:
        return self.options.get(name, default)

    def option_int(self, name: str, default: int) -> int:
        try:
            return int(self.options.get(name, ""))
        except (TypeError, ValueError):
            return default


def coerce_confidence(value: float) -> float:
    """Keep confidence inside the documented 0..1 range."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))
