"""Fast filename / folder search across Desktop, Documents and Downloads.

Design choices:

* Names, not contents.  Content search belongs to the homework plugin.
* A bounded walk (depth + entry cap + time budget) with a disk cache.
* A partial scan reports itself as partial instead of silently returning a
  truncated view of the filesystem.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from ..models import Action, Health, PluginResult, Query
from ..plugin import Plugin, PluginContext
from ..search import age_days, fuzzy_filename_score, fuzzy_path_score, walk_files
from ..storage import read_json, write_json_atomic
from ..text import human_size, parse_duration, relative_time, tokenize

INDEX_VERSION = 2
NOISE = {"find", "search", "show", "open", "my", "the", "a", "an", "file", "files", "folder", "folders", "please"}

_INLINE_EXT = re.compile(r"\b(?:ext|type):([a-z0-9,]+)\b", re.IGNORECASE)
_INLINE_IN = re.compile(r"\b(?:in|root):([^\s]+)", re.IGNORECASE)
_INLINE_RECENT = re.compile(r"\brecent:(\d+[smhdw])\b", re.IGNORECASE)
_INLINE_KIND = re.compile(r"\bkind:(file|folder)s?\b", re.IGNORECASE)


class FileSearchPlugin(Plugin):
    id = "file_search"
    name = "File Search"
    description = "Find files and folders by name on Desktop, Documents, and Downloads."
    aliases = ("files", "file", "find", "file_search")
    keywords = (
        "find file",
        "find files",
        "search files",
        "search for file",
        "where is",
        "locate",
        "my files",
    )
    priority = 100

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._index: list[dict[str, Any]] | None = None
        self._index_mtime: float | None = None
        self._index_roots: tuple[str, ...] = ()
        self._index_complete = False

    # ------------------------------------------------------------------ handler

    def handle(self, query: Query, ctx: PluginContext) -> PluginResult:
        roots = self._roots()
        if not roots:
            return PluginResult(
                plugin=self.id,
                ok=False,
                title="No searchable folders",
                text=(
                    "None of the configured search roots exist. Set paths.file_search_roots "
                    "in config.json to folders you want to search."
                ),
                error="file-roots-missing",
                confidence=0.0,
            )

        text, filters = _parse_filters(query)
        tokens = [token for token in tokenize(text, keep_stopwords=False) if token not in NOISE]
        if not tokens and not filters:
            return PluginResult(
                plugin=self.id,
                title="File Search",
                text=(
                    "Search by filename. Filters work too.\n\n"
                    "Examples:\n  @files budget 2026\n  @files ext:pdf invoice\n"
                    "  @files in:downloads recent:7d\n  @files kind:folder taxes"
                ),
                mode="help",
                confidence=0.0,
            )

        selected_roots = roots
        if filters.get("in"):
            resolved = _resolve_root(str(filters["in"]), roots)
            if resolved is None:
                names = ", ".join(root.name for root in roots)
                return PluginResult(
                    plugin=self.id,
                    ok=False,
                    title="Unknown folder",
                    text=f"I could not find a search root called '{filters['in']}'. Available: {names}",
                    error="unknown-root",
                    confidence=0.0,
                )
            selected_roots = [resolved]

        entries, partial, index_age = self._load_or_build(roots, selected_roots, ctx, reindex="reindex" in query.options)

        filtered = _filter_entries(entries, filters)
        if not filtered:
            return PluginResult(
                plugin=self.id,
                ok=True,
                title="No matches",
                text=f"No files or folders matched: {query.text.strip()}\n\nSearched: "
                + ", ".join(root.name for root in selected_roots),
                actions=[Action("open", "Open search folder", str(selected_roots[0]))],
                confidence=0.0,
                mode="search",
                data={
                    "roots": [str(root) for root in selected_roots],
                    "partial": partial,
                    "indexed": len(entries),
                },
            )

        ranked = _rank(filtered, tokens)
        if not ranked:
            return PluginResult(
                plugin=self.id,
                ok=True,
                title="No matches",
                text=f"No files or folders matched: {query.text.strip()}\n\nSearched: "
                + ", ".join(root.name for root in selected_roots),
                actions=[Action("open", "Open search folder", str(selected_roots[0]))],
                confidence=0.0,
                mode="search",
                data={
                    "roots": [str(root) for root in selected_roots],
                    "partial": partial,
                    "indexed": len(entries),
                    "matched": 0,
                },
            )

        limit = int(filters.get("limit") or self.config.number("file_search.default_limit", 12))
        results = [entry for _, entry in ranked[: max(1, min(50, limit))]]

        lines: list[str] = []
        actions: list[Action] = []
        for entry in results:
            kind = "folder" if entry.get("kind") == "folder" else _kind_label(entry)
            size = "" if entry.get("kind") == "folder" else f" · {human_size(entry.get('size', 0))}"
            modified = relative_time(float(entry.get("mtime", 0)))
            lines.append(
                f"{entry['name']}\n  {_short_dir(entry['dir'])} · {kind}{size} · {modified}"
            )
            if len(actions) < 8:
                actions.append(Action("open", f"Open {entry['name']}", entry["path"]))
        if results:
            actions.append(Action("reveal", "Show in Explorer", results[0]["path"]))

        title = f"Found {len(results)} item{'s' if len(results) != 1 else ''}"
        if partial:
            title += " · partial scan"
        confidence = (
            min(0.95, 0.42 + ranked[0][0] / 1200.0) if tokens else 0.35
        )

        return PluginResult(
            plugin=self.id,
            ok=True,
            title=title,
            text="\n\n".join(lines),
            actions=actions,
            confidence=confidence,
            mode="search",
            data={
                "roots": [str(root) for root in selected_roots],
                "filters": filters,
                "indexed": len(entries),
                "matched": len(filtered),
                "partial": partial,
                "index_age_seconds": index_age,
            },
        )

    # ------------------------------------------------------------------- index

    def _roots(self) -> list[Path]:
        configured = self.config.paths("paths.file_search_roots")
        candidates = configured or [
            Path.home() / "Desktop",
            Path.home() / "Documents",
            self.config.downloads_dir,
            Path.home() / "OneDrive" / "Desktop",
            Path.home() / "OneDrive" / "Documents",
        ]
        seen: set[str] = set()
        result: list[Path] = []
        for path in candidates:
            key = os.path.normcase(str(path))
            if key in seen or not path.is_dir():
                continue
            seen.add(key)
            result.append(path)
        return result

    def _load_or_build(
        self,
        all_roots: list[Path],
        selected_roots: list[Path],
        ctx: PluginContext,
        *,
        reindex: bool,
    ) -> tuple[list[dict[str, Any]], bool, float]:
        index_path = ctx.cache_dir / "file_index.json"
        roots_key = tuple(sorted(os.path.normcase(str(root)) for root in all_roots))
        try:
            disk_mtime = index_path.stat().st_mtime
        except OSError:
            disk_mtime = 0.0

        if (
            not reindex
            and self._index is not None
            and self._index_complete
            and self._index_mtime == disk_mtime
            and self._index_roots == roots_key
        ):
            return (
                self._slice(self._index, selected_roots),
                not self._index_complete,
                max(0.0, time.time() - disk_mtime) if disk_mtime else 0.0,
            )

        if not reindex:
            cached = read_json(index_path, {}) or {}
            if (
                isinstance(cached, dict)
                and cached.get("version") == INDEX_VERSION
                and tuple(sorted(cached.get("roots", []))) == roots_key
            ):
                age = time.time() - float(cached.get("built", 0))
                ttl = self.config.number("file_search.cache_ttl_seconds", 3600)
                if age <= ttl:
                    entries = [entry for entry in cached.get("entries", []) if isinstance(entry, dict)]
                    self._index = entries
                    self._index_mtime = disk_mtime
                    self._index_roots = roots_key
                    self._index_complete = bool(cached.get("complete", True))
                    return self._slice(entries, selected_roots), False, age

        entries, complete = self._build(all_roots, ctx)
        if complete:
            write_json_atomic(
                index_path,
                {
                    "version": INDEX_VERSION,
                    "built": time.time(),
                    "roots": list(roots_key),
                    "complete": True,
                    "entries": entries,
                },
            )
            try:
                self._index_mtime = index_path.stat().st_mtime
            except OSError:
                self._index_mtime = 0.0
        else:
            self._index_mtime = 0.0
        self._index = entries
        self._index_roots = roots_key
        self._index_complete = complete
        return self._slice(entries, selected_roots), not complete, 0.0

    def _build(self, roots: list[Path], ctx: PluginContext) -> tuple[list[dict[str, Any]], bool]:
        max_entries = int(self.config.number("file_search.max_entries", 20000))
        max_depth = int(self.config.number("file_search.max_depth", 6))
        budget = self.config.number("file_search.scan_budget_seconds", 2.5)
        if ctx.remaining() is not None:
            budget = min(budget, max(0.2, ctx.remaining() or 0.2))

        walk = walk_files(
            roots,
            max_depth=max_depth,
            skip_names=self.config.get("file_search.skip_names", []),
            limit=max_entries,
            deadline=ctx.deadline,
            max_seconds=budget,
            include_dirs=True,
        )
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in walk.files:
            if path.name.lower() in {"desktop.ini", "thumbs.db"} or path.name.startswith("~$"):
                continue
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            entry = _entry(path, "file")
            if entry:
                seen.add(key)
                entries.append(entry)
        for path in walk.dirs:
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            entry = _entry(path, "folder")
            if entry:
                seen.add(key)
                entries.append(entry)
        return entries, walk.complete

    @staticmethod
    def _slice(entries: list[dict[str, Any]], roots: list[Path]) -> list[dict[str, Any]]:
        if len(roots) == 1:
            prefix = os.path.normcase(str(roots[0])).rstrip("\\/") + os.sep
            return [
                entry for entry in entries
                if os.path.normcase(entry.get("path", "")).startswith(prefix)
            ]
        return entries

    def health(self, ctx: PluginContext) -> Health:
        roots = self._roots()
        if not roots:
            return Health(False, "No search roots exist.")
        index_path = ctx.cache_dir / "file_index.json"
        cached = read_json(index_path, {}) or {}
        count = len(cached.get("entries", [])) if isinstance(cached, dict) else 0
        built = float(cached.get("built", 0)) if isinstance(cached, dict) else 0.0
        age_text = relative_time(built) if built else "not built yet"
        return Health(
            True,
            f"{len(roots)} root(s) · {count} indexed item(s) · index {age_text}",
        )


# ------------------------------------------------------------------- helpers


def _parse_filters(query: Query) -> tuple[str, dict[str, Any]]:
    text = query.text or ""
    filters: dict[str, Any] = {}

    match = _INLINE_EXT.search(text)
    if match:
        filters["ext"] = [item if item.startswith(".") else f".{item}" for item in match.group(1).split(",")]
        text = _INLINE_EXT.sub(" ", text)

    match = _INLINE_IN.search(text)
    if match:
        filters["in"] = match.group(1)
        text = _INLINE_IN.sub(" ", text)

    match = _INLINE_RECENT.search(text)
    if match:
        filters["recent_seconds"] = parse_duration(match.group(1))
        text = _INLINE_RECENT.sub(" ", text)

    match = _INLINE_KIND.search(text)
    if match:
        filters["kind"] = match.group(1).lower()
        text = _INLINE_KIND.sub(" ", text)

    options = query.options
    if options.get("ext"):
        filters["ext"] = [
            item if item.startswith(".") else f".{item}"
            for item in options["ext"].split(",")
        ]
    if options.get("in"):
        filters["in"] = options["in"]
    if options.get("recent"):
        filters["recent_seconds"] = parse_duration(options["recent"])
    if options.get("kind"):
        filters["kind"] = options["kind"].lower()
    if options.get("limit"):
        try:
            filters["limit"] = int(options["limit"])
        except ValueError:
            pass

    return re.sub(r"\s{2,}", " ", text).strip(), filters


def _resolve_root(value: str, roots: list[Path]) -> Path | None:
    needle = value.strip().lower()
    for root in roots:
        if needle in {root.name.lower(), str(root).lower()}:
            return root
    for root in roots:
        if needle in str(root).lower():
            return root
    return None


def _filter_entries(entries: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    extensions = {item.lower() for item in filters.get("ext", [])}
    kind = filters.get("kind")
    recent = filters.get("recent_seconds")
    now = time.time()
    result: list[dict[str, Any]] = []
    for entry in entries:
        if kind and entry.get("kind") != kind:
            continue
        if extensions and Path(entry.get("name", "")).suffix.lower() not in extensions:
            continue
        if recent and (now - float(entry.get("mtime", 0))) > recent:
            continue
        result.append(entry)
    return result


def _rank(entries: list[dict[str, Any]], tokens: list[str]) -> list[tuple[float, dict[str, Any]]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    phrase = " ".join(tokens)
    now = time.time()
    for entry in entries:
        score = 0.0
        if tokens:
            name_score = fuzzy_filename_score(phrase, entry.get("name", ""))
            path_score = fuzzy_path_score(phrase, entry.get("path", ""))
            score = max(name_score, path_score * 0.7)
            if score < 0:
                continue
            score += max(0.0, 40.0 - age_days(float(entry.get("mtime", 0)), now=now) * 1.5)
        ranked.append((score, entry))
    ranked.sort(
        key=lambda pair: (pair[0], float(pair[1].get("mtime", 0))),
        reverse=True,
    )
    return ranked


def _entry(path: Path, kind: str) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "name": path.name,
        "path": str(path),
        "dir": str(path.parent),
        "kind": kind,
        "mtime": stat.st_mtime,
        "size": stat.st_size if kind == "file" else 0,
    }


def _kind_label(entry: dict[str, Any]) -> str:
    suffix = Path(entry.get("name", "")).suffix.lower().lstrip(".")
    return suffix.upper() if suffix else "file"


def _short_dir(directory: str) -> str:
    path = Path(directory)
    parts = path.parts
    if len(parts) <= 2:
        return str(path)
    return str(Path(parts[-2]) / parts[-1])
