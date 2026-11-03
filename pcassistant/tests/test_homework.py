from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcassistant.llm import LLMError
from pcassistant.plugins.homework_search import HomeworkSearchPlugin

from .support import FakeLLM, make_config, make_context, make_query, write

BIOLOGY = """# Cell Division

## Mitosis

Mitosis is part of the cell cycle. It produces two genetically identical diploid
cells and is used for growth and repair.

## Meiosis

Meiosis produces four haploid gametes. The key difference between mitosis and
meiosis is the number of divisions and the chromosome count in the daughter cells.
"""


class HomeworkSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "school"
        write(self.root / "biology.md", BIOLOGY)
        write(
            self.root / "biology_review.md",
            "# Review\n\nMitosis and meiosis differ in the number of divisions and the "
            "chromosome count in daughter cells. Mitosis makes diploid cells; meiosis "
            "makes haploid gametes.",
        )
        write(self.root / "history.md", "# French Revolution\n\nThe revolution began in 1789.")
        self.ctx = make_context(Path(self.tmp.name))

    def _plugin(self, *, synthesize: bool = True):
        config = make_config(
            {
                "paths": {"homework_roots": [str(self.root)]},
                "plugins": {"homework_search": {"synthesize": synthesize}},
            }
        )
        return HomeworkSearchPlugin(config)

    def test_grounded_answer_has_citations(self):
        llm = FakeLLM(
            ["Mitosis makes two identical diploid cells [1]. Meiosis makes four haploid gametes [2]."]
        )
        ctx = make_context(Path(self.tmp.name), llm=llm)
        result = self._plugin().handle(
            make_query("what is the difference between mitosis and meiosis"), ctx
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "grounded-answer")
        self.assertIn("[1]", result.text)
        self.assertGreaterEqual(len(result.citations), 2)
        self.assertTrue(any(citation.source.endswith("biology.md") for citation in result.citations))
        self.assertIn("ONLY", llm.calls[0]["system"])

    def test_search_mode_without_ai(self):
        ctx = make_context(Path(self.tmp.name), llm=FakeLLM(available=False))
        result = self._plugin().handle(make_query("mitosis"), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "search")
        self.assertTrue(result.citations)
        self.assertTrue(any("biology" in citation.title.lower() for citation in result.citations))

    def test_synthesis_failure_degrades_to_sources(self):
        ctx = make_context(Path(self.tmp.name), llm=FakeLLM(error=LLMError("quota")))
        result = self._plugin().handle(make_query("what is mitosis"), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "search")
        self.assertTrue(result.data["notice"])

    def test_no_match_says_so_instead_of_guessing(self):
        ctx = make_context(Path(self.tmp.name), llm=FakeLLM(["should not be called"]))
        result = self._plugin().handle(make_query("photosynthesis light reactions"), ctx)
        self.assertTrue(result.ok)
        self.assertFalse(result.data["found"])
        self.assertEqual(result.citations, [])

    def test_synthesize_false_forces_search_mode(self):
        llm = FakeLLM(["should not be called"])
        ctx = make_context(Path(self.tmp.name), llm=llm)
        result = self._plugin(synthesize=False).handle(
            make_query("what is the difference between mitosis and meiosis"), ctx
        )
        self.assertEqual(result.mode, "search")
        self.assertEqual(llm.calls, [])

    def test_refusal_clears_citations(self):
        llm = FakeLLM(["Your notes don't cover that."])
        ctx = make_context(Path(self.tmp.name), llm=llm)
        result = self._plugin().handle(make_query("what is mitosis"), ctx)
        self.assertEqual(result.mode, "grounded-answer")
        self.assertFalse(result.data["covered"])
        self.assertEqual(result.citations, [])
        self.assertLessEqual(result.confidence, 0.2)

    def test_health_counts_indexed_files(self):
        plugin = self._plugin()
        plugin.handle(make_query("mitosis"), make_context(Path(self.tmp.name)))
        health = plugin.health(self.ctx)
        self.assertTrue(health.ok)
        self.assertIn("indexed", health.detail)


if __name__ == "__main__":
    unittest.main()
