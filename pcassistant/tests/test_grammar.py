from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcassistant.plugins.grammar_fixer import GrammarFixerPlugin

from .support import FakeLLM, make_config, make_context, make_query


class GrammarFixerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _plugin(self, **plugin_settings):
        data = {"plugins": {"grammar_fixer": plugin_settings}} if plugin_settings else None
        return GrammarFixerPlugin(make_config(data))

    def test_ai_correction(self):
        llm = FakeLLM(["I went to the store yesterday."])
        ctx = make_context(self.root, llm=llm)
        result = self._plugin().handle(
            make_query("fix my grammar: i has went too the store yesterday"), ctx
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "I went to the store yesterday.")
        self.assertEqual(result.mode, "ai")
        self.assertEqual(result.actions[0].kind, "copy")
        self.assertGreater(result.data["change_count"], 0)
        self.assertIn("copy-editing", llm.calls[0]["system"])

    def test_mechanical_fallback_when_no_ai(self):
        ctx = make_context(self.root, llm=FakeLLM(available=False))
        result = self._plugin().handle(
            make_query("fix my grammar: i has went too the store"), ctx
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "mechanical-fallback")
        self.assertIn("mechanical", result.title.lower())
        self.assertTrue(result.data["notice"])
        self.assertTrue(result.text.startswith("I has"))

    def test_compact_trigger_phrases_are_stripped(self):
        llm = FakeLLM(["The cat sat."])
        ctx = make_context(self.root, llm=llm)
        result = self._plugin().handle(make_query("fix grammar: teh cat sat"), ctx)
        self.assertEqual(result.data["original"], "teh cat sat")
        self.assertEqual(result.text, "The cat sat.")

        result = self._plugin().handle(make_query("proofread this: i is tired"), ctx)
        self.assertEqual(result.data["original"], "i is tired")

    def test_offline_setting_never_calls_network(self):
        llm = FakeLLM(["should not be used"])
        ctx = make_context(self.root, llm=llm)
        result = self._plugin(allow_network=False).handle(make_query("grammar: teh cat"), ctx)
        self.assertEqual(result.mode, "offline")
        self.assertEqual(llm.calls, [])

    def test_empty_input_returns_help(self):
        ctx = make_context(self.root)
        result = self._plugin().handle(make_query("grammar:"), ctx)
        self.assertEqual(result.mode, "help")
        self.assertIn("Send the passage", result.text)

    def test_oversized_input_is_rejected_without_calling_ai(self):
        llm = FakeLLM(["never"])
        ctx = make_context(self.root, llm=llm)
        result = self._plugin(max_chars=50).handle(make_query("fix grammar: " + "word " * 40), ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "input-too-long")
        self.assertEqual(llm.calls, [])

    def test_tone_option_is_forwarded(self):
        llm = FakeLLM(["Formal."])
        ctx = make_context(self.root, llm=llm)
        result = self._plugin().handle(
            make_query("fix grammar: hey can u send it", options={"tone": "formal"}), ctx
        )
        self.assertEqual(result.data["tone"], "formal")
        self.assertIn("formal register", llm.calls[0]["system"])


if __name__ == "__main__":
    unittest.main()
