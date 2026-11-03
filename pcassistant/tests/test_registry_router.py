from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from pcassistant.plugin import PluginContext
from pcassistant.registry import PluginRegistry
from pcassistant.router import Router, parse_query

from .support import ExplodingPlugin, FakeLLM, make_config, make_context, make_query, write


class QueryParsingTests(unittest.TestCase):
    def test_explicit_prefix_and_options(self):
        import types

        def resolve(name):
            return types.SimpleNamespace(id=name) if name in {"files", "grammar"} else None

        query = parse_query("@files budget --ext=pdf --reindex", resolve=resolve)
        self.assertEqual(query.hint, "files")
        self.assertEqual(query.text, "budget")
        self.assertEqual(query.options["ext"], "pdf")
        self.assertEqual(query.options["reindex"], "true")

    def test_plain_text_stays_intact(self):
        query = parse_query("explain C:\\Users\\me\\notes.txt")
        self.assertIsNone(query.hint)
        self.assertEqual(query.text, "explain C:\\Users\\me\\notes.txt")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        context = PluginContext(
            config=make_config(),
            llm=FakeLLM(available=False),
            logger=logging.getLogger("test.registry"),
            cache_dir=root / "cache",
        )
        self.registry = PluginRegistry(context.config, context, context.logger)
        self.report = self.registry.load()
        self.addCleanup(self.registry.close)
        self.router = Router(self.registry, context.logger)

    def test_all_builtin_plugins_load(self):
        expected = {
            "grammar_fixer",
            "gp7_search",
            "homework_search",
            "file_search",
            "downloads_summarizer",
        }
        self.assertEqual(set(self.report.loaded), expected)
        self.assertEqual(self.report.failed, [])

    def test_routes_natural_language(self):
        self.assertEqual(self.router.route("summarize my recent downloads").plugin.id, "downloads_summarizer")
        self.assertEqual(self.router.route("fix my grammar: i has went home").plugin.id, "grammar_fixer")
        self.assertEqual(self.router.route("find the budget file").plugin.id, "file_search")
        self.assertEqual(self.router.route("what is a monad").plugin, None)

    def test_explicit_route_wins(self):
        route = self.router.route("@gp7 cell membrane")
        self.assertEqual(route.plugin.id, "gp7_search")
        self.assertEqual(route.query.text, "cell membrane")

    def test_plugin_failure_is_isolated(self):
        plugin = ExplodingPlugin(self.registry.config)
        ctx = make_context(Path(self.tmp.name))
        result = plugin.run(make_query("do it"), ctx)
        self.assertFalse(result.ok)
        self.assertIn("could not complete", result.text)
        self.assertIn("RuntimeError", result.data["detail"])

    def test_disabled_plugin_is_not_executed(self):
        self.registry.config.data["plugins"]["file_search"]["enabled"] = False
        plugin = self.registry.get("file_search")
        result = self.registry.execute(plugin, make_query("budget"))
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.text.lower())


class ExternalPluginTests(unittest.TestCase):
    def test_external_file_is_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "hello.py",
                "from pcassistant.plugin import Plugin\n"
                "from pcassistant.models import PluginResult\n\n"
                "class HelloPlugin(Plugin):\n"
                "    id = 'hello'\n"
                "    name = 'Hello'\n"
                "    aliases = ('hi',)\n"
                "    def handle(self, query, ctx):\n"
                "        return PluginResult(plugin=self.id, text='hello world')\n",
            )
            context = PluginContext(
                config=make_config(),
                llm=FakeLLM(available=False),
                logger=logging.getLogger("test.external"),
                cache_dir=root / "cache",
            )
            registry = PluginRegistry(context.config, context, context.logger)
            self.addCleanup(registry.close)
            report = registry.load(extra_paths=[root])
            self.assertIn("hello", report.loaded)
            self.assertEqual(report.failed, [])

            router = Router(registry, context.logger)
            route = router.route("@hi")
            self.assertEqual(route.plugin.id, "hello")


if __name__ == "__main__":
    unittest.main()
