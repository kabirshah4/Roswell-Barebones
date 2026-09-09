"""Gemini-backed news enrichment.

A drop-in alternative to `claude_client.ClaudeClient`: same `.enabled` flag,
same `enrich(title, summary)` contract, same `Enrichment` / `REFUSED` / `None`
return triple. The poller therefore does not know or care which provider is
wired in.

Gemini's free tier is the reason this exists — enrichment runs on every new
article across the whole watchlist, so it is the single largest source of AI
spend in the app.

Imports of `google.genai` live here and in `gemini_agent.py` only.
"""

import json
import logging
import os
from collections.abc import Callable
from typing import Any

from backend.services.claude_client import REFUSED, VALID_SENTIMENTS, Enrichment, _Refusal
from backend.services.gemini_agent import DEFAULT_MODEL

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You summarise financial news for a trader's dashboard. "
    "Given a headline and blurb, write one plain sentence (max 20 words) capturing "
    "what happened and why it matters, then classify the likely effect on the "
    "company's share price as exactly one of: bullish, bearish, neutral. "
    "Judge the article only; do not speculate beyond it. "
    'Reply with JSON only: {"summary": "...", "sentiment": "..."}'
)

_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "sentiment": {"type": "string"}},
    "required": ["summary", "sentiment"],
}

# Gemini reports a content-policy stop the way Claude reports a refusal: a 200
# with no usable text. These are deterministic for the same input, so they map
# to REFUSED (no retry) rather than None (retry).
_REFUSAL_REASONS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"})


def _build_default_caller(model: str) -> Callable[..., Any] | None:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        logger.info("No GEMINI_API_KEY; Gemini enrichment disabled")
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)

        def caller(article: str) -> Any:
            return client.models.generate_content(
                model=model,
                contents=article,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM,
                    response_mime_type="application/json",
                    response_schema=_SCHEMA,
                    max_output_tokens=512,
                ),
            )

        return caller
    except Exception:
        logger.info("google-genai unavailable; Gemini enrichment disabled")
        return None


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    reason = getattr(candidates[0], "finish_reason", None)
    # The SDK returns an enum; its name is the stable part.
    return str(getattr(reason, "name", reason) or "").upper()


class GeminiEnricher:
    def __init__(
        self,
        caller: Callable[..., Any] | None = None,
        model: str = DEFAULT_MODEL,
        _force_disabled: bool = False,
    ) -> None:
        self._model = model
        self._caller = (
            None if _force_disabled else (caller or _build_default_caller(model))
        )

    @property
    def enabled(self) -> bool:
        return self._caller is not None

    def enrich(self, title: str, summary: str) -> Enrichment | _Refusal | None:
        """Summarise and classify one article. Never raises.

        Mirrors ClaudeClient.enrich exactly: REFUSED means "do not retry",
        None means "transient, retry later".
        """
        if self._caller is None:
            return None

        article = f"Headline: {title}\n\nBlurb: {summary}".strip()
        try:
            response = self._caller(article)

            if _finish_reason(response) in _REFUSAL_REASONS:
                logger.warning("Gemini blocked an article; not retrying")
                return REFUSED

            text = (getattr(response, "text", None) or "").strip()
            if not text:
                logger.warning("Gemini returned no text for an article")
                return None

            data = json.loads(text)
            article_summary = str(data.get("summary", "")).strip()
            if not article_summary:
                return None
            sentiment = str(data.get("sentiment", "")).strip().lower()
            if sentiment not in VALID_SENTIMENTS:
                sentiment = "neutral"
            return Enrichment(summary=article_summary, sentiment=sentiment)
        except Exception as exc:
            # Type only — a provider error body can carry the API key.
            logger.warning("Gemini enrichment failed (%s)", type(exc).__name__)
            return None
