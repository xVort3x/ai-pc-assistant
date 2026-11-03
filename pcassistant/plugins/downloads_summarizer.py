"""Recent downloads digest.

Summaries are cached by file signature so re-running does not re-spend AI
quota.  When extraction or the model is unavailable the plugin still produces
a useful metadata digest and labels it honestly.
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
from ..storage import file_signature, read_json, write_json_atomic
from ..text import human_size, relative_time, snippet, truncate

SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md",
    ".csv", ".json", ".html", ".htm", ".py", ".js",
}
TEMP_SUFFIXES = {".crdownload", ".part", ".tmp", ".download", ".partial"}
NOISE = {
    "summarize", "summarise", "summary", "downloads", "download", "recent",
    "new", "files", "file", "my", "the", "last", "please", "digest", "today",
    "yesterday", "week",
}
CACHE_VERSION = 1

_SYSTEM = """Summarize the downloaded file for a short digest.
Return one plain-text sentence of at most 30 words.
Say what the file is and the most important concrete detail: amount, date,
topic, version, sender, or purpose. No preamble, no markdown, no quotes.
If the content is boilerplate or unreadable, say what it appears to be."""

_DAYS_RE = re.compile(r"\bdays:(\d+)\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit:(\d+)\b", re.IGNORECASE)


class DownloadsSummarizerPlugin(Plugin):
    id = "downloads_summarizer"
    name = "Downloads Summarizer"
    description = "Summarize recent downloads, with dates and file links."
    aliases = ("downloads", "summarize_downloads", "downloads_summarizer")
    keywords = (
        "summarize downloads",
        "summarise downloads",
        "recent downloads",
        "what did i download",
        "new downloads",
    )
    priority = 115
    requires_network = True

    # ------------------------------------------------------------------ handler

    def handle(self, query: Query, ctx: PluginContext) -> PluginResult:
        folder = self.config.downloads_dir
        if not folder.is_dir():
            return PluginResult(
                plugin=self.id,
                ok=False,
                title="Downloads folder not found",
                text=f"The configured downloads folder does not exist:\n{folder}",
                error="downloads-missing",
                confidence=0.0,
            )

        days, limit, tokens, all_time = _parse_request(query, self.settings)
        files = _recent_files(folder, days, limit, tokens, all_time)
        if not files:
            scope = "all time" if all_time else f"the last {days} day{'s' if days != 1 else ''}"
            return PluginResult(
                plugin=self.id,
                ok=True,
                title="No recent downloads",
                text=f"No matching downloads in {scope}.\n\nFolder: {folder}",
                actions=[Action("open", "Open Downloads", str(folder))],
                confidence=0.0,
                mode="digest",
                data={"folder": str(folder), "count": 0, "days": days},
            )

        cache_path = ctx.cache_dir / "download_summaries.json"
        cache = _load_cache(cache_path)
        model = str(self.config.get("llm.model", ""))
        ttl_days = int(self.settings.get("summary_ttl_days", 30))
        max_chars = int(self.settings.get("max_chars", 12000))
        now = time.time()

        lines: list[str] = []
        citations: list[Citation] = []
        actions: list[Action] = [Action("open", "Open Downloads", str(folder))]
        ai_used = 0
        notices: list[str] = []
        cache_dirty = False
        partial = False

        for path in files:
            if ctx.expired:
                partial = True
                notices.append("Stopped early to stay inside the time budget.")
                break

            try:
                stat = path.stat()
            except OSError:
                continue
            signature = file_signature(path)
            cached = cache.get(str(path))
            summary = ""
            kind = ""

            if (
                isinstance(cached, dict)
                and cached.get("sig") == signature
                and cached.get("model", "") == model
                and now - float(cached.get("at", 0)) <= ttl_days * 86400
            ):
                summary = str(cached.get("summary", "")).strip()
                kind = str(cached.get("kind", "")).strip()
                if summary:
                    ai_used += 1

            if not summary:
                extracted = extract_text(path, max_chars=max_chars)
                kind = extracted.kind or path.suffix.lstrip(".") or "file"
                can_use_ai = (
                    ctx.llm.available
                    and bool(extracted.text.strip())
                    and (ctx.remaining() is None or (ctx.remaining() or 0) > 15)
                )
                if can_use_ai:
                    try:
                        summary = ctx.llm.complete(
                            f"FILE: {path.name}\nTYPE: {kind}\nCONTENT:\n{extracted.text}",
                            system=_SYSTEM,
                            max_tokens=120,
                            temperature=0.2,
                        )
                        summary = _clean_summary(summary)
                        if summary:
                            ai_used += 1
                            cache[str(path)] = {
                                "sig": signature,
                                "summary": summary,
                                "kind": kind,
                                "model": model,
                                "at": now,
                            }
                            cache_dirty = True
                    except LLMError as exc:
                        notices.append(f"{path.name}: AI summary unavailable ({exc})")
                        summary = ""

                if not summary:
                    summary = _metadata_summary(path, stat, extracted, tokens)

            label = f"{path.name} — {relative_time(stat.st_mtime, now=now)} · {human_size(stat.st_size)}"
            lines.append(f"{label}\n   {truncate(summary, 320)}")
            citations.append(
                Citation(
                    title=path.name,
                    source=str(path),
                    locator=relative_time(stat.st_mtime, now=now),
                    snippet=truncate(summary, 180),
                )
            )
            if len(actions) < 6:
                actions.append(Action("open", f"Open {path.name}", str(path)))

        if cache_dirty:
            _save_cache(cache_path, cache, now)

        if ai_used == len(files):
            mode, confidence = "ai-digest", 0.85
        elif ai_used:
            mode, confidence = "mixed-digest", 0.68
        else:
            mode, confidence = "metadata-digest", 0.4
            if not ctx.llm.available:
                notices.append("No AI key configured; showing metadata and previews.")

        scope = "all time" if all_time else f"last {days} day{'s' if days != 1 else ''}"
        title = f"Downloads · {scope} · {len(files)} file{'s' if len(files) != 1 else ''}"
        if partial:
            title += " · partial"

        return PluginResult(
            plugin=self.id,
            ok=True,
            title=title,
            text="\n\n".join(lines),
            citations=citations,
            actions=actions,
            confidence=confidence,
            mode=mode,
            data={
                "folder": str(folder),
                "days": days,
                "count": len(files),
                "ai_used": ai_used,
                "notices": notices[:10],
                "partial": partial,
            },
        )

    def health(self, ctx: PluginContext) -> Health:
        folder = self.config.downloads_dir
        if not folder.is_dir():
            return Health(False, f"Downloads folder not found: {folder}")
        cached = read_json(ctx.cache_dir / "download_summaries.json", {}) or {}
        count = len(cached.get("entries", {})) if isinstance(cached, dict) else 0
        return Health(True, f"{folder} · {count} cached summar(ies)")


# --------------------------------------------------------------------- helpers


def _parse_request(query: Query, settings: dict[str, Any]) -> tuple[int, int, list[str], bool]:
    days = int(settings.get("days", 7))
    limit = int(settings.get("limit", 8))
    all_time = False
    text = query.text or ""

    match = _DAYS_RE.search(text)
    if match:
        days = max(1, int(match.group(1)))
        text = _DAYS_RE.sub(" ", text)
    match = _LIMIT_RE.search(text)
    if match:
        limit = max(1, min(50, int(match.group(1))))
        text = _LIMIT_RE.sub(" ", text)

    lowered = text.lower()
    if re.search(r"\b(all|everything)\b", lowered):
        all_time = True
        text = re.sub(r"\b(all|everything)\b", " ", text, flags=re.IGNORECASE)
    elif "today" in query.tokens:
        days = 1
    elif "yesterday" in query.tokens:
        days = 2

    if query.options.get("days"):
        try:
            days = max(1, int(query.options["days"]))
        except ValueError:
            pass
    if query.options.get("limit"):
        try:
            limit = max(1, min(50, int(query.options["limit"])))
        except ValueError:
            pass
    if "all" in query.options:
        all_time = True

    from ..text import tokenize

    tokens = [token for token in tokenize(text, keep_stopwords=False) if token not in NOISE]
    return days, limit, tokens, all_time


def _recent_files(
    folder: Path, days: int, limit: int, tokens: list[str], all_time: bool
) -> list[Path]:
    now = time.time()
    cutoff = 0 if all_time else now - days * 86400
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []

    candidates: list[tuple[float, Path]] = []
    for path in entries:
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        if path.name.startswith(".") or path.name.startswith("~$"):
            continue
        if path.suffix.lower() in TEMP_SUFFIXES:
            continue
        if path.suffix.lower() not in SUFFIXES:
            continue
        if stat.st_mtime < cutoff:
            continue
        if tokens and not all(token in path.name.lower() for token in tokens):
            continue
        candidates.append((stat.st_mtime, path))

    candidates.sort(reverse=True)
    return [path for _, path in candidates[: max(1, limit)]]


def _metadata_summary(path: Path, stat, extracted, tokens: list[str]) -> str:
    bits = [extracted.kind or path.suffix.lstrip(".") or "file"]
    if extracted.note:
        bits.append(extracted.note)
    elif extracted.text.strip():
        bits.append("Preview: " + snippet(extracted.text, tokens or [path.stem], width=150))
    else:
        bits.append("No readable text.")
    return " · ".join(bits)


def _clean_summary(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```") and value.endswith("```"):
        value = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    value = value.strip().strip('"').strip("'").strip()
    value = re.sub(r"\s+", " ", value)
    return truncate(value, 320)


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    raw = read_json(path, {}) or {}
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {str(key): value for key, value in entries.items() if isinstance(value, dict)}


def _save_cache(path: Path, entries: dict[str, dict[str, Any]], now: float) -> None:
    if len(entries) > 500:
        newest = sorted(
            entries.items(), key=lambda item: float(item[1].get("at", 0)), reverse=True
        )[:500]
        entries = dict(newest)
    write_json_atomic(
        path,
        {"version": CACHE_VERSION, "saved": now, "entries": entries},
    )
