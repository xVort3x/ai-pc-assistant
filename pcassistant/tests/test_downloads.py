from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from pcassistant.llm import LLMError
from pcassistant.plugins.downloads_summarizer import DownloadsSummarizerPlugin

from .support import FakeLLM, make_config, make_context, make_query, write


class DownloadsSummarizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "Downloads"
        self.invoice = write(
            self.root / "invoice_acme.txt",
            "Invoice from Acme. Amount due: $420.00. Due date: 12 November 2026.",
        )
        write(self.root / "archive.zip", "not a real zip")
        self.ctx = make_context(Path(self.tmp.name))

    def _plugin(self, **settings):
        data = {
            "paths": {"downloads": str(self.root)},
            "plugins": {"downloads_summarizer": settings},
        }
        return DownloadsSummarizerPlugin(make_config(data))

    def test_ai_digest(self):
        llm = FakeLLM(["Invoice from Acme for $420, due 12 November 2026."])
        ctx = make_context(Path(self.tmp.name), llm=llm)
        result = self._plugin().handle(make_query("summarize recent downloads"), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "ai-digest")
        self.assertIn("invoice_acme.txt", result.text)
        self.assertIn("$420", result.text)
        self.assertEqual(result.data["ai_used"], 1)
        self.assertTrue(any(citation.source.endswith("invoice_acme.txt") for citation in result.citations))
        self.assertNotIn("archive.zip", result.text)

    def test_cache_avoids_second_ai_call(self):
        first_llm = FakeLLM(["Invoice summary."])
        self._plugin().handle(
            make_query("summarize downloads"), make_context(Path(self.tmp.name), llm=first_llm)
        )
        second_llm = FakeLLM(error=LLMError("should not be called"))
        result = self._plugin().handle(
            make_query("summarize downloads"), make_context(Path(self.tmp.name), llm=second_llm)
        )
        self.assertEqual(result.mode, "ai-digest")
        self.assertEqual(second_llm.calls, [])

    def test_metadata_fallback_without_ai(self):
        ctx = make_context(Path(self.tmp.name), llm=FakeLLM(available=False))
        result = self._plugin().handle(make_query("summarize downloads"), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "metadata-digest")
        self.assertIn("invoice_acme.txt", result.text)
        self.assertTrue(result.data["notices"])

    def test_filename_filter(self):
        ctx = make_context(Path(self.tmp.name), llm=FakeLLM(available=False))
        result = self._plugin().handle(make_query("summarize downloads invoice"), ctx)
        self.assertIn("invoice_acme.txt", result.text)
        self.assertNotIn("archive.zip", result.text)

    def test_empty_folder_is_explicit(self):
        empty = Path(self.tmp.name) / "Empty"
        empty.mkdir()
        config = make_config(
            {"paths": {"downloads": str(empty)}, "plugins": {"downloads_summarizer": {}}}
        )
        result = DownloadsSummarizerPlugin(config).handle(
            make_query("summarize downloads"), make_context(Path(self.tmp.name))
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 0)
        self.assertIn("No recent downloads", result.title)

    def test_days_filter_excludes_old_file(self):
        old = write(self.root / "old_receipt.txt", "old")
        stamp = time.time() - 10 * 86400
        import os

        os.utime(old, (stamp, stamp))
        ctx = make_context(Path(self.tmp.name), llm=FakeLLM(available=False))
        result = self._plugin(days=3).handle(make_query("summarize downloads"), ctx)
        self.assertNotIn("old_receipt.txt", result.text)


if __name__ == "__main__":
    unittest.main()
