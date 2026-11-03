"""Text extraction adapters.

The host must work on a stock Windows Python install, so every heavy format
has a stdlib path plus an optional better path:

* PDF  -> ``pypdf`` or ``PyPDF2`` if installed, otherwise a clear note.
* DOCX -> read ``word/document.xml`` from the zip (no python-docx needed).
* PPTX -> read slide XML from the zip.
* XLSX -> read shared strings from the zip (approximate, documented).
* HTML -> stdlib parser, scripts and styles discarded.

Subtitle parsing (SRT, VTT, Whisper JSON, timestamped text) lives here too
because both GP7 search and downloads summaries benefit from it.
"""

from __future__ import annotations

import html
import json
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".sql", ".sh", ".ps1", ".bat", ".cmd", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".toml",
}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
SUBTITLE_SUFFIXES = {".srt", ".vtt"}
CODE_SUFFIXES = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".sql", ".sh", ".ps1", ".bat", ".cmd", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
}


@dataclass(slots=True)
class Extracted:
    text: str
    kind: str
    note: str = ""
    truncated: bool = False


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _strip_xml(xml: str) -> str:
    text = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<w:br[^>]*/>", "\n", text)
    text = re.sub(r"</w:p>", "\n", text)
    text = re.sub(r"</a:p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        chunks: list[str] = []
        for member in ("word/document.xml",):
            if member in names:
                chunks.append(_strip_xml(archive.read(member).decode("utf-8", "replace")))
        for member in sorted(n for n in names if re.match(r"word/(header|footer)\d+\.xml$", n)):
            chunks.append(_strip_xml(archive.read(member).decode("utf-8", "replace")))
        return "\n\n".join(chunk for chunk in chunks if chunk)


def _pptx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        slides = [
            name for name in archive.namelist()
            if re.match(r"ppt/slides/slide\d+\.xml$", name)
        ]
        slides.sort(key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)))  # type: ignore[union-attr]
        chunks = []
        for name in slides:
            text = _strip_xml(archive.read(name).decode("utf-8", "replace"))
            if text:
                chunks.append(text)
        return "\n\n".join(chunks)


def _xlsx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        if "xl/sharedStrings.xml" not in archive.namelist():
            return ""
        xml = archive.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        values = re.findall(r"<t[^>]*>(.*?)</t>", xml, flags=re.DOTALL)
        return "\n".join(html.unescape(value).strip() for value in values if value.strip())


def _pdf_text(path: Path, max_chars: int) -> tuple[str, str]:
    reader_class: Any = None
    try:
        from pypdf import PdfReader  # type: ignore

        reader_class = PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore

            reader_class = PdfReader
        except ImportError:
            return "", "PDF text extraction needs the optional 'pypdf' package."
    try:
        reader = reader_class(str(path))
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            if text:
                parts.append(text)
                total += len(text)
            if total >= max_chars:
                break
        return "\n\n".join(parts), ""
    except Exception as exc:  # malformed/encrypted PDFs are common
        return "", f"Could not read PDF ({type(exc).__name__})."


def extract_text(path: Path, *, max_chars: int = 12000) -> Extracted:
    """Return plain text for a supported file, never raising on bad input."""

    suffix = path.suffix.lower()
    try:
        if suffix in HTML_SUFFIXES:
            raw = path.read_text(encoding="utf-8", errors="replace")
            text = html_to_text(raw)
            kind = "web page"
        elif suffix in TEXT_SUFFIXES:
            raw = path.read_text(encoding="utf-8", errors="replace")
            text = raw
            kind = "code" if suffix in CODE_SUFFIXES else "text"
        elif suffix in SUBTITLE_SUFFIXES:
            raw = path.read_text(encoding="utf-8", errors="replace")
            segments = parse_timed_text(raw)
            text = "\n".join(
                f"[{segment['label']}] {segment['text']}" if segment["label"] else segment["text"]
                for segment in segments
            )
            kind = "transcript"
        elif suffix == ".pdf":
            text, note = _pdf_text(path, max_chars)
            kind = "PDF"
            if note:
                return Extracted("", kind, note)
        elif suffix == ".docx":
            text = _docx_text(path)
            kind = "Word document"
        elif suffix == ".pptx":
            text = _pptx_text(path)
            kind = "presentation"
        elif suffix == ".xlsx":
            text = _xlsx_text(path)
            kind = "spreadsheet"
            if not text:
                return Extracted("", kind, "Spreadsheet has no shared text cells.")
        else:
            return Extracted("", suffix.lstrip(".") or "file", "Unsupported file type.")
    except (OSError, zipfile.BadZipFile, UnicodeError) as exc:
        return Extracted("", suffix.lstrip(".") or "file", f"Could not read file ({type(exc).__name__}).")

    truncated = len(text) > max_chars
    return Extracted(text[:max_chars], kind, "", truncated)


