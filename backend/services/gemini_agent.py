"""Gemini-backed analyst chat.

An alternative to `chat_agent.ChatAgent` that speaks Google's function-calling
protocol instead of Anthropic's. It exposes the same `.enabled` / `.ask()`
surface, so the route and the frontend need no knowledge of which provider is
in use.

`chat_tools` is reused wholesale - every tool is a pure SQLite reader, so only
the schema *shape* differs between providers (Anthropic calls it `input_schema`,
Gemini calls it `parameters`). The rules the system prompt enforces are
identical: the model reads, it never fetches, and it never originates a price
level.

This is the only module in the project permitted to import `google.genai`.
"""

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.services import chat_tools
from backend.services.chat_agent import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Verified against a live key on 2026-08-26. models.list() advertises models the
# API then refuses to serve (gemini-2.5-flash and gemini-2.0-flash both 404 for
# new keys), so every name here was confirmed to answer a real request.
# Measured 2026-08-26 against a live key: flash-lite answered 12/12 requests in
# 7s, while gemini-flash-latest (3.7-flash) managed 1/12 and 503'd the rest. A
# tool-use answer costs one request per lookup, so a model that fails two calls
# in three cannot hold a conversation regardless of how good its answers are.
DEFAULT_MODEL = "gemini-flash-lite-latest"     # resolves to gemini-3.5-flash-lite

# The free tier's binding limit is requests per DAY, and the quota is
# "GenerateRequestsPerDayPerProjectPerModel" -- per MODEL. gemini-3.5-flash
# allows 20/day, and a single tool-use answer can spend a dozen of them. Since
# each model carries its own separate daily allowance, falling back down this
# chain multiplies the usable budget instead of ending the conversation.
FALLBACK_MODELS = ("gemini-flash-latest", "gemini-3.5-flash")


def to_function_declarations(schemas: list[dict]) -> list[dict]:
    """Translate our Anthropic-shaped tool schemas into Gemini declarations.

    The only difference is the key holding the JSON Schema. Kept as a pure
    function so it can be tested without the SDK present.
    """
    out = []
    for s in schemas:
        out.append(
            {
                "name": s["name"],
                "description": s["description"],
                "parameters": s.get("input_schema") or {"type": "object", "properties": {}},
            }
        )
    return out


def _build_default_caller(model: str, max_tokens: int) -> Callable[..., Any] | None:
    """Construct the real SDK caller, or None when no key resolves.

    The returned callable takes the model as an argument so the agent can move
    down the fallback chain without rebuilding the client.
    """
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        logger.info("No GEMINI_API_KEY; Gemini chat disabled")
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)

        def caller(
            contents: list, declarations: list[dict], model_name: str = model
        ) -> Any:
            return client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=[types.Tool(function_declarations=declarations)],
                    # Gemini spends this budget on reasoning before it writes
                    # anything, so a tight cap does not shorten the answer --
                    # it returns an empty one.
                    max_output_tokens=max_tokens,
                ),
            )

        return caller
    except Exception:
        logger.info("google-genai unavailable; Gemini chat disabled")
        return None


_RATE_LIMIT_MARKERS = ("resource_exhausted", "429", "quota")

# Clamp: a delay far beyond this means the daily cap is gone, not a per-minute
# blip, and no amount of waiting inside one HTTP request will fix that.
_MAX_SINGLE_WAIT = 65.0


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


# A model that is overloaded is as useless to this answer as one whose daily
# allowance is spent, and the remedy is the same: try the next model rather
# than fail. Kept distinct from the retryable per-minute case, which is about
# *our* request rate and would follow us to any model.
_OVERLOAD_MARKERS = ("unavailable", "503", "overloaded")


def _is_overloaded(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _OVERLOAD_MARKERS)


def _is_daily_quota(exc: Exception) -> bool:
    """True when the exhausted quota is a per-DAY one.

    This matters because the API still returns a small `retryDelay` (21s was
    observed against a daily cap), so honouring that delay would sleep the
    request for no reason -- the allowance does not come back until tomorrow.
    A daily cap is a signal to change model, not to wait.
    """
    return "perday" in str(exc).lower().replace("_", "").replace("-", "")


def _retry_delay(exc: Exception) -> float | None:
    """Seconds the API asked us to wait, or None if it did not say.

    Only the number is read out of the error. The body is never logged or
    echoed, because it can carry the API key.
    """
    hit = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(exc))
    if not hit:
        return None
    return min(float(hit.group(1)), _MAX_SINGLE_WAIT)


@dataclass
class _CallState:
    """Mutable per-answer state: how long we may still wait, and on what model."""

    budget: float
    models: list[str]


