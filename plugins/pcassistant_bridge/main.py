"""PC Assistant Bridge - desktop shell <-> plugin engine adapter.

This module is the only coupling point between the shell and the plugin
system.  It is deliberately a *thin, defensive* adapter:

* It never invokes the general chat fallback.  Free text is suggestion-only.
* Explicit intent is detected with ``parse_query(...).hint`` and nothing else.
* Every action returned to the shell is sanitised against a fixed allowlist.
* Failures become JSON payloads, never exceptions or stack traces.

Interaction model
-----------------
``preview(text)`` runs while the user types:

    explicit + file_search / gp7_search  -> execute now
    explicit + any other plugin          -> defer, requires_enter = True
    free text                            -> suggestions only

``commit(text)`` runs when the user presses Enter:

    explicit                             -> execute now
    free text                            -> suggestions only (still no chat)

The shell can echo ``submit`` from a deferred/suggestion payload back into
``handle()`` to run the exact request the user selected.

Host entry points
-----------------
In-process (preferred)::

    import main
    main.preview("budget")
    main.commit("@grammar i has went too the store")
    main.handle({"op": "commit", "text": "budget", "hint": "files"})
    main.close_engine()

Subprocess / stdio::

    echo {"op":"commit","text":"@files budget"} | python main.py --request -

Only ``pcassistant.config``, ``llm``, ``logging_setup``, ``models``, ``plugin``,
``registry`` and ``router`` are imported.  Nothing here mutates engine state.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def _add_import_roots() -> bool:
    """Make ``pcassistant`` importable for both host and stdio loading.

    Host: ``import main`` runs with the app root on ``sys.path``.
    Stdio: ``python plugins/pcassistant_bridge/main.py`` does not, so the
    repository root is derived from this file, with ``PCASSISTANT_ROOT`` and
    the current directory as fallbacks.
    """

    candidates = [
        os.environ.get("PCASSISTANT_ROOT", ""),
        str(Path(__file__).resolve().parents[2]),
        str(Path.cwd()),
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "pcassistant" / "__init__.py").is_file():
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return True
    return False


if not _add_import_roots():  # pragma: no cover - deployment guard
    raise RuntimeError(
        "PC Assistant package not found. Place this plugin beside the "
        "pcassistant/ package or set PCASSISTANT_ROOT."
    )

from pcassistant.config import Config  # noqa: E402
from pcassistant.llm import LLMClient  # noqa: E402
from pcassistant.logging_setup import setup_logging  # noqa: E402
from pcassistant.models import Query  # noqa: E402
from pcassistant.plugin import Plugin, PluginContext  # noqa: E402
from pcassistant.registry import LoadReport, PluginRegistry  # noqa: E402
from pcassistant.router import Router, parse_query  # noqa: E402

API_VERSION = 1
BRIDGE_ID = "pcassistant_bridge"
BRIDGE_VERSION = "1.0.0"

#: Plugins allowed to execute on every keystroke.
LIVE_PLUGINS = frozenset({"file_search", "gp7_search"})

#: Plugins that only execute on Enter.  Anything not in ``LIVE_PLUGINS`` is
#: treated as enter-only, so a future plugin is safe by default.
ENTER_ONLY_PLUGINS = frozenset({"grammar_fixer", "homework_search", "downloads_summarizer"})

ALLOWED_ACTION_KINDS = frozenset({"open", "reveal", "copy"})
DENIED_ACTION_KINDS = frozenset({"run"})

MAX_ACTIONS = 64
MAX_COPY_CHARS = 200_000
MAX_SUGGESTIONS = 3
PREVIEW_TIMEOUT_SECONDS = 3.0
PREVIEW_TEXT_CHARS = 240

PHASE_PREVIEW = "preview"
PHASE_COMMIT = "commit"

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Action policy
# --------------------------------------------------------------------------- #


def _field(item: Any, name: str) -> str:
    """Read ``name`` from an ``Action`` or a plain mapping, defensively."""

    if isinstance(item, Mapping):
        value = item.get(name, "")
    else:
        value = getattr(item, name, "")
    return "" if value is None else str(value)


def _safe_local_target(target: str) -> bool:
    """``open``/``reveal`` may only point at an existing absolute path."""

    if not target or "\x00" in target:
        return False
    try:
        path = Path(target).expanduser()
        return path.is_absolute() and path.exists()
    except (OSError, ValueError):
        return False


def sanitize_actions(
    actions: Iterable[Any],
    *,
    limit: int = MAX_ACTIONS,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split plugin actions into ``(allowed, blocked)``.

    ``run`` is always blocked, unknown kinds are blocked, and ``open`` /
    ``reveal`` targets must exist locally.  Duplicates are collapsed so a
    plugin cannot flood the shell with the same affordance.
    """

    allowed: list[dict[str, str]] = []
    blocked: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for item in list(actions or []):
        kind = _field(item, "kind").strip().lower()
        label = _field(item, "label").strip()
        target = _field(item, "target")

        key = (kind, target)
        if key in seen:
            continue
        seen.add(key)

        def _block(reason: str) -> None:
            blocked.append(
                {"kind": kind, "label": label or kind, "target": target, "reason": reason}
            )

        if kind in DENIED_ACTION_KINDS:
            _block("denied-kind")
            continue
        if kind not in ALLOWED_ACTION_KINDS:
            _block("unsupported-kind")
            continue
        if kind in {"open", "reveal"} and not _safe_local_target(target):
            _block("target-not-found")
            continue
        if kind == "copy":
            if not target.strip():
                _block("empty-target")
                continue
            if len(target) > MAX_COPY_CHARS:
                _block("target-too-large")
                continue

        if len(allowed) >= limit:
            _block("action-limit")
            continue
        allowed.append({"kind": kind, "label": label or kind.title(), "target": target})

    return allowed, blocked


