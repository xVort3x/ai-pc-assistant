"""LLM access with retries, model fallback, caching, and strict redaction.

Only Gemini is implemented today.  The provider seam is intentionally a
single method so an OpenAI-compatible or local model can be added without
touching any plugin.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any

from .config import Config


class LLMError(RuntimeError):
    """The model was reachable but could not answer."""


class LLMUnavailable(LLMError):
    """No key/provider is configured, so callers should use a fallback."""


class _Transient(Exception):
    pass


class _ModelMissing(Exception):
    pass


class _Fatal(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LLMClient:
    def __init__(self, config: Config, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("pcassistant.llm")
        self._cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self._lock = threading.Lock()
        self.last_model = ""
        self._api_key = str(config.get("llm.api_key", "")).strip()
        self._provider = str(config.get("llm.provider", "gemini")).strip().lower()

    # ------------------------------------------------------------------ state

    @property
    def available(self) -> bool:
        if not self.config.bool("llm.enabled", True):
            return False
        if self._provider in {"", "none", "off", "disabled"}:
            return False
        return bool(self._api_key)

    @property
    def provider(self) -> str:
        return self._provider

    def models(self) -> list[str]:
        primary = str(self.config.get("llm.model", "")).strip()
        fallbacks = self.config.get("llm.fallback_models", [])
        if isinstance(fallbacks, str):
            fallbacks = [fallbacks]
        ordered = [primary, *[str(item).strip() for item in fallbacks]]
        return list(dict.fromkeys(item for item in ordered if item))

    # ------------------------------------------------------------------ public

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        use_cache: bool = True,
    ) -> str:
        """Return plain text from the model.

        Raises ``LLMUnavailable`` when there is no key and ``LLMError`` for
        every other failure.  Plugins are expected to catch both and degrade
        gracefully rather than show a stack trace.
        """

        if not self.available:
            raise LLMUnavailable("No AI key is configured.")
        if not (prompt or "").strip():
            raise LLMError("Empty prompt.")

        max_tokens = int(max_tokens or self.config.number("llm.max_output_tokens", 900))
        temperature = float(
            temperature if temperature is not None else self.config.number("llm.temperature", 0.3)
        )
        cache_key = self._cache_key(prompt, system, max_tokens, temperature)
        if use_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached

        if self._provider != "gemini":
            raise LLMError(f"Unsupported LLM provider: {self._provider}")

        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system.strip():
            payload["systemInstruction"] = {"parts": [{"text": system.strip()}]}

        text = self._complete_with_fallback(payload)
        if use_cache:
            self._cache_put(cache_key, text)
        return text

    # ------------------------------------------------------------------ gemini

    def _complete_with_fallback(self, payload: dict[str, Any]) -> str:
        retries = max(0, int(self.config.number("llm.max_retries", 2)))
        last_error = ""
        for model in self.models():
            for attempt in range(retries + 1):
                started = time.perf_counter()
                try:
                    text = self._call_gemini(model, payload)
                    self.last_model = model
                    self.logger.debug(
                        "LLM ok model=%s attempt=%d ms=%d",
                        model,
                        attempt + 1,
                        int((time.perf_counter() - started) * 1000),
                    )
                    return text
                except _ModelMissing:
                    last_error = f"{model}: model unavailable"
                    break
                except _Transient as exc:
                    last_error = self._sanitize(str(exc))
                    if attempt >= retries:
                        break
                    time.sleep(min(4.0, 0.7 * (2**attempt)))
                except _Fatal as exc:
                    message = self._friendly_fatal(exc)
                    self.logger.warning("LLM failed model=%s: %s", model, message)
                    raise LLMError(message) from None
        self.logger.warning("LLM exhausted models: %s", self._sanitize(last_error))
        raise LLMError(
            "The AI service did not answer. Check the key, the model name, and your "
            "connection, then try again."
        )

    def _call_gemini(self, model: str, payload: dict[str, Any]) -> str:
        key = urllib.parse.quote(self._api_key, safe="")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model, safe='')}:generateContent?key={key}"
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        timeout = max(5.0, self.config.number("llm.timeout_seconds", 45))

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                pass
            if exc.code == 404:
                raise _ModelMissing() from None
            if exc.code in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise _Transient(f"HTTP {exc.code} {detail}") from None
            raise _Fatal(f"HTTP {exc.code} {detail}", status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _Transient(f"network: {exc}") from None
        except OSError as exc:
            raise _Transient(f"network: {exc}") from None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise _Transient("invalid JSON from provider") from None

        blocked = (
            parsed.get("promptFeedback", {}).get("blockReason")
            if isinstance(parsed.get("promptFeedback"), dict)
            else None
        )
        if blocked:
            raise _Fatal(f"request blocked by safety filter ({blocked})")

        candidates = parsed.get("candidates") or []
        if not candidates:
            raise _Transient("empty candidate list")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict)).strip()
        if not text:
            finish = candidates[0].get("finishReason", "")
            raise _Transient(f"empty answer (finishReason={finish})")
        return text

    # ------------------------------------------------------------------ helpers

    def _friendly_fatal(self, exc: _Fatal) -> str:
        if exc.status in {400, 401, 403}:
            return (
                "The AI service rejected the request. Check that the Gemini key is valid "
                "and that the configured model is available to it."
            )
        if exc.status == 413:
            return "The text is too long for the AI service. Shorten it and try again."
        detail = self._sanitize(str(exc))
        return f"The AI service refused the request ({detail})."

    def _sanitize(self, message: str) -> str:
        text = message or ""
        if self._api_key:
            text = text.replace(self._api_key, "***")
        # Defensive: never let a query string leak even if the key changed.
        text = re.sub(r"key=[^&\s\"']+", "key=***", text)
        return text[:500]

    def _cache_key(self, prompt: str, system: str, max_tokens: int, temperature: float) -> str:
        material = "\x00".join(
            [self._provider, str(self.models()), system, prompt, str(max_tokens), f"{temperature:.3f}"]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> str | None:
        ttl = self.config.number("llm.cache_ttl_seconds", 86400)
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            stored_at, text = entry
            if time.time() - stored_at > ttl:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return text

    def _cache_put(self, key: str, text: str) -> None:
        with self._lock:
            self._cache[key] = (time.time(), text)
            self._cache.move_to_end(key)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
