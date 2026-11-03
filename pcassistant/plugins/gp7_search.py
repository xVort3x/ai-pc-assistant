"""GP7 recording search.

Searches local transcripts produced by the recording/transcription pipeline
and links each hit back to its recording.  The index is a JSON cache keyed by
file signature, so re-runs only parse new or changed transcripts.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..extract import parse_timed_text
from ..models import Action, Citation, Health, PluginResult, Query
from ..plugin import Plugin, PluginContext
from ..search import fuzzy_filename_score, walk_files
from ..storage import file_signature, read_json, write_json_atomic
from ..text import snippet

TRANSCRIPT_SUFFIXES = {".txt", ".md", ".srt", ".vtt", ".json"}
RECORDING_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wav", ".mp3", ".m4a", ".flac"}
DEFAULT_ROOTS = (
    "~/Videos/GP7",
    "~/Videos/GoPro",
    "~/Recordings",
    "~/Documents/GP7",
    "~/OneDrive/Documents/GP7",
)
NOISE = {
    "find", "search", "show", "open", "my", "me", "the", "a", "an",
    "about", "for", "in", "on", "gp7", "recording", "recordings",
    "transcript", "transcripts", "video", "videos", "please",
}
MAX_TRANSCRIPT_BYTES = 40_000_000
MAX_SEGMENTS = 20_000
MAX_TOTAL_SEGMENTS = 200_000
INDEX_VERSION = 1


class GP7SearchPlugin(Plugin):
    id = "gp7_search"
    name = "GP7 Search"
    description = "Search GP7 recordings and transcripts, with timestamps."
    aliases = ("gp7", "recordings", "transcripts", "gp7_search")
    keywords = (
        "gp7",
        "recording",
        "recordings",
        "transcript",
        "transcripts",
        "in my videos",
    )
    priority = 110

    # ------------------------------------------------------------------ handler

    def handle(self, query: Query, ctx: PluginContext) -> PluginResult:
        root = self._resolve_root()
        if root is None:
            return PluginResult(
                plugin=self.id,
                ok=False,
                title="GP7 folder not configured",
                text=(
                    "Set paths.gp7_root in config.json (or PCASSISTANT_GP7_ROOT) to the "
                    "folder that holds your GP7 recordings and transcripts.\n\n"
                    "Example:\n  \"paths\": { \"gp7_root\": \"D:\\\\Recordings\\\\GP7\" }"
                ),
                error="gp7-root-missing",
                confidence=0.0,
            )
        if not root.is_dir():
            return PluginResult(
                plugin=self.id,
                ok=False,
                title="GP7 folder not found",
                text=f"The configured folder does not exist:\n{root}",
                error="gp7-root-not-found",
                data={"root": str(root)},
                confidence=0.0,
            )

        tokens = _query_tokens(query.text)
        if not tokens:
            return PluginResult(
                plugin=self.id,
                title="GP7 Search",
                text=(
                    "Ask for something from your recordings.\n\n"
                    "Examples:\n  @gp7 cell membrane\n  @gp7 last lecture on recursion"
                ),
                mode="help",
                confidence=0.0,
            )

        index, partial, total = self._load_or_build(root, ctx)
        if not index:
            return PluginResult(
                plugin=self.id,
                ok=True,
                title="No transcripts found",
                text=(
                    f"No .txt, .md, .srt, .vtt, or .json transcripts were found under:\n{root}\n\n"
                    "Add transcripts beside the recordings, or point paths.gp7_root at the right folder."
                ),
                actions=[Action("open", "Open recordings folder", str(root))],
                confidence=0.0,
                data={"root": str(root), "matches": 0, "partial": partial},
            )

        phrase = " ".join(tokens)
        matches: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        name_hits: list[tuple[float, dict[str, Any]]] = []

        for item in index.values():
            ctx.check_deadline()
            name_score = fuzzy_filename_score(phrase, item.get("name", ""))
            if name_score > 0:
                name_hits.append((name_score, item))
            for segment in item.get("segments", []):
                text = segment.get("text", "")
                lowered = text.lower()
                matched = [token for token in tokens if token in lowered]
                if not matched:
                    continue
                coverage = len(set(matched)) / len(tokens)
                score = coverage * 9.0 + len(set(matched)) * 1.2
                if phrase and phrase in lowered:
                    score += 5.0
                if name_score > 0:
                    score += 1.8
                matches.append((score, item, segment))

        matches.sort(key=lambda entry: entry[0], reverse=True)
        selected: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        per_item: dict[str, int] = {}
        for entry in matches:
            path = entry[1]["path"]
            if per_item.get(path, 0) >= 2:
                continue
            per_item[path] = per_item.get(path, 0) + 1
            selected.append(entry)
            if len(selected) >= int(self.settings.get("max_results", 8)):
                break

        if not selected and not name_hits:
            return PluginResult(
                plugin=self.id,
                ok=True,
                title="No matches",
                text=(
                    f"Nothing in {total} transcript(s) matched: {query.text.strip()}\n\n"
                    f"Folder: {root}"
                ),
                actions=[Action("open", "Open recordings folder", str(root))],
                confidence=0.0,
                mode="search",
                data={
                    "root": str(root),
                    "matches": 0,
                    "transcripts": total,
                    "partial": partial,
                },
            )

        segments_out: list[str] = []
        citations: list[Citation] = []
        actions: list[Action] = []
        seen_actions: set[str] = set()
        recordings: set[str] = set()

        for score, item, segment in selected:
            name = item["name"]
            label = segment.get("label") or "start"
            recordings.add(item["recording"] or item["path"])
            line = snippet(segment.get("text", ""), tokens, width=170)
            segments_out.append(f"{name}\n  {label}  {line}")
            citations.append(
                Citation(
                    title=name,
                    source=item.get("transcript", item["path"]),
                    locator=label,
                    snippet=line,
                )
            )
            target = item.get("recording") or item["transcript"]
            if target not in seen_actions:
                seen_actions.add(target)
                actions.append(Action("open", f"Open {Path(target).name}", target))

        if name_hits:
            header_names = " · ".join(
                Path(item.get("recording") or item["path"]).name
                for _, item in sorted(name_hits, key=lambda pair: pair[0], reverse=True)[:3]
            )
            segments_out.insert(0, f"Recordings matching the name: {header_names}\n")
            for _, item in sorted(name_hits, key=lambda pair: pair[0], reverse=True)[:3]:
                target = item.get("recording") or item["transcript"]
                if target not in seen_actions:
                    seen_actions.add(target)
                    actions.append(Action("open", f"Open {Path(target).name}", target))
                    citations.append(
                        Citation(title=item["name"], source=target, locator="filename")
                    )

        actions.append(Action("reveal", "Show recordings folder", str(root)))
        header = f"Found {len(selected)} match{'es' if len(selected) != 1 else ''}"
        if selected:
            header += f" in {len({entry[1]['path'] for entry in selected})} recording(s)"
        elif name_hits:
            header = (
                f"Found {len(name_hits)} recording"
                f"{'s' if len(name_hits) != 1 else ''} matching the name"
            )
        if partial:
            header += " · partial index"

        confidence = min(0.9, 0.42 + 0.07 * len(selected))
        return PluginResult(
            plugin=self.id,
            ok=True,
            title=header,
            text="\n\n".join(segments_out),
            citations=citations,
            actions=actions[:8],
            confidence=confidence,
            mode="search",
            data={
                "root": str(root),
                "matches": len(selected),
                "transcripts": total,
                "partial": partial,
                "query_terms": tokens,
            },
        )

    def health(self, ctx: PluginContext) -> Health:
        root = self._resolve_root()
        if root is None:
            return Health(False, "Set paths.gp7_root to your recordings folder.")
        if not root.is_dir():
            return Health(False, f"Folder not found: {root}")
        index_path = ctx.cache_dir / "gp7_index.json"
        cached = read_json(index_path, {}) or {}
        count = len(cached.get("items", {})) if isinstance(cached, dict) else 0
        return Health(True, f"{root} · {count} indexed transcript(s)")

    # ------------------------------------------------------------------ index

    def _resolve_root(self) -> Path | None:
        configured = self.config.path_value("paths.gp7_root")
        if configured is not None:
            return configured
        for candidate in DEFAULT_ROOTS:
            path = Path(candidate).expanduser()
            if path.is_dir():
                return path
        return None

    def _load_or_build(
        self, root: Path, ctx: PluginContext
    ) -> tuple[dict[str, dict[str, Any]], bool, int]:
        index_path = ctx.cache_dir / "gp7_index.json"
        cached = read_json(index_path, {}) or {}
        items: dict[str, dict[str, Any]] = {}
        if isinstance(cached, dict) and cached.get("version") == INDEX_VERSION:
            stored = cached.get("items")
            if isinstance(stored, dict):
                items = {str(key): value for key, value in stored.items() if isinstance(value, dict)}

        walk = walk_files(
            [root],
            max_depth=6,
            limit=max(2000, int(self.settings.get("max_files", 1500)) * 6),
            deadline=ctx.deadline,
        )
        all_files = walk.files
        transcripts = [
            path for path in all_files
            if path.suffix.lower() in TRANSCRIPT_SUFFIXES and _small_enough(path)
        ]
        recordings = [
            path for path in all_files if path.suffix.lower() in RECORDING_SUFFIXES
        ]
        recording_by_stem: dict[str, Path] = {}
        for path in recordings:
            recording_by_stem.setdefault(path.stem.lower(), path)

        current_paths = {str(path) for path in transcripts}
        for stale in list(items):
            if stale not in current_paths:
                items.pop(stale, None)

        dirty = False
        partial = not walk.complete
        max_files = int(self.settings.get("max_files", 1500))
        if len(transcripts) > max_files:
            partial = True
        total_segments = sum(len(item.get("segments", [])) for item in items.values())

        for path in transcripts[:max_files]:
            if ctx.expired:
                partial = True
                break
            signature = file_signature(path)
            key = str(path)
            existing = items.get(key)
            if existing and existing.get("sig") == signature:
                recording = _match_recording(path, recording_by_stem)
                if recording != existing.get("recording"):
                    existing["recording"] = recording
                    dirty = True
                continue

            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            segments = parse_timed_text(raw)[:MAX_SEGMENTS]
            if not segments:
                items.pop(key, None)
                dirty = True
                continue
            old_segments = len(existing.get("segments", [])) if existing else 0
            if total_segments - old_segments + len(segments) > MAX_TOTAL_SEGMENTS:
                partial = True
                break
            items[key] = {
                "path": key,
                "transcript": key,
                "name": path.stem,
                "sig": signature,
                "mtime": path.stat().st_mtime if path.exists() else 0.0,
                "recording": _match_recording(path, recording_by_stem),
                "segments": segments,
            }
            total_segments = total_segments - old_segments + len(segments)
            dirty = True

        if dirty:
            write_json_atomic(
                index_path,
                {
                    "version": INDEX_VERSION,
                    "built": time.time(),
                    "root": str(root),
                    "partial": partial,
                    "items": items,
                },
            )
        return items, partial, len(transcripts)


def _query_tokens(text: str) -> list[str]:
    from ..text import tokenize

    tokens = [token for token in tokenize(text or "", keep_stopwords=False) if token not in NOISE]
    return list(dict.fromkeys(tokens))


def _small_enough(path: Path) -> bool:
    try:
        return path.stat().st_size <= MAX_TRANSCRIPT_BYTES
    except OSError:
        return False


def _match_recording(path: Path, by_stem: dict[str, Path]) -> str | None:
    exact = by_stem.get(path.stem.lower())
    if exact is not None:
        return str(exact)
    stem = path.stem.lower()
    for candidate_stem, candidate in by_stem.items():
        if candidate_stem.startswith(stem) or stem.startswith(candidate_stem):
            return str(candidate)
    return None