# --------------------------------------------------------------------- timing

_TIMESTAMP_RE = re.compile(
    r"^\s*\[?(?P<stamp>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\]?"
    r"\s*(?:-->.*?)?\s*[-–—:]?\s*(?P<text>.*)$"
)
_CUE_TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]+\}")


def parse_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    if ":" not in text:
        try:
            return float(text)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def _segment(start: float | None, text: str) -> dict[str, Any] | None:
    cleaned = html.unescape(_CUE_TAG_RE.sub("", text or "")).strip()
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    if not cleaned:
        return None
    label = ""
    if start is not None:
        total = int(start)
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        label = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    return {"start": start, "label": label, "text": cleaned}


def _json_segments(raw: str) -> list[dict[str, Any]] | None:
    stripped = raw.lstrip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    items: Any
    if isinstance(parsed, dict):
        items = (
            parsed.get("segments")
            or parsed.get("items")
            or parsed.get("lines")
            or parsed.get("transcript")
            or parsed.get("results")
        )
    else:
        items = parsed
    if not isinstance(items, list):
        return None

    segments: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            segment = _segment(None, item)
        elif isinstance(item, dict):
            text = (
                item.get("text")
                or item.get("content")
                or item.get("sentence")
                or item.get("transcript")
                or ""
            )
            start = parse_timestamp(
                item.get("start", item.get("startTime", item.get("timestamp", item.get("time"))))
            )
            segment = _segment(start, str(text))
        else:
            segment = None
        if segment:
            segments.append(segment)
    return segments or None


def parse_timed_text(raw: str) -> list[dict[str, Any]]:
    """Parse SRT/VTT/Whisper JSON/timestamped text into labelled segments.

    Each segment is ``{"start": float|None, "label": "m:ss", "text": str}``.
    Plain text degrades to one segment per non-empty line.
    """

    if not raw or not raw.strip():
        return []

    json_segments = _json_segments(raw)
    if json_segments:
        return _dedupe(json_segments)

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text)
    segments: list[dict[str, Any]] = []

    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        start: float | None = None
        body: list[str] = []
        for line in lines:
            if "-->" in line:
                start = parse_timestamp(line.split("-->", 1)[0])
                continue
            match = _TIMESTAMP_RE.match(line)
            if match and match.group("stamp") and not body:
                start = parse_timestamp(match.group("stamp"))
                remainder = match.group("text").strip()
                if remainder:
                    body.append(remainder)
                continue
            if re.match(r"^\d+$", line.strip()) and not body:
                continue  # SRT cue number
            if line.strip().upper() in {"WEBVTT", "STYLE", "NOTE"} and not body:
                continue  # VTT metadata
            body.append(line)
        segment = _segment(start, " ".join(body))
        if segment:
            segments.append(segment)

    if not segments:
        for line in text.split("\n"):
            segment = _segment(None, line)
            if segment:
                segments.append(segment)

    return _dedupe(segments)


def _dedupe(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    previous = None
    for segment in segments:
        key = (segment["text"], segment["label"])
        if key == previous:
            continue
        result.append(segment)
        previous = key
    return result
