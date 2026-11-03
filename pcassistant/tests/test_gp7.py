from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcassistant.plugins.gp7_search import GP7SearchPlugin

from .support import make_config, make_context, make_query, write

SRT = """1
00:00:05,000 --> 00:00:09,000
The cell membrane controls what enters and leaves the cell.

2
00:00:12,000 --> 00:00:16,000
Diffusion and osmosis move water across the membrane.
"""


class GP7SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "gp7"
        self.root.mkdir()
        write(self.root / "lecture1.srt", SRT)
        (self.root / "lecture1.mp4").write_bytes(b"fake video")
        write(self.root / "notes.txt", "A plain transcript about recursion and base cases.")
        self.ctx = make_context(Path(self.tmp.name))

    def _plugin(self):
        config = make_config({"paths": {"gp7_root": str(self.root)}})
        return GP7SearchPlugin(config)

    def test_searches_transcript_with_timestamp(self):
        result = self._plugin().handle(make_query("cell membrane osmosis"), self.ctx)
        self.assertTrue(result.ok)
        self.assertGreaterEqual(len(result.citations), 1)
        locators = [citation.locator for citation in result.citations]
        self.assertTrue(any(locator in {"0:05", "0:12"} for locator in locators))
        self.assertTrue(any(action.target.endswith("lecture1.mp4") for action in result.actions))
        self.assertTrue(result.data["transcripts"] >= 2)

    def test_plain_text_transcript_is_indexed(self):
        result = self._plugin().handle(make_query("recursion"), self.ctx)
        self.assertTrue(result.ok)
        self.assertIn("recursion", result.text.lower())

    def test_no_match_is_explicit(self):
        result = self._plugin().handle(make_query("quantum chromodynamics"), self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["matches"], 0)
        self.assertEqual(result.confidence, 0.0)

    def test_missing_root_is_actionable(self):
        config = make_config({"paths": {"gp7_root": str(Path(self.tmp.name) / "missing")}})
        result = GP7SearchPlugin(config).handle(make_query("anything"), self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "gp7-root-not-found")

    def test_index_is_cached(self):
        plugin = self._plugin()
        plugin.handle(make_query("membrane"), self.ctx)
        index_file = self.ctx.cache_dir / "gp7_index.json"
        self.assertTrue(index_file.is_file())
        first = index_file.stat().st_mtime
        plugin.handle(make_query("osmosis"), self.ctx)
        self.assertEqual(first, index_file.stat().st_mtime)

    def test_health_reports_configuration(self):
        health = self._plugin().health(self.ctx)
        self.assertTrue(health.ok)
        self.assertIn("indexed", health.detail)


if __name__ == "__main__":
    unittest.main()
