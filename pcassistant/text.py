"""Text utilities: tokenising, snippet extraction, and safe cleanup.

These functions are intentionally dependency-free.  Retrieval quality lives
in ``search.py``; this module only handles the string mechanics.
"""

from __future__ import annotations

import math
import re
import time
from typing import Iterable, Sequence

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+#.\-]*", re.UNICODE)
_SENTENCE_START_RE = re.compile(r"(^|[.!?][ \t]+)([a-z])", re.MULTILINE)
SPACE = re.compile(r"[ \t]+")
BLANK_LINES = re.compile(r"\n{3,}")

STOPWORDS = frozenset(
    """
    a an and are as at be been being but by can could did do does doing for
    from had has have having he her here him his how i if in into is it its
    just me more most my no not of on or our out over she should so some
    than that the their them then there these they this those to too up us
    was we were what when where which who why will with would you your
    """.split()
)


def tokenize(text: str, *, keep_stopwords: bool = True) -> tuple[str, ...]:
    tokens = [match.group(0).lower() for match in _TOKEN_RE.finditer(text or "")]
    if not keep_stopwords:
        meaningful = [token for token in tokens if token not in STOPWORDS]
        return tuple(meaningful or tokens)
    return tuple(tokens)


def normalize_ws(text: str) -> str:
    return SPACE.sub(" ", (text or "").replace("\r\n", "\n")).strip()


def truncate(text: str, limit: int, *, suffix: str = "…") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def snippet(text: str, terms: Sequence[str], *, width: int = 180) -> str:
    """Return a readable window around the first matching term."""

    flat = normalize_ws(text)
    if not flat:
        return ""
    flat = " ".join(flat.split())
    lowered = flat.lower()
    positions = [lowered.find(term.lower()) for term in terms if term]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return truncate(flat, width)
    centre = min(positions)
    start = max(0, centre - width // 3)
    end = min(len(flat), start + width)
    start = max(0, end - width)
    piece = flat[start:end].strip()
    if start > 0:
        piece = "…" + piece
    if end < len(flat):
        piece = piece + "…"
    return piece


def human_size(size: int | float) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def relative_time(timestamp: float, *, now: float | None = None) -> str:
    if not timestamp:
        return "unknown date"
    delta = max(0.0, (now or time.time()) - timestamp)
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} d ago"
    if delta < 86400 * 365:
        return f"{int(delta // (86400 * 30))} mo ago"
    return f"{int(delta // (86400 * 365))} y ago"


_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$", re.IGNORECASE)


def parse_duration(value: str, *, default_seconds: float = 0.0) -> float:
    """Parse ``30m``, ``7d``, ``2w`` into seconds."""

    match = _DURATION_RE.match((value or "").strip())
    if not match:
        return default_seconds
    amount = float(match.group(1))
    unit = match.group(2).lower()
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return amount * factor


def fmt_timestamp(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def mechanical_cleanup(text: str) -> tuple[str, list[str]]:
    """Deterministic, conservative cleanup used when no LLM is available.

    This is explicitly *not* a grammar model.  It fixes mechanical problems
    and labels itself as a fallback so the user is never misled.
    """

    original = text or ""
    changes: list[str] = []

    cleaned = original.replace("\r\n", "\n").replace("\r", "\n")
    if cleaned != original:
        changes.append("normalized line endings")

    without_tabs = cleaned.replace("\t", " ")
    if without_tabs != cleaned:
        changes.append("replaced tabs with spaces")
    cleaned = without_tabs

    collapsed = SPACE.sub(" ", cleaned)
    if collapsed != cleaned:
        changes.append("collapsed repeated spaces")
    cleaned = collapsed

    trimmed_lines = "\n".join(line.strip() for line in cleaned.split("\n"))
    if trimmed_lines != cleaned:
        changes.append("trimmed line edges")
    cleaned = trimmed_lines

    compact = BLANK_LINES.sub("\n\n", cleaned)
    if compact != cleaned:
        changes.append("collapsed blank lines")
    cleaned = compact

    no_space_before = re.sub(r"[ \t]+([,.;:!?])", r"\1", cleaned)
    if no_space_before != cleaned:
        changes.append("removed spaces before punctuation")
    cleaned = no_space_before

    spaced_after = re.sub(r"([,;:])(?=[A-Za-z])", r"\1 ", cleaned)
    if spaced_after != cleaned:
        changes.append("added spaces after punctuation")
    cleaned = spaced_after

    fixed_i = re.sub(r"\b(i)\b", "I", cleaned)
    if fixed_i != cleaned:
        changes.append("capitalized the pronoun I")
    cleaned = fixed_i

    capitalized = _SENTENCE_START_RE.sub(
        lambda match: match.group(1) + match.group(2).upper(), cleaned
    )
    if capitalized != cleaned:
        changes.append("capitalized sentence starts")
    cleaned = capitalized

    stripped = cleaned.strip()
    if stripped != cleaned:
        changes.append("trimmed outer whitespace")
    cleaned = stripped

    if cleaned and cleaned[-1].isalnum():
        cleaned += "."
        changes.append("added terminal punctuation")

    return cleaned, changes


def cosineish_overlap(query_tokens: Iterable[str], text_tokens: Iterable[str]) -> float:
    """Cheap Jaccard-like overlap used for routing tie-breaks."""

    left = set(query_tokens)
    right = set(text_tokens)
    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))
