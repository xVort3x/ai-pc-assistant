"""Builders for throwaway plugins used in tests."""
import json
import logging
from pathlib import Path

from assistant.config import Settings
from assistant.core.plugin_api import PluginContext

PLUGIN_SOURCE = '''
import time
from assistant.core.plugin_api import Plugin, SearchResult

class P(Plugin):
    def search(self, request):
        delay = {delay}
        if delay:
            time.sleep(delay)
        if {boom}:
            raise RuntimeError("boom")
        return [SearchResult(title="{pid}:" + request.query, score={score})]

def create_plugin(ctx):
    return P(ctx)
'''


def make_plugin(root: Path, pid: str, *, keyword=None, delay=0, boom=False, score=1,
                api_version=1, source=None, manifest_extra=None) -> Path:
    folder = root / pid
    folder.mkdir(parents=True)
    manifest = {"id": pid, "name": pid, "version": "1.0", "api_version": api_version}
    if keyword:
        manifest["keyword"] = keyword
    manifest.update(manifest_extra or {})
    (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "main.py").write_text(
        source or PLUGIN_SOURCE.format(pid=pid, delay=delay, boom=boom, score=score),
        encoding="utf-8")
    return folder


def context_factory(tmp_path: Path):
    settings = Settings(tmp_path / "config.json")

    def factory(plugin_id: str) -> PluginContext:
        return PluginContext(
            plugin_id=plugin_id, logger=logging.getLogger(plugin_id),
            data_dir=tmp_path / "data" / plugin_id, settings=settings.plugin(plugin_id),
            notify=lambda t, m: None, hide_window=lambda: None)
    return factory
