"""Command-line bridge.

The stable contract for other hosts (including the PowerShell palette) is:

    python -m pcassistant --json "query"

which prints one JSON object with ``api``, ``ok``, ``plugin``, ``title``,
``text``, ``citations``, ``actions``, ``confidence``, ``mode``, ``data``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .assistant import Assistant
from .models import PluginResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcassistant",
        description="Local AI PC assistant with pluggable capabilities.",
    )
    parser.add_argument("query", nargs="*", help="what you want to do")
    parser.add_argument("--plugin", help="force a plugin id or alias")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--list", action="store_true", help="list plugins")
    parser.add_argument("--health", action="store_true", help="show plugin and AI health")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument(
        "--plugins-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="load external plugins from DIR (repeatable)",
    )
    parser.add_argument("--version", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print(f"pcassistant {__version__}")
        return 0

    assistant = None
    try:
        assistant = Assistant(
            config_path=args.config,
            plugin_paths=[Path(path) for path in args.plugins_dir],
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        message = f"Could not start the assistant: {type(exc).__name__}: {exc}"
        if args.json:
            _print_json({"api": 1, "ok": False, "plugin": "assistant", "error": message})
        else:
            print(message)
        return 1

    try:
        if args.list:
            plugins = assistant.list_plugins()
            if args.json:
                _print_json({"api": 1, "plugins": plugins})
            else:
                for plugin in plugins:
                    health = plugin.get("health", {})
                    state = "ok" if health.get("ok") else "needs setup"
                    print(f"{plugin['id']:<22} {plugin['name']:<22} {state}")
                    print(f"  {plugin['description']}")
                    if health.get("detail"):
                        print(f"  {health['detail']}")
            return 0

        if args.health:
            health = assistant.health()
            if args.json:
                _print_json({"api": 1, "health": health})
            else:
                for identifier, info in health["plugins"].items():
                    state = "ok" if info.get("ok") else "needs setup"
                    enabled = "" if info.get("enabled") else " (disabled)"
                    print(f"{identifier:<22} {state}{enabled}")
                    if info.get("detail"):
                        print(f"  {info['detail']}")
                llm = health["llm"]
                print(f"\nAI: {'ready' if llm['available'] else 'not configured'} · {llm['provider']}")
                for warning in health.get("warnings", []):
                    print(f"  ! {warning}")
                if health["load"]["failed"]:
                    print("\nPlugin load failures:")
                    for failure in health["load"]["failed"]:
                        print(f"  {failure.get('module')}: {failure.get('error')}")
            return 0

        raw = " ".join(args.query).strip()
        if not raw and not args.plugin:
            parser.print_help()
            return 0

        result = assistant.ask(raw, hint=args.plugin)
        if args.json:
            payload = {"api": 1, **result.to_dict()}
            _print_json(payload)
        else:
            _print_human(result)
        return 0 if result.ok else 1
    finally:
        assistant.close()


def _print_human(result: PluginResult) -> None:
    print(result.title or result.plugin)
    print("=" * max(12, len(result.title or result.plugin)))
    if result.text:
        print(result.text)
    if result.citations:
        print("\nSources")
        for index, citation in enumerate(result.citations, start=1):
            location = f" ({citation.locator})" if citation.locator else ""
            print(f"  [{index}] {citation.title}{location}")
            print(f"      {citation.source}")
    if result.actions:
        print("\nActions")
        for action in result.actions:
            print(f"  {action.kind:<6} {action.label}")
    if not result.ok and result.error:
        print(f"\nError: {result.error}")
    if result.confidence:
        print(f"\nConfidence: {result.confidence:.0%} · {result.mode} · {result.elapsed_ms} ms")


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _ensure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