def plugin_policy(plugin_id: str) -> str:
    return "live" if plugin_id in LIVE_PLUGINS else "enter"


ROW_SCHEMA_VERSION = 1
ROW_KINDS = ("result", "action", "suggestion", "status")


def _row(
    *,
    identifier: str,
    kind: str,
    title: str,
    subtitle: str = "",
    badge: str = "",
    plugin: str | None = None,
    confidence: float = 0.0,
    action: Mapping[str, Any] | None = None,
    submit: Mapping[str, Any] | None = None,
    search_text: str = "",
) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "badge": badge,
        "plugin": plugin,
        "confidence": round(max(0.0, min(1.0, float(confidence))), 3),
        "action": None if action is None else dict(action),
        "submit": None if submit is None else dict(submit),
        "search_text": search_text,
    }


def to_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Map one bridge payload to the rows the desktop shell renders.

    Rows are the only UI contract: the shell never reads plugin internals.
    ``action`` is attached only for already-whitelisted kinds, and
    ``submit`` lets the shell turn a row into an explicit request.
    """

    if not isinstance(payload, Mapping):
        return []

    phase = str(payload.get("phase") or "preview")
    if phase in {"health", "describe"}:
        return []

    plugin = payload.get("plugin")
    title = str(payload.get("title") or "")
    text = str(payload.get("text") or "")
    mode = str(payload.get("mode") or "")
    confidence = float(payload.get("confidence") or 0.0)
    rows: list[dict[str, Any]] = []

    def add(kind: str, row_title: str, **extra: Any) -> None:
        rows.append(
            _row(
                identifier=f"{phase}:{kind}:{len(rows)}",
                kind=kind,
                title=row_title,
                subtitle=str(extra.get("subtitle") or ""),
                badge=str(extra.get("badge") or ""),
                plugin=extra.get("plugin", plugin),
                confidence=float(extra.get("confidence", confidence)),
                action=extra.get("action"),
                submit=extra.get("submit"),
                search_text=str(extra.get("search_text") or ""),
            )
        )

    if not payload.get("ok", False):
        add(
            "status",
            title or "Bridge error",
            subtitle=text or str(payload.get("error") or ""),
            badge="error",
            confidence=0.0,
        )
        return rows

    if mode == "suggest":
        for suggestion in payload.get("suggestions") or []:
            if not isinstance(suggestion, Mapping):
                continue
            command = str(suggestion.get("command") or "")
            add(
                "suggestion",
                command,
                subtitle=str(suggestion.get("name") or ""),
                badge="press Enter",
                plugin=str(suggestion.get("plugin") or "") or None,
                confidence=float(suggestion.get("score") or 0.0),
                submit=suggestion.get("submit") if isinstance(suggestion.get("submit"), Mapping) else None,
                search_text=command,
            )
        if not rows:
            add("status", "No plugin matched", subtitle=text, badge="free text", confidence=0.0)
        return rows

    if mode == "deferred":
        add(
            "status",
            title or "Press Enter to run",
            subtitle=text,
            badge="press Enter",
            submit=payload.get("submit") if isinstance(payload.get("submit"), Mapping) else None,
            search_text=text,
        )
        return rows

    add(
        "result",
        title or str(plugin or "Result"),
        subtitle=text,
        badge=mode,
        search_text=text,
    )

    for action in payload.get("actions") or []:
        if not isinstance(action, Mapping):
            continue
        if str(action.get("kind") or "").strip().lower() not in ALLOWED_ACTION_KINDS:
            continue  # defence in depth: rows can never surface a blocked kind
        add(
            "action",
            str(action.get("label") or action.get("kind") or "Action"),
            subtitle=str(action.get("kind") or ""),
            badge=str(action.get("kind") or ""),
            action=action,
        )

    return rows


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def _finish(payload: dict[str, Any]) -> dict[str, Any]:
    """Finalise a payload: JSON-safe data plus the shell row mapping."""

    data = _jsonable(payload)
    data["rows"] = to_rows(data)
    return data


def _policy_payload() -> dict[str, Any]:
    return {
        "live_plugins": sorted(LIVE_PLUGINS),
        "enter_only_plugins": sorted(ENTER_ONLY_PLUGINS),
        "allowed_actions": sorted(ALLOWED_ACTION_KINDS),
        "denied_actions": sorted(DENIED_ACTION_KINDS),
        "free_text": "suggest-only",
        "free_text_calls_general_ai": False,
        "guard": "parse_query(...).hint",
        "max_actions": MAX_ACTIONS,
        "require_existing_target": True,
    }


def _error_payload(
    message: str,
    *,
    phase: str,
    error: str,
    plugin: str | None = None,
    title: str = "Bridge error",
) -> dict[str, Any]:
    return _finish(
        {
            "api": API_VERSION,
            "ok": False,
            "phase": phase,
            "executed": False,
            "explicit": bool(plugin),
            "requires_enter": False,
            "plugin": plugin,
            "title": title,
            "text": message,
            "actions": [],
            "blocked_actions": [],
            "citations": [],
            "confidence": 0.0,
            "mode": "error",
            "data": {},
            "suggestions": [],
            "error": error,
            "elapsed_ms": 0,
        }
    )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class BridgeEngine:
    """Owns one plugin registry for the lifetime of the shell process."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        config_path: str | Path | None = None,
        llm: Any | None = None,
        logger: logging.Logger | None = None,
        plugin_paths: Iterable[Path] = (),
    ) -> None:
        self.config = config or Config.load(config_path)
        self.config.ensure_home()
        self.logger = logger or setup_logging(self.config.log_dir)
        self.llm = llm if llm is not None else LLMClient(self.config, self.logger)
        self.ctx = PluginContext(
            config=self.config,
            llm=self.llm,
            logger=self.logger,
            cache_dir=self.config.cache_dir,
        )
        self.registry = PluginRegistry(self.config, self.ctx, self.logger)
        self.report: LoadReport = self.registry.load(
            extra_paths=[Path(path) for path in plugin_paths]
        )
        self.router = Router(self.registry, self.logger)
        self._closed = False

    # ------------------------------------------------------------- resolution

    def _prepare(
        self, raw: str, hint: str | None, phase: str
    ) -> tuple[Query | None, Plugin | None, dict[str, Any] | None]:
        """Apply the hint guard.  Returns ``(query, plugin, error_payload)``."""

        text = "" if raw is None else str(raw)
        try:
            query = parse_query(text, hint=hint, resolve=self.registry.resolve)
        except Exception as exc:  # noqa: BLE001 - parsing must never crash a shell
            self.logger.exception("bridge parse failed")
            return None, None, _error_payload(
                "The request could not be parsed.",
                phase=phase,
                error=f"parse-failed: {type(exc).__name__}",
            )

        # Explicit hint supplied by the shell (button, menu, echoed submit).
        if hint:
            plugin = self.registry.resolve(hint.strip().lstrip("@/"))
            if plugin is None:
                return None, None, _error_payload(
                    f"Unknown plugin: {hint}",
                    phase=phase,
                    error="unknown-plugin",
                )
            inline = self.registry.resolve(query.hint) if query.hint else None
            if inline is not None and inline.id != plugin.id:
                return None, None, _error_payload(
                    f"The selected plugin ({plugin.id}) does not match the text prefix ({query.hint}).",
                    phase=phase,
                    error="conflicting-hint",
                    plugin=plugin.id,
                )
            return query, plugin, None

        # Guard: only an explicit prefix produces a hint.  Free text stops here
        # and is never executed, so the chat fallback can never be reached.
        plugin = self.registry.resolve(query.hint) if query.hint else None
        return query, plugin, None

    # ------------------------------------------------------------------ phase

    def preview(self, raw: str, hint: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        query, plugin, error = self._prepare(raw, hint, PHASE_PREVIEW)
        if error is not None:
            return error

        if plugin is None:
            return self._suggestions(query, PHASE_PREVIEW, started)

        if plugin.id in LIVE_PLUGINS:
            return self._run(
                plugin,
                query,
                PHASE_PREVIEW,
                started,
                timeout=PREVIEW_TIMEOUT_SECONDS,
            )

        return self._deferred(plugin, query, PHASE_PREVIEW, started)

    def commit(self, raw: str, hint: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        query, plugin, error = self._prepare(raw, hint, PHASE_COMMIT)
        if error is not None:
            return error

        if plugin is None:
            return self._suggestions(query, PHASE_COMMIT, started)

        return self._run(plugin, query, PHASE_COMMIT, started, timeout=None)

    # ---------------------------------------------------------------- runners

    def _run(
        self,
        plugin: Plugin,
        query: Query,
        phase: str,
        started: float,
        *,
        timeout: float | None,
    ) -> dict[str, Any]:
        ctx = self.ctx
        try:
            if timeout is None:
                result = self.registry.execute(plugin, query)
            else:
                result = self.registry.execute(plugin, query, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            self.logger.exception("bridge execution failed for %s", plugin.id)
            payload = _error_payload(
                f"{plugin.name} could not run.",
                phase=phase,
                error=f"execution-failed: {type(exc).__name__}",
                plugin=plugin.id,
                title=f"{plugin.name} failed",
            )
            payload["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
            return payload

        allowed, blocked = sanitize_actions(result.actions)
        payload = result.to_dict()
        payload.update(
            {
                "api": API_VERSION,
                "phase": phase,
                "executed": True,
                "explicit": True,
                "requires_enter": False,
                "plugin": result.plugin or plugin.id,
                "actions": allowed,
                "blocked_actions": blocked,
                "suggestions": [],
                "dispatch": {
                    "plugin": plugin.id,
                    "alias": plugin.aliases[0] if plugin.aliases else plugin.id,
                    "policy": plugin_policy(plugin.id),
                    "timeout_seconds": timeout,
                },
            }
        )
        payload["elapsed_ms"] = int((time.perf_counter() - started) * 1000)

        if blocked:
            self.logger.warning(
                "bridge blocked %d action(s) from %s: %s",
                len(blocked),
                plugin.id,
                ", ".join(sorted({item["reason"] for item in blocked})),
            )
        self.logger.info(
            "bridge phase=%s plugin=%s ok=%s ms=%d",
            phase,
            payload.get("plugin"),
            payload.get("ok"),
            payload["elapsed_ms"],
        )
        return _finish(payload)

    def _deferred(
        self, plugin: Plugin, query: Query, phase: str, started: float
    ) -> dict[str, Any]:
        """Enter-only plugin touched during typing: describe, never execute."""

        alias = plugin.aliases[0] if plugin.aliases else plugin.id
        preview = (query.text or "").strip()
        if len(preview) > PREVIEW_TEXT_CHARS:
            preview = preview[: PREVIEW_TEXT_CHARS - 1].rstrip() + "\u2026"

        return _finish(
            {
                "api": API_VERSION,
                "ok": True,
                "phase": phase,
                "executed": False,
                "explicit": True,
                "requires_enter": True,
                "plugin": plugin.id,
                "title": f"{plugin.name} \u2014 press Enter to run",
                "text": preview,
                "actions": [],
                "blocked_actions": [],
                "citations": [],
                "confidence": 0.0,
                "mode": "deferred",
                "data": {
                    "plugin": plugin.id,
                    "name": plugin.name,
                    "description": plugin.description,
                    "reason": "enter-only",
                },
                "suggestions": [],
                "submit": {
                    "op": PHASE_COMMIT,
                    "hint": alias,
                    "text": query.text,
                },
                "command": f"@{alias}" + (f" {query.text}" if query.text else ""),
                "error": "",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        )

    def _suggestions(self, query: Query, phase: str, started: float) -> dict[str, Any]:
        """Free text: score, suggest an explicit command, never execute."""

        text = (query.text or "").strip()
        try:
            route = self.router.route(text)
            alternatives = list(route.alternatives or [])
        except Exception:  # noqa: BLE001 - suggestions are best effort
            self.logger.exception("bridge suggestion scoring failed")
            alternatives = []

        suggestions: list[dict[str, Any]] = []
        for item in alternatives[:MAX_SUGGESTIONS]:
            identifier = str(item.get("plugin") or "")
            plugin = self.registry.resolve(identifier)
            if plugin is None:
                continue
            alias = plugin.aliases[0] if plugin.aliases else plugin.id
            suggestions.append(
                {
                    "plugin": plugin.id,
                    "name": plugin.name,
                    "alias": alias,
                    "command": f"@{alias}" + (f" {text}" if text else ""),
                    "score": round(float(item.get("score") or 0.0), 3),
                    "policy": plugin_policy(plugin.id),
                    "submit": {"op": PHASE_COMMIT, "hint": alias, "text": text},
                }
            )

        if suggestions:
            body = "Free text is not executed. Pick a command:\n" + "\n".join(
                f"  {item['command']}" for item in suggestions
            )
        else:
            body = (
                "Free text is not executed. Address a plugin explicitly: "
                "@files, @gp7, @homework, @downloads, or @grammar."
            )

        return _finish(
            {
                "api": API_VERSION,
                "ok": True,
                "phase": phase,
                "executed": False,
                "explicit": False,
                "requires_enter": False,
                "plugin": None,
                "title": "Choose a plugin",
                "text": body,
                "actions": [],
                "blocked_actions": [],
                "citations": [],
                "confidence": 0.0,
                "mode": "suggest",
                "data": {"free_text": text, "reason": "no-explicit-plugin"},
                "suggestions": suggestions,
                "error": "",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        )

    # ---------------------------------------------------------------- reports

    def health(self) -> dict[str, Any]:
        try:
            plugins = self.registry.health()
            llm = {
                "available": bool(getattr(self.llm, "available", False)),
                "provider": getattr(self.llm, "provider", ""),
            }
        except Exception as exc:  # noqa: BLE001
            return _error_payload(
                "Health check failed.",
                phase="health",
                error=f"health-failed: {type(exc).__name__}",
            )

        return _finish(
            {
                "api": API_VERSION,
                "ok": True,
                "phase": "health",
                "executed": False,
                "plugin": None,
                "actions": [],
                "blocked_actions": [],
                "suggestions": [],
                "error": "",
                "elapsed_ms": 0,
                "bridge": {"id": BRIDGE_ID, "version": BRIDGE_VERSION},
                "plugins": plugins,
                "llm": llm,
                "load": self.report.to_dict(),
                "policy": _policy_payload(),
            }
        )

    def describe(self) -> dict[str, Any]:
        try:
            described = self.registry.describe()
        except Exception as exc:  # noqa: BLE001
            return _error_payload(
                "Plugin catalogue unavailable.",
                phase="describe",
                error=f"describe-failed: {type(exc).__name__}",
            )

        plugins = []
        for item in described:
            identifier = str(item.get("id") or "")
            instance = self.registry.get(identifier)
            plugins.append(
                {
                    "id": identifier,
                    "name": item.get("name"),
                    "description": item.get("description"),
                    "aliases": item.get("aliases", []),
                    "priority": item.get("priority"),
                    "enabled": self.registry.is_enabled(instance) if instance else False,
                    "policy": plugin_policy(identifier),
                }
            )

        return _finish(
            {
                "api": API_VERSION,
                "ok": True,
                "phase": "describe",
                "executed": False,
                "plugin": None,
                "actions": [],
                "blocked_actions": [],
                "suggestions": [],
                "error": "",
                "elapsed_ms": 0,
                "bridge": {"id": BRIDGE_ID, "version": BRIDGE_VERSION},
                "plugins": plugins,
                "policy": _policy_payload(),
            }
        )

    # ------------------------------------------------------------------- rpc

    def handle(self, request: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            return _error_payload(
                "Expected a JSON object request.",
                phase=PHASE_PREVIEW,
                error="invalid-request",
            )

        op = str(request.get("op") or PHASE_PREVIEW).strip().lower()
        raw = request.get("text") or ""
        hint = request.get("hint") or None
        if hint is not None:
            hint = str(hint).strip() or None

        try:
            if op == PHASE_PREVIEW:
                return self.preview(str(raw), hint)
            if op == PHASE_COMMIT:
                return self.commit(str(raw), hint)
            if op == "health":
                return self.health()
            if op == "describe":
                return self.describe()
        except Exception as exc:  # noqa: BLE001 - last line of defence
            self.logger.exception("bridge request failed")
            return _error_payload(
                "The bridge could not complete the request.",
                phase=op,
                error=f"internal-error: {type(exc).__name__}",
            )

        return _error_payload(
            f"Unknown op: {op!r}. Use preview, commit, health, or describe.",
            phase=op,
            error="unknown-op",
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.registry.close()
        except Exception:  # noqa: BLE001 - shutdown must stay quiet
            self.logger.exception("bridge shutdown failed")


# --------------------------------------------------------------------------- #
# Process-wide engine
# --------------------------------------------------------------------------- #

_ENGINE: BridgeEngine | None = None
_ENGINE_LOCK = threading.Lock()


def get_engine() -> BridgeEngine:
    """Lazily build the single registry for this process."""

    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = BridgeEngine()
    return _ENGINE


def set_engine(engine: BridgeEngine | None) -> None:
    """Replace the process-wide engine (used by hosts and tests)."""

    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None and _ENGINE is not engine:
            _ENGINE.close()
        _ENGINE = engine


def close_engine() -> None:
    set_engine(None)


# --------------------------------------------------------------------------- #
# Module-level facade
# --------------------------------------------------------------------------- #


def _with_engine(call) -> dict[str, Any]:
    try:
        return call(get_engine())
    except Exception as exc:  # noqa: BLE001 - startup boundary
        _log.exception("bridge engine unavailable")
        return _error_payload(
            "The plugin engine is unavailable. Check the assistant configuration.",
            phase="startup",
            error=f"engine-unavailable: {type(exc).__name__}",
            title="Bridge unavailable",
        )


def preview(text: str, hint: str | None = None) -> dict[str, Any]:
    return _with_engine(lambda engine: engine.preview(text, hint))


def commit(text: str, hint: str | None = None) -> dict[str, Any]:
    return _with_engine(lambda engine: engine.commit(text, hint))


def health() -> dict[str, Any]:
    return _with_engine(lambda engine: engine.health())


def describe() -> dict[str, Any]:
    return _with_engine(lambda engine: engine.describe())


def handle(request: Mapping[str, Any] | None) -> dict[str, Any]:
    return _with_engine(lambda engine: engine.handle(request))


# --------------------------------------------------------------------------- #
# Optional stdio entry point
# --------------------------------------------------------------------------- #


def _ensure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8()
    parser = argparse.ArgumentParser(
        prog="pcassistant_bridge",
        description="JSON adapter between the desktop shell and PC Assistant plugins.",
    )
    parser.add_argument("--request", help="JSON request object, or '-' for stdin")
    parser.add_argument("--op", default=PHASE_PREVIEW, choices=["preview", "commit", "health", "describe"])
    parser.add_argument("--hint", help="explicit plugin id or alias")
    parser.add_argument("text", nargs="*", help="request text")
    args = parser.parse_args(argv)

    try:
        if args.request:
            raw = sys.stdin.read() if args.request == "-" else args.request
            request = json.loads(raw or "{}")
        elif not sys.stdin.isatty():
            raw = sys.stdin.read().strip()
            request = json.loads(raw) if raw else {"op": args.op}
        else:
            request = {"op": args.op, "hint": args.hint, "text": " ".join(args.text)}
    except (json.JSONDecodeError, OSError) as exc:
        payload = _error_payload(
            f"Invalid JSON request: {exc}",
            phase=args.op,
            error="invalid-json",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    try:
        payload = handle(request)
    finally:
        close_engine()

    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())