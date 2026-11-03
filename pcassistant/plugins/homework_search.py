"""Homework / study-material search.

Retrieval is deterministic BM25 over chunked notes.  When the query is a
question and an LLM is available, the answer is synthesised *only* from the
retrieved excerpts with numbered citations.  If the notes do not cover the
question, the plugin says so instead of guessing.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ..extract import extract_text
from ..llm import LLMError
from ..models import Action, Citation, Health, PluginResult, Query
from ..plugin import Plugin, PluginContext
from ..search import Document, SearchIndex, chunk_markdown, walk_files
from ..storage import file_signature, read_json, write_json_atomic
from ..text import snippet, truncate

DEFAULT_ROOTS = (
    "~/Documents/School",
    "~/Documents/Classes",
    "~/Documents/Coursework",
    "~/Documents/Study",
    "~/OneDrive/Documents/School",
    "~/OneDrive/Documents/Classes",
    "~/OneDrive/Documents/Coursework",
    "~/OneDrive/Documents/Study",
)
SUFFIXES = {".md", ".markdown", ".txt", ".pdf", ".docx", ".pptx", ".html", ".htm", ".json"}
NOISE = {
    "homework", "note", "notes", "class", "classes", "study", "material",
    "materials", "find", "search", "show", "my", "about", "please",
}
INDEX_VERSION = 1
MAX_CHUNKS = 20_000
QUESTION_RE = re.compile(
    r"^(what|why|how|when|where|who|which|define|explain|compare|describe|list|summarize|summarise)\b",
    re.IGNORECASE,
)

_SYSTEM = """You are a study assistant working from the student's own notes.
Use ONLY the numbered excerpts provided. Do not use outside knowledge.
If the excerpts do not contain the answer, say exactly: "Your notes don't cover that."
Cite every claim with [1], [2], etc. Keep the answer under 160 words.
Plain text only, no markdown headings."""


class HomeworkSearchPlugin(Plugin):
    id = "homework_search"
    name = "Homework Search"
    description = "Search study guides, flashcards, and class notes; answer with citations."
    aliases = ("homework", "study", "notes", "homework_search")
    keywords = (
        "homework",
        "study guide",
        "study notes",
        "flashcards",
        "practice test",
        "class notes",
        "my notes",
    )
    priority = 120
    requires_network = False

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._cached_key: tuple | None = None
        self._cached_index: SearchIndex | None = None

    # ------------------------------------------------------------------ handler

    def handle(self, query: Query, ctx: PluginContext) -> PluginResult:
        roots = self._roots()
        if not roots:
            candidates = self._candidates()
            listing = "\n".join(f"  {path}" for path in candidates[:8])
            return PluginResult(
                plugin=self.id,
                ok=False,
                title="No study folders found",
                text=(
                    "Add paths.homework_roots in config.json, pointing at your school or study folders.\n\n"
                    f"Checked:\n{listing or '  (nothing configured)'}"
                ),
                error="homework-roots-missing",
                confidence=0.0,
            )

        tokens = _query_tokens(query.text)
        if not tokens:
            return PluginResult(
                plugin=self.id,
                title="Homework Search",
                text=(
                    "Ask a question or search for a topic from your notes.\n\n"
                    "Examples:\n  @homework what is the difference between mitosis and meiosis\n"
                    "  @homework chapter 4 vocabulary"
                ),
                mode="help",
                confidence=0.0,
            )

        files, partial = self._ensure_index(roots, ctx)
        documents, total_chunks = self._documents(files)
        if not documents:
            return PluginResult(
                plugin=self.id,
                ok=True,
                title="No readable study material",
                text=(
                    "The study folders exist, but no readable notes were found.\n\n"
                    + "\n".join(f"  {root}" for root in roots)
                ),
                actions=[Action("open", "Open study folder", str(roots[0]))],
                confidence=0.0,
                data={"roots": [str(root) for root in roots], "found": False},
            )

        index = self._search_index(files, documents, total_chunks)
        phrase = " ".join(tokens)
        ranked = index.search(phrase, limit=max(12, int(self.settings.get("max_results", 6)) * 4))

        selected: list[Any] = []
        per_source: dict[str, int] = {}
        limit = int(self.settings.get("max_results", 6))
        for item in ranked:
            source = item.document.source
            if per_source.get(source, 0) >= 2:
                continue
            per_source[source] = per_source.get(source, 0) + 1
            selected.append(item)
            if len(selected) >= limit:
                break

        if not selected:
            return PluginResult(
                plugin=self.id,
                ok=True,
                title="No matching study material",
                text=(
                    f"Nothing in {len(files)} note file(s) matched: {query.text.strip()}\n\n"
                    "Folders searched:\n" + "\n".join(f"  {root}" for root in roots)
                ),
                actions=[Action("open", "Open study folder", str(roots[0]))],
                confidence=0.0,
                mode="search",
                data={
                    "roots": [str(root) for root in roots],
                    "found": False,
                    "matches": 0,
                    "partial": partial,
                },
            )

        top = selected[0]
        notice = ""
        if (
            bool(self.settings.get("synthesize", True))
            and ctx.llm.available
            and _is_question(query.text)
            and top.confidence >= 0.25
        ):
            try:
                return self._synthesize(query, ctx, selected, roots, partial, len(files))
            except LLMError as exc:
                notice = f"AI synthesis was unavailable; showing source passages instead. {exc}"

        return self._search_result(
            query, selected, roots, partial, len(files), total_chunks, notice
        )

    # ------------------------------------------------------------------ output

    def _search_result(
        self,
        query: Query,
        selected: list[Any],
        roots: list[Path],
        partial: bool,
        file_count: int,
        total_chunks: int,
        notice: str,
    ) -> PluginResult:
        lines: list[str] = []
        citations: list[Citation] = []
        actions: list[Action] = []
        seen_sources: set[str] = set()

        for item in selected:
            doc = item.document
            label = doc.section or "notes"
            excerpt = snippet(doc.text, query.tokens, width=220)
            lines.append(f"{doc.title} › {label}\n  {excerpt}")
            citations.append(
                Citation(
                    title=doc.title,
                    source=doc.source,
                    locator=doc.section or doc.locator,
                    snippet=excerpt,
                )
            )
            if doc.source not in seen_sources and len(actions) < 4:
                seen_sources.add(doc.source)
                actions.append(Action("open", f"Open {doc.title}", doc.source))

        actions.append(Action("open", "Open study folder", str(roots[0])))
        title = f"Found {len(selected)} passage{'s' if len(selected) != 1 else ''} in {len(seen_sources)} file(s)"
        if partial:
            title += " · partial index"
        if notice:
            lines.append(notice)

        return PluginResult(
            plugin=self.id,
            ok=True,
            title=title,
            text="\n\n".join(lines),
            citations=citations,
            actions=actions,
            confidence=min(0.85, selected[0].confidence),
            mode="search",
            data={
                "roots": [str(root) for root in roots],
                "found": True,
                "matches": len(selected),
                "files": file_count,
                "chunks": total_chunks,
                "partial": partial,
                "synthesized": False,
                "notice": notice,
            },
        )

    def _synthesize(
        self,
        query: Query,
        ctx: PluginContext,
        selected: list[Any],
        roots: list[Path],
        partial: bool,
        file_count: int,
    ) -> PluginResult:
        excerpts = selected[:5]
        blocks: list[str] = []
        for number, item in enumerate(excerpts, start=1):
            doc = item.document
            heading = f"{doc.title} › {doc.section}" if doc.section else doc.title
            blocks.append(f"[{number}] {heading}\n{truncate(doc.text, 1100)}")

        prompt = (
            f"QUESTION: {query.text.strip()}\n\n"
            "EXCERPTS:\n\n" + "\n\n".join(blocks)
        )
        answer = ctx.llm.complete(
            prompt,
            system=_SYSTEM,
            max_tokens=650,
            temperature=0.2,
        ).strip()

        refusal = bool(
            re.search(
                r"(?:don'?t|do not|doesn'?t|does not|not)\s+(?:cover|contain|include|mention)",
                answer,
                re.IGNORECASE,
            )
        )

        cited_numbers = {
            int(match) for match in re.findall(r"\[(\d+)\]", answer)
            if 1 <= int(match) <= len(excerpts)
        }
        chosen = (
            [excerpts[number - 1] for number in sorted(cited_numbers)]
            if cited_numbers and not refusal
            else ([] if refusal else excerpts[:3])
        )

        citations: list[Citation] = []
        actions: list[Action] = []
        seen: set[str] = set()
        for item in chosen:
            doc = item.document
            citations.append(
                Citation(
                    title=doc.title,
                    source=doc.source,
                    locator=doc.section or doc.locator,
                    snippet=truncate(doc.text, 180),
                )
            )
            if doc.source not in seen and len(actions) < 4:
                seen.add(doc.source)
                actions.append(Action("open", f"Open {doc.title}", doc.source))
        actions.append(Action("open", "Open study folder", str(roots[0])))

        return PluginResult(
            plugin=self.id,
            ok=True,
            title="Answer from your notes",
            text=answer,
            citations=citations,
            actions=actions,
            confidence=0.2 if refusal else min(0.9, selected[0].confidence + 0.08),
            mode="grounded-answer",
            data={
                "roots": [str(root) for root in roots],
                "found": True,
                "covered": not refusal,
                "matches": len(selected),
                "files": file_count,
                "partial": partial,
                "synthesized": True,
                "model": ctx.llm.last_model,
                "cited": sorted(cited_numbers),
            },
        )

    # ------------------------------------------------------------------ index

    def _candidates(self) -> list[Path]:
        configured = self.config.paths("paths.homework_roots")
        candidates = configured or [Path(path).expanduser() for path in DEFAULT_ROOTS]
        seen: set[str] = set()
        result: list[Path] = []
        for path in candidates:
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result

    def _roots(self) -> list[Path]:
        return [path for path in self._candidates() if path.is_dir()]

    def _ensure_index(
        self, roots: list[Path], ctx: PluginContext
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        index_path = ctx.cache_dir / "homework_index.json"
        cached = read_json(index_path, {}) or {}
        files: dict[str, dict[str, Any]] = {}
        if isinstance(cached, dict) and cached.get("version") == INDEX_VERSION:
            stored = cached.get("files")
            if isinstance(stored, dict):
                files = {
                    str(key): value
                    for key, value in stored.items()
                    if isinstance(value, dict)
                }

        max_files = int(self.settings.get("max_files", 1500))
        max_bytes = int(self.settings.get("max_file_bytes", 5_000_000))
        walk = walk_files(
            roots,
            max_depth=8,
            limit=max(3000, max_files * 6),
            deadline=ctx.deadline,
        )
        candidates = [
            path
            for path in walk.files
            if path.suffix.lower() in SUFFIXES and _size_ok(path, max_bytes)
        ]
        current = {str(path) for path in candidates}
        dirty = False
        for stale in list(files):
            if stale not in current:
                files.pop(stale, None)
                dirty = True
        partial = not walk.complete
        if len(candidates) > max_files:
            partial = True
        total_chunks = sum(len(item.get("chunks", [])) for item in files.values())

        for count, path in enumerate(candidates[:max_files]):
            if count % 20 == 0:
                if ctx.expired:
                    partial = True
                    break
            signature = file_signature(path)
            key = str(path)
            existing = files.get(key)
            if existing and existing.get("sig") == signature:
                continue

            old_chunks = len(existing.get("chunks", [])) if existing else 0
            extracted = extract_text(path, max_chars=200_000)
            if not extracted.text.strip():
                files.pop(key, None)
                total_chunks -= old_chunks
                dirty = True
                continue

            chunks = chunk_markdown(extracted.text, target=1200)
            if not chunks:
                files.pop(key, None)
                total_chunks -= old_chunks
                dirty = True
                continue

            room = MAX_CHUNKS - (total_chunks - old_chunks)
            if room <= 0:
                partial = True
                break
            chunks = chunks[:room]
            files[key] = {
                "sig": signature,
                "mtime": path.stat().st_mtime if path.exists() else 0.0,
                "title": path.stem,
                "chunks": [{"section": section, "text": text} for section, text in chunks],
            }
            total_chunks = total_chunks - old_chunks + len(chunks)
            dirty = True

        if dirty:
            write_json_atomic(
                index_path,
                {
                    "version": INDEX_VERSION,
                    "built": time.time(),
                    "roots": [str(root) for root in roots],
                    "partial": partial,
                    "files": files,
                },
            )
        return files, partial

    def _documents(
        self, files: dict[str, dict[str, Any]]
    ) -> tuple[list[Document], int]:
        documents: list[Document] = []
        for path, item in files.items():
            title = item.get("title") or Path(path).stem
            for number, chunk in enumerate(item.get("chunks", []), start=1):
                text = chunk.get("text", "")
                if not text:
                    continue
                documents.append(
                    Document(
                        text=text,
                        title=title,
                        source=path,
                        section=chunk.get("section", ""),
                        locator=f"part {number}",
                        mtime=float(item.get("mtime", 0.0)),
                    )
                )
        return documents, len(documents)

    def _search_index(
        self,
        files: dict[str, dict[str, Any]],
        documents: list[Document],
        total_chunks: int,
    ) -> SearchIndex:
        key = (
            len(files),
            total_chunks,
            sum(len(doc.text) for doc in documents),
            round(sum(float(item.get("mtime", 0.0)) for item in files.values()), 3),
        )
        if self._cached_index is not None and self._cached_key == key:
            return self._cached_index
        self._cached_index = SearchIndex(documents)
        self._cached_key = key
        return self._cached_index

    def health(self, ctx: PluginContext) -> Health:
        roots = self._roots()
        if not roots:
            return Health(False, "No study folders exist; set paths.homework_roots.")
        index_path = ctx.cache_dir / "homework_index.json"
        cached = read_json(index_path, {}) or {}
        files = cached.get("files", {}) if isinstance(cached, dict) else {}
        chunks = sum(len(item.get("chunks", [])) for item in files.values() if isinstance(item, dict))
        return Health(
            True,
            f"{len(roots)} folder(s) · {len(files)} indexed file(s) · {chunks} chunk(s)",
        )


def _query_tokens(text: str) -> list[str]:
    from ..text import tokenize

    tokens = [token for token in tokenize(text or "", keep_stopwords=False) if token not in NOISE]
    return list(dict.fromkeys(tokens))


def _is_question(text: str) -> bool:
    value = (text or "").strip()
    return "?" in value or bool(QUESTION_RE.match(value))


def _size_ok(path: Path, limit: int) -> bool:
    try:
        return path.stat().st_size <= limit
    except OSError:
        return False
