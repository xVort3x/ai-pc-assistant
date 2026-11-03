"""Shared test helpers.  No network, no real user folders, no API keys."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from pcassistant.config import Config
from pcassistant.models import Query
from pcassistant.plugin import Plugin, PluginContext
from pcassistant.text import tokenize


class FakeLLM:
    def __init__(self, responses=None, *, available: bool = True, error: Exception | None = None):
        self.available = available
        self.provider = "fake"
        self.last_model = "fake-model"
        self._responses = list(responses) if isinstance(responses, (list, tuple)) else [responses]
        self._error = error
        self.calls: list[dict] = []

    def models(self) -> list[str]:
        return ["fake-model"]

    def complete(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self._error is not None:
            raise self._error
        if not self._responses:
            return "Fake answer."
        return self._responses.pop(0)


def make_config(data: dict | None = None) -> Config:
    base = {"llm": {"enabled": False}}
    if data:
        base.update(data)
    return Config.from_dict(base)


def make_context(tmp: Path, *, llm=None, timeout: float = 30.0) -> PluginContext:
    logger = logging.getLogger(f"test.{id(tmp)}")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return PluginContext(
        config=make_config(),
        llm=llm or FakeLLM(available=False),
        logger=logger,
        cache_dir=Path(tmp) / "cache",
        deadline=time.monotonic() + timeout,
    )


def make_query(text: str, options: dict | None = None) -> Query:
    return Query(
        raw=text,
        text=text,
        options=options or {},
        tokens=tokenize(text, keep_stopwords=False),
    )


def write(path: Path, content: str, *, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)
    return path


class ExplodingPlugin(Plugin):
    id = "exploding"
    name = "Exploding"
    description = "Raises on purpose."

    def handle(self, query, ctx):
        raise RuntimeError("boom")
