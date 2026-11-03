"""Integration tests for the desktop shell <-> plugin engine bridge.

The bridge is loaded the way a host loads it: by file path, not as an
installed package.  Tests are offline and use temporary folders only.

Covered guarantees
------------------
1. Free text never executes anything and never reaches the general chat path.
2. ``parse_query(...).hint`` is the only execution trigger.
3. ``file_search`` / ``gp7_search`` may run live; the rest wait for Enter.
4. Only ``open``, ``reveal`` and ``copy`` reach the shell; ``run`` is blocked.
5. Every payload is JSON-safe and errors never escape as exceptions.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BRIDGE_PATH = ROOT / "plugins" / "pcassistant_bridge" / "main.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("pcassistant_bridge_main", BRIDGE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - environment guard
        raise RuntimeError(f"Could not load bridge from {BRIDGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()

from pcassistant.config import Config  # noqa: E402  (after sys.path setup)
from pcassistant.models import Action, PluginResult  # noqa: E402


ALL_PLUGIN_IDS = {
    "grammar_fixer",
    "gp7_search",
    "homework_search",
    "file_search",
    "downloads_summarizer",
}


class _FakeLLM:
    """Records calls; availability is explicit so tests can prove non-use."""

    def __init__(self, *, available: bool = True, reply: str = "I went to the store.") -> None:
        self.available = available
        self.provider = "fake"
        self.last_model = "fake-model" if available else ""
        self.reply = reply
        self.calls: list[dict] = []

    def models(self) -> list[str]:
        return ["fake-model"] if self.available else []

    def complete(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if not self.available:
            raise AssertionError("The bridge called the LLM while it was unavailable.")
        return self.reply


class BridgeTestCase(unittest.TestCase):
    """Temp folders, temp home, fake LLM, real registry."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

        patcher = mock.patch.dict(
            os.environ,
            {
                "PCASSISTANT_HOME": str(self.root / "home"),
                "PCASSISTANT_DISABLE_AI": "1",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self._make_fixtures()
        self.llm = _FakeLLM()
        self.engine = self._make_engine()
        self.addCleanup(self.engine.close)

    # ----------------------------------------------------------- fixtures

    def _make_fixtures(self) -> None:
        self.docs = self.root / "docs"
        self.docs.mkdir()
        (self.docs / "budget-2026.txt").write_text("quarterly budget", encoding="utf-8")
        (self.docs / "meeting-notes.txt").write_text("meeting notes", encoding="utf-8")

        self.gp7 = self.root / "gp7"
        self.gp7.mkdir()
        (self.gp7 / "lecture1.mp4").write_bytes(b"\x00")
        (self.gp7 / "lecture1.txt").write_text(
            "00:01 cell membrane overview\n00:42 membrane proteins", encoding="utf-8"
        )

        self.study = self.root / "study"
        self.study.mkdir()
        (self.study / "biology-notes.md").write_text(
            "# Osmosis\n\nWater moves across a semi-permeable membrane.",
            encoding="utf-8",
        )

        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        (self.downloads / "taxes.txt").write_text("tax document", encoding="utf-8")
        (self.downloads / "report.md").write_text("report body", encoding="utf-8")

    def _config(self) -> Config:
        return Config.from_dict(
            {
                "llm": {"enabled": False, "api_key": "", "provider": "gemini"},
                "paths": {
                    "file_search_roots": [str(self.docs)],
                    "gp7_root": str(self.gp7),
                    "homework_roots": [str(self.study)],
                    "downloads": str(self.downloads),
                },
            }
        )

    def _logger(self) -> logging.Logger:
        logger = logging.getLogger(f"test.bridge.{id(self)}")
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        return logger

    def _make_engine(self, llm=None) -> "bridge.BridgeEngine":
        return bridge.BridgeEngine(
            config=self._config(),
            llm=llm if llm is not None else self.llm,
            logger=self._logger(),
        )


# --------------------------------------------------------------------------- #
# 1. The hint guard
# --------------------------------------------------------------------------- #


class GuardTests(BridgeTestCase):
    def test_free_text_preview_never_executes(self):
        payload = self.engine.preview("budget file")

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["executed"])
        self.assertFalse(payload["explicit"])
        self.assertIsNone(payload["plugin"])
        self.assertEqual(payload["mode"], "suggest")
        self.assertEqual(self.llm.calls, [])

    def test_free_text_commit_never_executes(self):
        payload = self.engine.commit("summarize my recent downloads")

        self.assertFalse(payload["executed"])
        self.assertEqual(payload["mode"], "suggest")
        self.assertIn("not executed", payload["text"].lower())
        self.assertEqual(self.llm.calls, [])

    def test_free_text_suggests_an_explicit_command(self):
        payload = self.engine.commit("summarize my recent downloads")

        self.assertTrue(payload["suggestions"])
        top = payload["suggestions"][0]
        self.assertEqual(top["plugin"], "downloads_summarizer")
        self.assertTrue(top["command"].startswith("@downloads"))
        self.assertEqual(top["submit"]["op"], "commit")
        self.assertEqual(top["submit"]["hint"], "downloads")

    def test_free_text_never_reaches_general_chat(self):
        source = BRIDGE_PATH.read_text(encoding="utf-8")

        self.assertNotIn("from pcassistant.assistant", source)
        self.assertNotIn(".ask(", source)
        # Behavioral proof: an available model is never used for free text.
        payload = self.engine.commit("what is a monad")
        self.assertFalse(payload["executed"])
        self.assertEqual(self.llm.calls, [])

    def test_explicit_prefixes_are_the_only_trigger(self):
        for raw in ("@files budget", "files: budget", "/files budget"):
            payload = self.engine.preview(raw)
            self.assertTrue(payload["executed"], raw)
            self.assertTrue(payload["explicit"], raw)
            self.assertEqual(payload["plugin"], "file_search", raw)

    def test_unknown_prefix_is_treated_as_free_text(self):
        payload = self.engine.preview("@nope budget")

        self.assertFalse(payload["executed"])
        self.assertEqual(payload["mode"], "suggest")
        self.assertNotEqual(payload["error"], "unknown-plugin")

    def test_unknown_shell_hint_is_rejected(self):
        payload = self.engine.handle({"op": "commit", "hint": "does_not_exist", "text": "x"})

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "unknown-plugin")
        self.assertFalse(payload["executed"])

    def test_hint_may_include_an_at_sign(self):
        payload = self.engine.commit("i has went too the store", hint="@grammar")

        self.assertTrue(payload["executed"])
        self.assertEqual(payload["plugin"], "grammar_fixer")

    def test_conflicting_hint_is_rejected(self):
        payload = self.engine.handle(
            {"op": "commit", "hint": "files", "text": "@grammar i has went"}
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "conflicting-hint")
        self.assertFalse(payload["executed"])


