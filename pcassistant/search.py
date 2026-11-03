"""Shared retrieval and filename-search primitives.

Two distinct problems live here on purpose:

* ``SearchIndex`` ranks *content* (study notes, transcripts).
* ``fuzzy_filename_score`` ranks *names* (file search).

Keeping them separate prevents a content query from outranking an exact
filename match and keeps behaviour explainable.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .text import tokenize

DEFAULT_SKIP_DIRS = {
    "appdata",
    "node_modules",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
    "$recycle.bin",
    "system volume information",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "onedrivetemp",
    ".cache",
}

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(slots=True)
class Document:
    text: str
    title: str = ""
    source: str = ""
    section: str = ""
    locator: str = ""
    mtime: float = 0.0
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class Ranked:
    document: Document
    score: float
    confidence: float
    matched: tuple[str, ...] = ()


class SearchIndex:
    """A small BM25 index with title/section boosts.

    Built once per query set.  For a personal corpus this is fast enough and
    avoids maintaining an inverted index on disk before it is justified.
    """

    def __init__(self, documents: Sequence[Document]) -> None:
        self.documents = list(documents)
        self._token_lists = [tokenize(doc.text, keep_stopwords=False) for doc in self.documents]
        self._counts = [Counter(tokens) for tokens in self._token_lists]
        self._lengths = [len(tokens) for tokens in self._token_lists]
        self._df: Counter[str] = Counter()
        for counts in self._counts:
            self._df.update(counts.keys())
        total = sum(self._lengths)
        self._avgdl = (total / len(self._lengths)) if self._lengths else 0.0
        self._title_tokens = [
            set(tokenize(f"{doc.title} {doc.section}", keep_stopwords=False))
            for doc in self.documents
        ]

    def search(self, query: str, *, limit: int = 10) -> list[Ranked]:
        tokens = list(dict.fromkeys(tokenize(query, keep_stopwords=False)))
        if not tokens or not self.documents:
            return []

        total_docs = len(self.documents)
        idf: dict[str, float] = {}
        for token in tokens:
            frequency = self._df.get(token, 0)
            idf[token] = math.log(1 + (total_docs - frequency + 0.5) / (frequency + 0.5))

        phrase = re.sub(r"\s+", " ", query.strip().lower())
        k1, b = 1.2, 0.75
        ranked: list[Ranked] = []

        for index, doc in enumerate(self.documents):
            counts = self._counts[index]
            length = self._lengths[index] or 1
            score = 0.0
            matched: list[str] = []
            for token in tokens:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                matched.append(token)
                denominator = frequency + k1 * (1 - b + b * length / max(1.0, self._avgdl))
                score += idf[token] * (frequency * (k1 + 1)) / denominator
                if token in self._title_tokens[index]:
                    score += idf[token] * 0.9
            if not matched:
                continue
            coverage = len(set(matched)) / len(tokens)
            score += coverage * 1.4
            if len(phrase) >= 4 and phrase in doc.text.lower():
                score += 2.0
            if score <= 0:
                continue
            confidence = min(0.95, score / (score + 3.0))
            ranked.append(Ranked(doc, score, confidence, tuple(matched)))

        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:limit]


# ------------------------------------------------------------------ chunking


def chunk_markdown(text: str, *, target: int = 1200) -> list[tuple[str, str]]:
    """Split text into ``(section, chunk)`` pairs at paragraph boundaries."""

    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []

    section = ""
    paragraphs: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        for line in block.split("\n"):
            heading = HEADING_RE.match(line)
            if heading:
                section = heading.group(2).strip()[:120]
                break
        paragraphs.append((section, block))

    chunks: list[tuple[str, str]] = []
    buffer: list[str] = []
    buffer_section = ""
    size = 0

    def flush() -> None:
        nonlocal buffer, size, buffer_section
        if buffer:
            chunks.append((buffer_section, "\n\n".join(buffer).strip()))
        buffer = []
        size = 0

    for paragraph_section, paragraph in paragraphs:
        if not buffer:
            buffer_section = paragraph_section
        if len(paragraph) > target:
            flush()
            for piece in _split_long(paragraph, target):
                chunks.append((paragraph_section, piece))
            continue
        if size + len(paragraph) + 2 > target and buffer:
            flush()
            buffer_section = paragraph_section
        buffer.append(paragraph)
        size += len(paragraph) + 2

    flush()
    return [(sec, chunk) for sec, chunk in chunks if chunk]


def _split_long(text: str, target: int) -> list[str]:
    pieces: list[str] = []
    words = text.split()
    current: list[str] = []
    size = 0
    for word in words:
        if size + len(word) + 1 > target and current:
            pieces.append(" ".join(current))
            current = []
            size = 0
        current.append(word)
        size += len(word) + 1
    if current:
        pieces.append(" ".join(current))
    return pieces


# ------------------------------------------------------------- filename match


def fuzzy_filename_score(query: str, name: str) -> float:
    """Higher is better.  ``-1`` means no match.

    Subsequence matching is limited to short, single-word queries.  Without
    that limit "budget" matches almost any long path because the letters
    eventually appear in order.
    """

    q = (query or "").strip().lower()
    n = (name or "").strip().lower()
    if not q or not n:
        return -1.0
    if n == q:
        return 1000.0
    if Path(n).stem == q:
        return 980.0
    if n.startswith(q):
        return 900.0 - min(80, len(n) - len(q))
    position = n.find(q)
    if position >= 0:
        return 750.0 - min(150, position)

    query_tokens = [token for token in re.split(r"[^a-z0-9]+", q) if token]
    if query_tokens and all(token in n for token in query_tokens):
        return 650.0 - min(100, len(n) - len(q))

    if any(n.startswith(token) for token in query_tokens):
        return 600.0 - min(100, len(n) - len(q))

    # Word-initial acronym: "vsc" -> "visual studio code".
    initials = "".join(
        part[0] for part in re.split(r"[^a-z0-9]+", n) if part
    )
    if initials.startswith(q):
        return 560.0 - min(100, len(n) - len(q))

    return -1.0


def fuzzy_path_score(query: str, path: str) -> float:
    """Path matching without the subsequence trap.

    A full path contains almost every letter sequence, so only substring and
    token presence are meaningful here.
    """

    q = (query or "").strip().lower()
    p = (path or "").strip().lower()
    if not q or not p:
        return -1.0
    position = p.find(q)
    if position >= 0:
        return 700.0 - min(250, position)
    tokens = [token for token in re.split(r"[^a-z0-9]+", q) if token]
    if tokens and all(token in p for token in tokens):
        return 500.0 - min(150, len(p) - len(q))
    return -1.0


# --------------------------------------------------------------------- walker


@dataclass(slots=True)
class WalkResult:
    files: list[Path]
    dirs: list[Path] = field(default_factory=list)
    complete: bool = True
    stop_reason: str = ""

    @property
    def truncated(self) -> bool:
        return not self.complete


def walk_files(
    roots: Iterable[Path],
    *,
    max_depth: int = 6,
    skip_names: Iterable[str] = (),
    limit: int = 20000,
    deadline: float | None = None,
    max_seconds: float | None = None,
    include_dirs: bool = False,
    include_hidden: bool = False,
) -> WalkResult:
    """Bounded, non-following directory walk.

    Stops early on the deadline, the ``max_seconds`` budget, or the entry
    limit, and reports that the result is incomplete instead of pretending
    the filesystem ended.
    """

    skip = {name.lower() for name in DEFAULT_SKIP_DIRS}
    skip.update(name.lower() for name in skip_names if name)
    started = time.monotonic()
    effective_deadline = deadline
    if max_seconds:
        budget_deadline = started + max_seconds
        effective_deadline = (
            min(effective_deadline, budget_deadline)
            if effective_deadline is not None
            else budget_deadline
        )

    files: list[Path] = []
    dirs: list[Path] = []
    stack: list[tuple[Path, int]] = [(Path(root), 0) for root in roots]

    while stack:
        if effective_deadline is not None and time.monotonic() >= effective_deadline:
            return WalkResult(files, dirs, False, "time budget reached")
        if _entry_count(files, dirs, include_dirs) >= limit:
            return WalkResult(files, dirs, False, "entry limit reached")

        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue

        for entry in entries:
            if _entry_count(files, dirs, include_dirs) >= limit:
                return WalkResult(files, dirs, False, "entry limit reached")
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not include_hidden and entry.name.startswith("."):
                continue
            if is_directory:
                if depth >= max_depth or entry.name.lower() in skip:
                    continue
                if include_dirs:
                    dirs.append(Path(entry.path))
                stack.append((Path(entry.path), depth + 1))
            else:
                files.append(Path(entry.path))

    return WalkResult(files, dirs, True, "")


def _entry_count(files: list[Path], dirs: list[Path], include_dirs: bool) -> int:
    return len(files) + (len(dirs) if include_dirs else 0)


def is_probably_binary(path: Path, *, sample: int = 2048) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(sample)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    if not chunk:
        return False
    text_characters = bytes(range(32, 127)) + b"\n\r\t\f\b"
    non_text = sum(byte not in text_characters for byte in chunk)
    return non_text / len(chunk) > 0.30


def age_days(mtime: float, *, now: float | None = None) -> float:
    if not mtime:
        return 9999.0
    return max(0.0, ((now or time.time()) - mtime) / 86400.0)
