"""Grammar and proofreading plugin.

The LLM is an enhancement, not a dependency.  When it is unavailable the
plugin performs conservative mechanical cleanup and says so.  It never
pretends a heuristic is a grammar model.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from ..llm import LLMError, LLMUnavailable
from ..models import Action, PluginResult, Query
from ..plugin import Plugin, PluginContext
from ..text import mechanical_cleanup, truncate

TONE_INSTRUCTIONS = {
    "neutral": "Keep the author's voice; fix errors only.",
    "formal": "Use a professional, formal register without changing meaning.",
    "concise": "Make sentences tighter while preserving every fact and nuance.",
    "simple": "Use plain, simple wording while preserving meaning.",
}

_SYSTEM = """You are the copy-editing engine inside a desktop assistant.
Rewrite the user's text so it is grammatically correct, clearly punctuated, and natural.
Rules:
- Preserve the author's meaning, voice, formatting, line breaks, names, numbers, and technical terms.
- Fix grammar, spelling, punctuation, capitalisation, and obvious word-choice errors only.
- Do not add new information, opinions, examples, or commentary.
- Keep the same language as the input.
- Tone: {tone}
- Return ONLY the corrected text. No preamble, no quotation marks, no markdown fences.
- If nothing needs changing, return the text unchanged."""

_START_TRIGGERS = (
    r"^\s*(?:please\s+)?(?:can you\s+)?(?:fix|correct|proofread|check|improve)\s+"
    r"(?:my|this|the)\s+(?:grammar|spelling|punctuation|english|writing|text|sentence)\s*[:\-–]?\s*",
    r"^\s*(?:fix|correct|proofread|check)\s+(?:the\s+)?(?:grammar|spelling|punctuation)\s*[:\-–]?\s*",
    r"^\s*(?:grammar|grammar_fixer|proofread|spelling|punctuation)\s*[:\-–]\s*",
    r"^\s*(?:proofread|check|fix|correct)\s+(?:this|the following|the text)\s*[:\-–]?\s*",
    r"^\s*(?:fix|correct|proofread)\s*[:\-–]\s*",
)


class GrammarFixerPlugin(Plugin):
    id = "grammar_fixer"
    name = "Grammar Fixer"
    description = "Correct grammar, spelling, punctuation, and tone in a passage."
    aliases = ("grammar", "proofread", "grammar_fixer", "fix")
    keywords = (
        "grammar",
        "proofread",
        "spelling",
        "punctuation",
        "grammar check",
        "fix this text",
        "correct this text",
    )
    priority = 130
    requires_network = True

    # ------------------------------------------------------------------ routing

    def can_handle(self, query: Query, ctx: PluginContext) -> float:
        text = (query.text or "").strip()
        if not text:
            return 0.0
        lowered = text.lower()

        if re.search(
            r"\b(proofread|grammar check|fix (?:my |the )?grammar|"
            r"correct (?:my |the )?(?:grammar|spelling|punctuation|text|writing)|"
            r"check (?:my |the )?(?:grammar|spelling|punctuation))\b",
            lowered,
        ):
            return 0.94
        if re.match(r"^\s*(?:fix|correct|proofread)\s*[:\-–]", lowered):
            return 0.93
        if re.match(r"^(what|why|how|who|when|where|is|are|can|do|does|should)\b", lowered):
            return 0.05
        if _contains_word(lowered, "grammar") or _contains_word(lowered, "proofread"):
            return 0.85
        if len(text.split()) >= 8 and re.match(r"^\s*(fix|correct|proofread)\b", lowered):
            return 0.8
        return 0.0

    # ------------------------------------------------------------------ handler

    def handle(self, query: Query, ctx: PluginContext) -> PluginResult:
        text = _extract_text(query.text)
        if not text:
            return PluginResult(
                plugin=self.id,
                title="Grammar Fixer",
                text=(
                    "Send the passage to correct.\n\n"
                    "Example:\n  @grammar: i has went to the store yesterday\n"
                    "  @grammar --tone=formal: hey can u send me that file"
                ),
                mode="help",
                confidence=0.0,
            )

        max_chars = int(self.settings.get("max_chars", 8000))
        if len(text) > max_chars:
            return PluginResult(
                plugin=self.id,
                ok=False,
                title="Text is too long",
                text=(
                    f"That passage is {len(text):,} characters; the limit is {max_chars:,}. "
                    "Split it into smaller sections and run them separately."
                ),
                error="input-too-long",
                confidence=0.0,
            )

        tone = query.option("tone", str(self.settings.get("tone", "neutral"))).lower()
        if tone not in TONE_INSTRUCTIONS:
            tone = "neutral"

        allow_network = bool(self.settings.get("allow_network", True))
        notice = ""
        model = ""

        if allow_network and ctx.llm.available:
            try:
                corrected = ctx.llm.complete(
                    text,
                    system=_SYSTEM.format(tone=TONE_INSTRUCTIONS[tone]),
                    max_tokens=max(400, min(2000, len(text) // 2 + 300)),
                    temperature=0.15,
                )
                model = ctx.llm.last_model
                corrected = _clean_model_output(corrected)
                if not corrected:
                    raise LLMError("The model returned an empty correction.")
                mode = "ai"
                confidence = 0.92
            except (LLMUnavailable, LLMError) as exc:
                corrected, changes = mechanical_cleanup(text)
                mode = "mechanical-fallback"
                confidence = 0.38
                notice = f"AI was unavailable; mechanical cleanup used instead. {exc}"
                return self._result(text, corrected, changes, tone, mode, confidence, notice, "")
        else:
            corrected, changes = mechanical_cleanup(text)
            mode = "offline" if not allow_network else "mechanical-fallback"
            confidence = 0.38
            notice = (
                "Network access is disabled for this plugin; mechanical cleanup used instead."
                if not allow_network
                else "No AI key configured; mechanical cleanup used instead."
            )
            return self._result(text, corrected, changes, tone, mode, confidence, notice, "")

        changes = _word_changes(text, corrected)
        return self._result(text, corrected, changes, tone, mode, confidence, notice, model)

    def _result(
        self,
        original: str,
        corrected: str,
        changes: list[dict[str, Any]],
        tone: str,
        mode: str,
        confidence: float,
        notice: str,
        model: str,
    ) -> PluginResult:
        changed = corrected.strip() != original.strip()
        if not changed:
            title = "No changes needed"
        elif mode == "ai":
            title = f"Corrected · {len(changes)} change{'s' if len(changes) != 1 else ''}"
        else:
            title = "Mechanical cleanup (AI unavailable)"

        actions = []
        if changed:
            actions.append(Action("copy", "Copy corrected text", corrected))

        return PluginResult(
            plugin=self.id,
            ok=True,
            title=title,
            text=corrected,
            actions=actions,
            confidence=confidence if changed else max(confidence, 0.8),
            mode=mode,
            data={
                "original": original,
                "corrected": corrected,
                "changes": changes[:20],
                "change_count": len(changes),
                "tone": tone,
                "model": model,
                "notice": notice,
            },
        )

    def health(self, ctx: PluginContext) -> "Health":
        from ..models import Health

        if not self.settings.get("allow_network", True):
            return Health(True, "Offline mode: mechanical cleanup only.")
        if ctx.llm.available:
            return Health(True, f"AI ready ({ctx.llm.provider}).")
        return Health(True, "No AI key; mechanical cleanup fallback will be used.")


def _extract_text(text: str) -> str:
    value = (text or "").strip()
    for pattern in _START_TRIGGERS:
        replaced = re.sub(pattern, "", value, count=1, flags=re.IGNORECASE)
        if replaced != value:
            value = replaced
            break
    return value.strip()


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def _clean_model_output(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```") and value.endswith("```"):
        value = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _word_changes(original: str, corrected: str) -> list[dict[str, Any]]:
    before = re.findall(r"\S+", original)
    after = re.findall(r"\S+", corrected)
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    changes: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append(
            {
                "type": tag,
                "before": truncate(" ".join(before[i1:i2]), 160),
                "after": truncate(" ".join(after[j1:j2]), 160),
            }
        )
        if len(changes) >= 20:
            break
    return changes