# --------------------------------------------------------------------------- #
# 2. Live vs Enter policy
# --------------------------------------------------------------------------- #


class LivePolicyTests(BridgeTestCase):
    def test_policy_payload_matches_requirements(self):
        policy = self.engine.describe()["policy"]

        self.assertEqual(set(policy["live_plugins"]), {"file_search", "gp7_search"})
        self.assertEqual(
            set(policy["enter_only_plugins"]),
            {"grammar_fixer", "homework_search", "downloads_summarizer"},
        )
        self.assertEqual(set(policy["allowed_actions"]), {"open", "reveal", "copy"})
        self.assertEqual(policy["denied_actions"], ["run"])
        self.assertFalse(policy["free_text_calls_general_ai"])
        self.assertEqual(policy["guard"], "parse_query(...).hint")

    def test_file_search_runs_live(self):
        payload = self.engine.preview("@files budget")

        self.assertTrue(payload["executed"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["plugin"], "file_search")
        self.assertEqual(payload["dispatch"]["policy"], "live")

    def test_gp7_runs_live(self):
        payload = self.engine.preview("@gp7 lecture")

        self.assertTrue(payload["executed"])
        self.assertEqual(payload["plugin"], "gp7_search")
        self.assertEqual(payload["dispatch"]["policy"], "live")

    def test_grammar_is_deferred_until_enter(self):
        payload = self.engine.preview("@grammar i has went too the store")

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["executed"])
        self.assertTrue(payload["requires_enter"])
        self.assertEqual(payload["plugin"], "grammar_fixer")
        self.assertEqual(payload["mode"], "deferred")
        self.assertEqual(self.llm.calls, [])
        self.assertIn("@grammar", payload["command"])
        self.assertEqual(payload["submit"]["op"], "commit")

    def test_grammar_runs_on_commit(self):
        payload = self.engine.commit("@grammar i has went too the store")

        self.assertTrue(payload["executed"])
        self.assertEqual(payload["plugin"], "grammar_fixer")
        self.assertEqual(payload["mode"], "ai")
        self.assertEqual(payload["text"], "I went to the store.")
        self.assertEqual(len(self.llm.calls), 1)

    def test_grammar_commit_works_without_ai(self):
        engine = self._make_engine(llm=_FakeLLM(available=False))
        self.addCleanup(engine.close)

        payload = engine.commit("@grammar i has went too the store")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "mechanical-fallback")
        self.assertTrue(payload["data"]["notice"])

    def test_homework_is_enter_only(self):
        preview = self.engine.preview("@homework osmosis")
        self.assertFalse(preview["executed"])
        self.assertTrue(preview["requires_enter"])
        self.assertEqual(preview["plugin"], "homework_search")

        commit = self.engine.commit("@homework osmosis")
        self.assertTrue(commit["executed"])
        self.assertEqual(commit["plugin"], "homework_search")
        self.assertIn(commit["mode"], {"search", "grounded-answer"})

    def test_downloads_is_enter_only(self):
        preview = self.engine.preview("@downloads")
        self.assertFalse(preview["executed"])
        self.assertTrue(preview["requires_enter"])

        commit = self.engine.commit("@downloads")
        self.assertTrue(commit["executed"])
        self.assertEqual(commit["plugin"], "downloads_summarizer")

    def test_preview_caps_the_time_budget(self):
        payload = self.engine.preview("@files budget")
        self.assertEqual(payload["dispatch"]["timeout_seconds"], bridge.PREVIEW_TIMEOUT_SECONDS)


