from __future__ import annotations

import unittest

from pcassistant.config import Config, deep_merge
from pcassistant.extract import parse_timed_text, parse_timestamp
from pcassistant.models import Action, Citation, PluginResult
from pcassistant.search import Document, SearchIndex, fuzzy_filename_score, fuzzy_path_score
from pcassistant.text import mechanical_cleanup, tokenize


class ModelTests(unittest.TestCase):
    def test_result_round_trip(self):
        result = PluginResult(
            plugin="demo",
            title="Hi",
            text="Body",
            citations=[Citation("T", "C:/x", "1:20", "snip")],
            actions=[Action("copy", "Copy", "Body")],
            confidence=0.8,
        )
        payload = result.to_dict()
        self.assertEqual(payload["plugin"], "demo")
        self.assertEqual(payload["citations"][0]["locator"], "1:20")
        self.assertEqual(payload["actions"][0]["kind"], "copy")

    def test_failure_shape(self):
        failure = PluginResult.failure("x", "broke")
        self.assertFalse(failure.ok)
        self.assertEqual(failure.error, "broke")


class ConfigTests(unittest.TestCase):
    def test_deep_merge_preserves_siblings(self):
        merged = deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 9}})
        self.assertEqual(merged["a"], {"x": 1, "y": 9})

    def test_dotted_access_and_defaults(self):
        config = Config.from_dict({"paths": {"gp7_root": "C:/rec"}})
        self.assertEqual(config.get("paths.gp7_root"), "C:/rec")
        self.assertEqual(config.get("nope.missing", "fallback"), "fallback")
        self.assertTrue(config.bool("llm.enabled", True))

    def test_tokenization_drops_stopwords(self):
        self.assertEqual(tokenize("What is the cell membrane", keep_stopwords=False), ("cell", "membrane"))


class SearchTests(unittest.TestCase):
    def test_bm25_prefers_relevant_document(self):
        docs = [
            Document("Mitosis creates two identical diploid cells.", title="Mitosis"),
            Document("The French Revolution began in 1789.", title="History"),
        ]
        ranked = SearchIndex(docs).search("diploid mitosis")
        self.assertEqual(ranked[0].document.title, "Mitosis")
        self.assertGreater(ranked[0].score, 0)

    def test_filename_exact_and_acronym(self):
        self.assertEqual(fuzzy_filename_score("budget", "budget"), 1000.0)
        self.assertEqual(fuzzy_filename_score("budget", "budget.xlsx"), 980.0)
        self.assertGreater(fuzzy_filename_score("vsc", "Visual Studio Code"), 0)
        self.assertLess(fuzzy_filename_score("vsc", "invalid-escaped-unicode.toml"), 0)

    def test_path_matching_has_no_subsequence_trap(self):
        self.assertGreater(fuzzy_path_score("budget", "C:/Docs/budget.xlsx"), 0)
        self.assertLess(fuzzy_path_score("budget", "C:/Program Files/ffmpeg/bin"), 0)


class TextTests(unittest.TestCase):
    def test_mechanical_cleanup(self):
        cleaned, changes = mechanical_cleanup("i has  went too the store")
        self.assertTrue(cleaned.startswith("I has"))
        self.assertTrue(cleaned.endswith("."))
        self.assertTrue(changes)

    def test_parse_timestamp(self):
        self.assertEqual(parse_timestamp("01:02:03,500"), 3723.5)
        self.assertEqual(parse_timestamp(12.5), 12.5)
        self.assertIsNone(parse_timestamp("nope"))

    def test_parse_srt(self):
        raw = (
            "1\n00:00:05,000 --> 00:00:08,000\nCell membrane overview\n\n"
            "2\n00:00:12,500 --> 00:00:15,000\nDiffusion and osmosis\n"
        )
        segments = parse_timed_text(raw)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["label"], "0:05")
        self.assertEqual(segments[1]["start"], 12.5)

    def test_parse_whisper_json(self):
        raw = '{"segments": [{"start": 3.0, "text": "hello"}, {"start": 8.0, "text": "world"}]}'
        segments = parse_timed_text(raw)
        self.assertEqual([segment["text"] for segment in segments], ["hello", "world"])
        self.assertEqual(segments[1]["label"], "0:08")


if __name__ == "__main__":
    unittest.main()