def _parts_of(response: Any) -> list:
    """Pull content parts out of a response, tolerating an empty candidate list."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return list(getattr(content, "parts", None) or [])


class GeminiAgent:
    def __init__(
        self,
        caller: Callable[..., Any] | None = None,
        model: str = DEFAULT_MODEL,
        max_turns: int = 24,
        max_tokens: int = 8192,
        retry_budget_seconds: float = 90.0,
        fallback_models: tuple[str, ...] = FALLBACK_MODELS,
        sleep: Callable[[float], None] = time.sleep,
        _force_disabled: bool = False,
    ) -> None:
        self._model = model
        self._max_turns = max_turns
        self._retry_budget = retry_budget_seconds
        # Never retry the primary model as a fallback -- its daily allowance is
        # exactly what ran out.
        self._fallbacks = tuple(m for m in fallback_models if m != model)
        self._sleep = sleep
        self._caller = (
            None
            if _force_disabled
            else (caller or _build_default_caller(model, max_tokens))
        )

    @property
    def enabled(self) -> bool:
        return self._caller is not None

    def ask(self, message: str, history: list[dict], db_path: Path) -> dict:
        """Run one conversational turn. Never raises."""
        if self._caller is None:
            return {
                "reply": (
                    "Gemini chat is off. Set GEMINI_API_KEY in .env and restart "
                    "to enable it."
                ),
                "tools_used": [],
            }

        declarations = to_function_declarations(chat_tools.TOOL_SCHEMAS)
        contents: list[Any] = []
        for turn in history:
            role = "model" if turn.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": str(turn.get("content", ""))}]})
        contents.append({"role": "user", "parts": [{"text": message}]})

        tools_used: list[str] = []
        state = _CallState(budget=self._retry_budget, models=self._models())

        try:
            for _ in range(self._max_turns):
                response = self._call_with_retry(contents, declarations, state)
                parts = _parts_of(response)

                calls = [p for p in parts if getattr(p, "function_call", None)]
                if not calls:
                    text = " ".join(
                        p.text for p in parts if getattr(p, "text", None)
                    ).strip()
                    return {"reply": text or "(no answer)", "tools_used": tools_used}

                contents.append({"role": "model", "parts": parts})

                results = []
                for part in calls:
                    call = part.function_call
                    name = call.name
                    args = dict(call.args or {})
                    tools_used.append(name)
                    output = chat_tools.execute(name, args, db_path)
                    results.append(
                        {
                            "function_response": {
                                "name": name,
                                # Gemini wants a dict; json round-trip keeps it
                                # serialisable the same way the Anthropic path does.
                                "response": json.loads(json.dumps(output, default=str)),
                            }
                        }
                    )
                contents.append({"role": "user", "parts": results})

            return {
                "reply": (
                    f"I used all {self._max_turns} lookup steps and still had more "
                    "to check. Ask about fewer tickers at once, or ask for one "
                    "thing at a time."
                ),
                "tools_used": tools_used,
            }
        except Exception as exc:
            # Type only - a provider error body can carry the API key.
            logger.warning("Gemini chat turn failed (%s)", type(exc).__name__)
            return {"reply": _explain(exc), "tools_used": tools_used}

    def _models(self) -> list[str]:
        return [self._model, *self._fallbacks]

    def _call_with_retry(
        self, contents: list, declarations: list[dict], state: "_CallState"
    ) -> Any:
        """One provider call, surviving both kinds of quota exhaustion.

        A per-minute limit is waited out. A per-day limit is not -- the
        allowance is gone until tomorrow, and it is charged per model, so the
        answer continues on the next model in the chain instead of ending.

        `state` carries the wait budget and the current model across the whole
        answer, so a question needing fifteen lookups cannot spend the full
        wait fifteen times over, and does not re-try an already-exhausted
        model on every subsequent round.
        """
        while True:
            try:
                return self._caller(contents, declarations, state.models[0])
            except Exception as exc:
                overloaded = _is_overloaded(exc)
                if not _is_rate_limit(exc) and not overloaded:
                    raise

                if overloaded or _is_daily_quota(exc):
                    if len(state.models) == 1:
                        raise
                    dropped = state.models.pop(0)
                    logger.info(
                        "Gemini model %s unusable (%s); falling back to %s",
                        dropped,
                        "overloaded" if overloaded else "daily quota spent",
                        state.models[0],
                    )
                    continue

                delay = _retry_delay(exc)
                if delay is None or delay > state.budget:
                    raise
                logger.info("Gemini rate-limited; waiting %.0fs", delay)
                self._sleep(delay)
                state.budget -= delay


# Failures a user can act on, answered with our OWN strings. The provider
# message is never echoed back, because an error body can embed the key.
_ACTIONABLE = (
    ("api key not valid",
     "Google rejected the API key. Check GEMINI_API_KEY in .env and restart."),
    ("api_key_invalid",
     "Google rejected the API key. Check GEMINI_API_KEY in .env and restart."),
    ("permission_denied",
     "This key lacks access to the Gemini API. Enable it in Google AI Studio."),
    ("perday",
     "Gemini's free daily request allowance is used up for every model this key "
     "can reach. It resets at midnight Pacific. Until then, set "
     "chat_provider=\"anthropic\" in backend/config.py to use your Anthropic key."),
    ("quota",
     "Gemini quota exhausted for now. The free tier resets - try again shortly."),
    ("resource_exhausted",
     "Gemini is rate-limiting this key. Wait a moment and try again."),
    ("no longer available",
     "That Gemini model has been retired for new keys. Update gemini_model in "
     "backend/config.py."),
    ("not_found",
     "That Gemini model is not available to this key. Update gemini_model in "
     "backend/config.py."),
    ("unavailable",
     "Gemini is temporarily overloaded for this model. Try again shortly."),
    ("503",
     "Gemini is temporarily overloaded for this model. Try again shortly."),
)


def _explain(exc: Exception) -> str:
    raw = str(exc)
    text = raw.lower()
    for needle, message in _ACTIONABLE:
        if needle in text:
            hint = _suggested_model(raw)
            return f"{message} {hint}" if hint else message
    return f"The Gemini call failed ({type(exc).__name__}). Try again."


def _suggested_model(raw: str) -> str:
    """Extract a `models/...` name the provider suggested, if any.

    Only the model fragment is echoed. A model name cannot contain an API key,
    whereas the full error body can - so this stays a narrow allowlist rather
    than passing the provider message through.
    """
    import re

    hits = re.findall(r"models/([a-z0-9.\-]+)", raw, flags=re.I)
    # The message names the retired model FIRST and the replacement second, so
    # take the last match rather than the first.
    candidates = [h for h in hits if "flash" in h or "pro" in h]
    suggestion = candidates[-1] if candidates else None
    return f"The API suggests {suggestion}." if suggestion else ""