# --------------------------------------------------------------------------- #
# 3. Action policy
# --------------------------------------------------------------------------- #


class ActionPolicyTests(BridgeTestCase):
    def test_run_is_blocked(self):
        allowed, blocked = bridge.sanitize_actions([Action("run", "Launch", "calc.exe")])

        self.assertEqual(allowed, [])
        self.assertEqual(blocked[0]["reason"], "denied-kind")
        self.assertEqual(blocked[0]["kind"], "run")

    def test_run_is_blocked_case_and_space_insensitively(self):
        allowed, blocked = bridge.sanitize_actions(
            [{"kind": "  RUN ", "label": "Launch", "target": "calc.exe"}]
        )

        self.assertEqual(allowed, [])
        self.assertEqual(blocked[0]["reason"], "denied-kind")

    def test_open_requires_an_existing_absolute_target(self):
        missing = self.root / "does-not-exist.txt"
        allowed, blocked = bridge.sanitize_actions(
            [
                Action("open", "Missing", str(missing)),
                Action("open", "Relative", "notes.txt"),
            ]
        )

        self.assertEqual(allowed, [])
        self.assertEqual([item["reason"] for item in blocked], ["target-not-found"] * 2)

    def test_open_and_reveal_are_allowed_for_real_paths(self):
        allowed, blocked = bridge.sanitize_actions(
            [
                Action("open", "Open doc", str(self.docs / "budget-2026.txt")),
                Action("reveal", "Reveal folder", str(self.docs)),
            ]
        )

        self.assertEqual(blocked, [])
        self.assertEqual([item["kind"] for item in allowed], ["open", "reveal"])

    def test_copy_is_allowed(self):
        allowed, blocked = bridge.sanitize_actions([Action("copy", "Copy", "hello world")])

        self.assertEqual(blocked, [])
        self.assertEqual(allowed[0], {"kind": "copy", "label": "Copy", "target": "hello world"})

    def test_copy_is_bounded(self):
        allowed, blocked = bridge.sanitize_actions([Action("copy", "Copy", "x" * (bridge.MAX_COPY_CHARS + 1))])

        self.assertEqual(allowed, [])
        self.assertEqual(blocked[0]["reason"], "target-too-large")

    def test_empty_copy_is_blocked(self):
        allowed, blocked = bridge.sanitize_actions([Action("copy", "Copy", "   ")])

        self.assertEqual(allowed, [])
        self.assertEqual(blocked[0]["reason"], "empty-target")

    def test_unsupported_kind_is_blocked(self):
        allowed, blocked = bridge.sanitize_actions([Action("exec", "Exec", "calc.exe")])

        self.assertEqual(allowed, [])
        self.assertEqual(blocked[0]["reason"], "unsupported-kind")

    def test_duplicate_actions_are_collapsed(self):
        action = Action("open", "Open docs", str(self.docs))
        allowed, blocked = bridge.sanitize_actions([action, action])

        self.assertEqual(len(allowed), 1)
        self.assertEqual(blocked, [])

    def test_action_limit_is_enforced(self):
        allowed, blocked = bridge.sanitize_actions(
            [Action("copy", f"Copy {index}", f"payload-{index}") for index in range(bridge.MAX_ACTIONS + 3)]
        )

        self.assertEqual(len(allowed), bridge.MAX_ACTIONS)
        self.assertTrue(all(item["reason"] == "action-limit" for item in blocked))

    def test_execution_is_sanitised_end_to_end(self):
        def fake_execute(plugin, query, timeout=None):
            return PluginResult(
                plugin=plugin.id,
                title="Crafted",
                text="crafted result",
                actions=[
                    Action("run", "Run it", "calc.exe"),
                    Action("open", "Open docs", str(self.docs)),
                    Action("copy", "Copy", "hello"),
                ],
            )

        self.engine.registry.execute = fake_execute  # type: ignore[assignment]

        payload = self.engine.commit("@files budget")

        self.assertTrue(payload["executed"])
        self.assertEqual([item["kind"] for item in payload["actions"]], ["open", "copy"])
        self.assertEqual([item["kind"] for item in payload["blocked_actions"]], ["run"])

    def test_real_plugins_emit_only_allowed_actions(self):
        for raw in ("@files budget", "@gp7 lecture", "@homework osmosis", "@downloads"):
            payload = self.engine.commit(raw)
            self.assertEqual(payload["blocked_actions"], [], raw)
            for action in payload["actions"]:
                self.assertIn(action["kind"], bridge.ALLOWED_ACTION_KINDS, raw)


