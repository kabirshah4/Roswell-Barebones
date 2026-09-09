"""Tool-use loop over the terminal's own cached data.

The model reads; it does not fetch and it does not compute price levels. Entry,
stop and target values come from the `signals` table, where the signal engine
put them after deriving them from ATR and swing structure.

The system prompt is deliberately blunt about the backtest result: on the sample
measured so far the A+ grade did not outperform B, so a reply citing a letter
without the measured hit rate would overstate what is known.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.services import chat_tools

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the analyst inside a personal market-research terminal.

You answer using the tools provided. They read the user's own cached data:
prices, news, fundamentals, earnings, screener results, computed setups, and
measured backtest statistics.

Hard rules:

1. NEVER invent or originate a price level. Entry, stop and target values come
   from get_signals, where they were computed from ATR and swing structure. If
   the user asks for levels on a ticker with no setup, say there is no setup
   rather than making numbers up.

1b. You CAN and SHOULD answer forward-looking questions - "where is this
   going", "could it hit 250", "what's it worth in a month" - using
   get_price_outlook. It returns a projection computed from measured
   volatility and damped trend: a central estimate plus 68% and 95% ranges
   over several horizons, and probability_above for a specific target.

   Use it freely and reason out loud about what it implies. Two conditions:
   quote the RANGE, not just the central number, because the range is the part
   the evidence supports; and never replace its numbers with your own guess.
   If the tool has no data for a ticker, say so - do not estimate from
   memory, because your training data has no idea what this stock did last
   week.

   Be direct about what the projection is: a statistical range implied by how
   the stock has actually been moving, not a prediction. It assumes the future
   resembles the recent past, which is exactly what earnings, news and macro
   shocks break. Combine it with the news and earnings tools rather than
   quoting it alone.

2. Whenever you mention a setup grade (A+, A, B), call get_backtest_stats for
   that ticker and report the measured win rate and average R alongside it. On
   the sample measured so far the A+ grade did NOT reliably outperform B, so a
   bare letter overstates what is known. Say so when it is relevant.

3. Report coverage. The screener universe warms slowly; if run_screener returns
   a low coverage figure, state it so partial results are not read as a full
   market scan.

4. Flag earnings proximity. A setup near an earnings date carries gap risk that
   the stop does not account for.

5. Be concrete and brief. Cite the numbers you retrieved. If a tool returns an
   error or no data, say that plainly instead of guessing.

6. Give the user your actual read. They asked for judgement, so weigh the
   evidence and commit to a view - bullish, bearish, or genuinely unclear -
   and say what would change your mind. Hedging everything into mush is not
   caution, it is a non-answer. Ground the view in retrieved numbers.

You are a research assistant, not a broker.
What you say is not financial advice, and you place no orders."""


def _build_default_creator(model: str) -> Callable[..., Any] | None:
    """Construct the real SDK caller, or None when no credentials resolve.

    Constructing anthropic.Anthropic() does not raise without credentials - it
    defers the failure to request time - so the resolved attributes are checked
    directly, mirroring the SDK's own validation.
    """
    try:
        import anthropic

        client = anthropic.Anthropic()
        try:
            has_creds = bool(
                client.api_key or client.auth_token or client.credentials
            )
        except AttributeError:
            logger.error(
                "anthropic client is missing an expected credential attribute; "
                "chat disabled. This usually means an SDK upgrade changed the API."
            )
            return None
        if not has_creds:
            logger.info("Anthropic credentials unavailable; chat disabled")
            return None

        def creator(**kwargs: Any) -> Any:
            return client.messages.create(**kwargs)

        return creator
    except Exception:
        logger.info("Anthropic client unavailable; chat disabled")
        return None


class ChatAgent:
    def __init__(
        self,
        message_creator: Callable[..., Any] | None = None,
        model: str = "claude-opus-5",
        max_turns: int = 24,
        max_tokens: int = 8192,
        _force_disabled: bool = False,
    ) -> None:
        self._model = model
        self._max_turns = max_turns
        self._max_tokens = max_tokens
        self._creator = (
            None
            if _force_disabled
            else (message_creator or _build_default_creator(model))
        )

    @property
    def enabled(self) -> bool:
        return self._creator is not None

    def ask(self, message: str, history: list[dict], db_path: Path) -> dict:
        """Run one conversational turn. Never raises."""
        if self._creator is None:
            return {
                "reply": (
                    "Chat is off. Set ANTHROPIC_API_KEY in .env (or run "
                    "`ant auth login`) and restart to enable it."
                ),
                "tools_used": [],
            }

        messages: list[dict] = list(history) + [{"role": "user", "content": message}]
        tools_used: list[str] = []

        try:
            for _ in range(self._max_turns):
                response = self._creator(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=SYSTEM_PROMPT,
                    tools=chat_tools.TOOL_SCHEMAS,
                    messages=messages,
                )

                blocks = list(getattr(response, "content", []) or [])
                calls = [b for b in blocks if getattr(b, "type", None) == "tool_use"]

                if not calls:
                    text = " ".join(
                        b.text for b in blocks if getattr(b, "type", None) == "text"
                    ).strip()
                    return {"reply": text or "(no answer)", "tools_used": tools_used}

                messages.append({"role": "assistant", "content": blocks})
                results = []
                for call in calls:
                    tools_used.append(call.name)
                    output = chat_tools.execute(call.name, call.input or {}, db_path)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": json.dumps(output, default=str),
                        }
                    )
                messages.append({"role": "user", "content": results})

            return {
                "reply": "I ran out of steps working that out. Try a narrower question.",
                "tools_used": tools_used,
            }
        except Exception as exc:
            # Log the exception TYPE only. Provider error text can embed the API key.
            logger.warning("Chat turn failed (%s)", type(exc).__name__)
            return {"reply": _explain(exc), "tools_used": tools_used}


# Provider failures a user can actually act on. Matched on a lowercased copy of
# the message and answered with our OWN fixed strings - the provider text is
# never echoed back, because an error body can carry the API key.
_ACTIONABLE = (
    ("credit balance is too low", 
     "Anthropic rejected the request: your account is out of credit. "
     "Add credits at console.anthropic.com under Plans & Billing, then ask again."),
    ("invalid x-api-key",
     "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env and restart."),
    ("authentication",
     "Anthropic could not authenticate. Check ANTHROPIC_API_KEY in .env and restart."),
    ("rate limit",
     "Anthropic is rate-limiting this key. Wait a moment and try again."),
    ("overloaded",
     "Anthropic is overloaded right now. Try again shortly."),
    ("not_found_error",
     "That model is not available to this account. Check ai_model in backend/config.py."),
)


def _explain(exc: Exception) -> str:
    """Turn a provider failure into something the user can act on."""
    text = str(exc).lower()
    for needle, message in _ACTIONABLE:
        if needle in text:
            return message
    return f"The model call failed ({type(exc).__name__}). Try again."
