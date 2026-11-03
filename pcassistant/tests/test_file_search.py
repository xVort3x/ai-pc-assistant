from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcassistant.plugins.file_search import FileSearchPlugin

from .support import make_config, make_context, make_query, write


class FileSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "searchroot"
        write(self.root / "budget_2026.xlsx", "numbers")
        write(self.root / "meeting notes.txt", "agenda")
        write(self.root / "Reports" / "invoice.pdf", "pdf")
        (self.root / "Projects").mkdir()
        self.ctx = make_context(Path(self.tmp.name))

    def _plugin(self):
        config = make_config({"paths": {"file_search_roots": [str(self.root)]}})
        return FileSearchPlugin(config)

    def test_finds_file_by_name(self):
        result = self._plugin().handle(make_query("budget"), self.ctx)
        self.assertTrue(result.ok)
        self.assertIn("budget_2026.xlsx", result.text)
        self.assertTrue(result.actions[0].target.endswith("budget_2026.xlsx"))
        self.assertGreater(result.confidence, 0.5)

    def test_extension_filter_only(self):
        result = self._plugin().handle(make_query("ext:pdf"), self.ctx)
        self.assertTrue(result.ok)
        self.assertIn("invoice.pdf", result.text)
        self.assertNotIn("budget_2026.xlsx", result.text)

    def test_folder_filter(self):
        result = self._plugin().handle(make_query("kind:folder"), self.ctx)
        self.assertTrue(result.ok)
        self.assertIn("Projects", result.text)
        self.assertIn("Reports", result.text)
        self.assertNotIn("budget_2026.xlsx", result.text)

    def test_unknown_root_is_explained(self):
        result = self._plugin().handle(make_query("in:nope budget"), self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unknown-root")

    def test_index_is_persisted_and_health_reports_it(self):
        plugin = self._plugin()
        plugin.handle(make_query("budget"), self.ctx)
        self.assertTrue((self.ctx.cache_dir / "file_index.json").is_file())
        health = plugin.health(self.ctx)
        self.assertTrue(health.ok)
        self.assertIn("indexed", health.detail)

    def test_no_match_is_honest(self):
        result = self._plugin().handle(make_query("quantum"), self.ctx)
        self.assertTrue(result.ok)
        self.assertIn("No matches", result.title)
        self.assertEqual(result.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