# --------------------------------------------------------------------------- #
# 4. Contract and error containment
# --------------------------------------------------------------------------- #


class ContractTests(BridgeTestCase):
    REQUIRED_KEYS = {
        "api",
        "ok",
        "phase",
        "executed",
        "plugin",
        "actions",
        "blocked_actions",
        "suggestions",
        "error",
        "elapsed_ms",
    }

    def _payloads(self):
        return [
            self.engine.preview("@files budget"),
            self.engine.preview("@grammar teh cat"),
            self.engine.preview("free text"),
            self.engine.commit("@grammar teh cat"),
            self.engine.commit("free text"),
            self.engine.health(),
            self.engine.describe(),
        ]

    def test_payloads_are_json_serialisable(self):
        for payload in self._payloads():
            encoded = json.dumps(payload)
            self.assertIsInstance(encoded, str)

    def test_payloads_share_the_required_shape(self):
        for payload in self._payloads():
            self.assertTrue(self.REQUIRED_KEYS.issubset(payload), payload.get("phase"))
            self.assertEqual(payload["api"], bridge.API_VERSION)
            self.assertIn(payload["phase"], {"preview", "commit", "health", "describe"})

    def test_health_reports_plugins_and_policy(self):
        payload = self.engine.health()

        self.assertTrue(payload["ok"])
        self.assertEqual(set(payload["plugins"]), ALL_PLUGIN_IDS)
        self.assertEqual(payload["load"]["failed"], [])
        self.assertFalse(payload["policy"]["free_text_calls_general_ai"])

    def test_describe_lists_every_plugin_with_policy(self):
        plugins = {item["id"]: item for item in self.engine.describe()["plugins"]}

        self.assertEqual(set(plugins), ALL_PLUGIN_IDS)
        self.assertEqual(plugins["file_search"]["policy"], "live")
        self.assertEqual(plugins["grammar_fixer"]["policy"], "enter")
        self.assertTrue(plugins["file_search"]["enabled"])

    def test_unknown_op_is_rejected(self):
        payload = self.engine.handle({"op": "nuke"})

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "unknown-op")

    def test_non_mapping_request_is_rejected(self):
        payload = self.engine.handle("not a request")  # type: ignore[arg-type]

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "invalid-request")

    def test_plugin_exception_is_contained(self):
        def boom(*args, **kwargs):
            raise RuntimeError("explode")

        self.engine.registry.execute = boom  # type: ignore[assignment]

        payload = self.engine.commit("@files budget")

        self.assertFalse(payload["ok"])
        self.assertFalse(payload["executed"])
        self.assertEqual(payload["error"], "execution-failed: RuntimeError")
        self.assertIn("could not run", payload["text"].lower())

    def test_missing_engine_returns_an_error_payload(self):
        bridge.set_engine(None)
        self.addCleanup(bridge.set_engine, None)

        with mock.patch.object(bridge._log, "exception"):
            with mock.patch.object(bridge, "get_engine", side_effect=RuntimeError("no config")):
                payload = bridge.preview("@files budget")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "engine-unavailable: RuntimeError")

    def test_manifest_matches_code_policy(self):
        manifest = json.loads((BRIDGE_PATH.parent / "plugin.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["id"], bridge.BRIDGE_ID)
        self.assertEqual(manifest["version"], bridge.BRIDGE_VERSION)
        self.assertEqual(manifest["entry"], "main.py")
        self.assertEqual(set(manifest["actions"]["allowed"]), bridge.ALLOWED_ACTION_KINDS)
        self.assertEqual(set(manifest["actions"]["denied"]), bridge.DENIED_ACTION_KINDS)
        self.assertEqual(set(manifest["policy"]["live_plugins"]), bridge.LIVE_PLUGINS)
        self.assertEqual(set(manifest["policy"]["enter_only_plugins"]), bridge.ENTER_ONLY_PLUGINS)
        self.assertFalse(manifest["policy"]["free_text_calls_general_ai"])
        self.assertEqual(manifest["policy"]["guard"], "parse_query(...).hint")

    def test_engine_is_reusable_and_idempotent_on_close(self):
        first = self.engine.preview("@files budget")
        self.engine.close()
        self.engine.close()
        self.assertTrue(first["executed"])


# --------------------------------------------------------------------------- #
# 5. Row mapping (shell UI contract)
# --------------------------------------------------------------------------- #


class RowMappingTests(BridgeTestCase):
    ROW_KEYS = {
        "id",
        "kind",
        "title",
        "subtitle",
        "badge",
        "plugin",
        "confidence",
        "action",
        "submit",
        "search_text",
    }

    def test_deferred_payload_maps_to_one_status_row(self):
        payload = self.engine.preview("@grammar i has went")

        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertTrue(self.ROW_KEYS.issubset(row))
        self.assertEqual(row["kind"], "status")
        self.assertEqual(row["badge"], "press Enter")
        self.assertEqual(row["plugin"], "grammar_fixer")
        self.assertEqual(row["submit"]["op"], "commit")
        self.assertEqual(row["submit"]["hint"], "grammar")

    def test_suggestion_payload_maps_one_row_per_suggestion(self):
        payload = self.engine.commit("summarize my recent downloads")

        self.assertTrue(payload["suggestions"])
        self.assertEqual(len(payload["rows"]), len(payload["suggestions"]))
        for row, suggestion in zip(payload["rows"], payload["suggestions"]):
            self.assertEqual(row["kind"], "suggestion")
            self.assertEqual(row["title"], suggestion["command"])
            self.assertEqual(row["submit"]["hint"], suggestion["alias"])
            self.assertEqual(row["submit"]["op"], "commit")

    def test_executed_payload_maps_result_then_actions(self):
        payload = self.engine.commit("@files budget")

        rows = payload["rows"]
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "result")
        self.assertEqual(rows[0]["plugin"], "file_search")
        self.assertEqual(rows[0]["badge"], payload["mode"])

        action_rows = [row for row in rows if row["kind"] == "action"]
        self.assertEqual(len(action_rows), len(payload["actions"]))
        for row in action_rows:
            self.assertIn(row["action"]["kind"], bridge.ALLOWED_ACTION_KINDS)
            self.assertEqual(row["badge"], row["action"]["kind"])

    def test_rows_never_expose_a_blocked_action(self):
        payload = {
            "api": 1,
            "ok": True,
            "phase": "commit",
            "executed": True,
            "plugin": "file_search",
            "title": "Crafted",
            "text": "crafted",
            "mode": "search",
            "confidence": 0.9,
            "actions": [
                {"kind": "run", "label": "Run", "target": "calc.exe"},
                {"kind": "copy", "label": "Copy", "target": "hello"},
            ],
        }

        rows = bridge.to_rows(payload)
        kinds = [row["action"]["kind"] for row in rows if row["action"]]

        self.assertEqual(kinds, ["copy"])
        self.assertNotIn("run", [row["badge"] for row in rows])

    def test_error_payload_maps_to_error_status_row(self):
        payload = self.engine.handle({"op": "nuke"})

        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0]["kind"], "status")
        self.assertEqual(payload["rows"][0]["badge"], "error")

    def test_health_and_describe_produce_no_rows(self):
        self.assertEqual(self.engine.health()["rows"], [])
        self.assertEqual(self.engine.describe()["rows"], [])

    def test_row_ids_are_unique_and_json_safe(self):
        payload = self.engine.commit("@files budget")
        identifiers = [row["id"] for row in payload["rows"]]

        self.assertEqual(len(identifiers), len(set(identifiers)))
        for row in payload["rows"]:
            self.assertTrue(self.ROW_KEYS.issubset(row))
            self.assertTrue(0.0 <= row["confidence"] <= 1.0)
        json.dumps(payload)

    def test_free_text_keystroke_never_calls_the_model(self):
        payload = self.engine.preview("summarize my recent downloads")

        self.assertEqual(self.llm.calls, [])
        self.assertFalse(payload["executed"])
        self.assertTrue(all(row["kind"] == "suggestion" for row in payload["rows"]))


# --------------------------------------------------------------------------- #
# 6. Stdio entry point
# --------------------------------------------------------------------------- #


class StdioTests(BridgeTestCase):
    def test_health_request_via_argv(self):
        bridge.set_engine(self.engine)
        self.addCleanup(bridge.set_engine, None)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = bridge.main(["--request", json.dumps({"op": "health"})])

        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["phase"], "health")

    def test_invalid_json_returns_exit_code_one(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = bridge.main(["--request", "{not json"])

        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "invalid-json")

    def test_subprocess_entry_point(self):
        """The shipping invocation must work without PYTHONPATH tweaks."""

        import subprocess

        env = {
            **os.environ,
            "PCASSISTANT_HOME": str(self.root / "home-subprocess"),
            "PCASSISTANT_DISABLE_AI": "1",
        }
        completed = subprocess.run(
            [sys.executable, str(BRIDGE_PATH), "--request", json.dumps({"op": "health"})],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            env=env,
            timeout=60,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["phase"], "health")
        self.assertFalse(payload["policy"]["free_text_calls_general_ai"])


if __name__ == "__main__":
    unittest.main()