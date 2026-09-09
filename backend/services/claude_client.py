"""The only module in the project that knows Anthropic exists.

Enrichment is optional: with no credentials the client reports `enabled = False`
and every call is a no-op, so the app works fully without an API key.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

VALID_SENTIMENTS = ("bullish", "bearish", "neutral")

_SYSTEM = (
    "You summarise financial news for a trader's dashboard. "
    "Given a headline and blurb, write one plain sentence (max 20 words) capturing "
    "what happened and why it matters, then classify the likely effect on the "
    "company's share price as exactly one of: bullish, bearish, neutral. "
    "Judge the article only; do not speculate beyond it."
)


@dataclass(frozen=True)
class Enrichment:
    summary: str
    sentiment: str


class _Refusal:
    """Sentinel type for the single instance ``REFUSED`` (below).

    Distinct from ``None`` so callers of ``enrich()`` can tell "Claude
    explicitly refused" (deterministic for the same input -- retrying just
    wastes money, per design doc §8) apart from "some other failure" (worth
    retrying, since it may be transient).
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "REFUSED"


REFUSED = _Refusal()


def _build_default_creator(model: str) -> Callable[..., Any] | None:
    """Construct the real SDK caller, or None when no credentials resolve.

    An unset ANTHROPIC_API_KEY does not by itself mean "no credentials" — the SDK
    also resolves ANTHROPIC_AUTH_TOKEN and `ant auth login` profiles. So we try to
    construct the client and let it tell us, rather than checking an env var.

    Constructing ``anthropic.Anthropic()`` never raises for missing credentials in
    the installed SDK (0.121.0) — it defers that failure to request time, inside
    header validation, raising ``TypeError`` only once a call is actually made.
    So a bare try/except around construction would leave `enabled` incorrectly
    `True` with no credentials, only failing later inside `enrich()`. Instead we
    inspect the same public attributes (`api_key`, `auth_token`, `credentials`)
    the SDK's own request-time validation checks, so `enabled` reflects reality
    immediately and without any network call.

    That attribute-shape assumption is pinned against ``anthropic<1.0``
    (pyproject.toml), but a future release could still rename or drop one of
    those attributes without tripping the pin. If that happens we must not
    fold it into "no credentials": a user with a genuinely working key would
    silently lose AI enrichment with no diagnostic anywhere. So the attribute
    read below is isolated in its own try/except that only catches
    AttributeError and logs it at ERROR level with a distinct message,
    instead of disappearing into the broad except around construction.
    """
    try:
        import anthropic
        from pydantic import BaseModel

        class _Enrichment(BaseModel):
            summary: str
            sentiment: str

        client = anthropic.Anthropic()
    except Exception:
        # Import failure, or the constructor itself raised (e.g. conflicting
        # explicit credential args, or an explicitly-selected `ant auth login`
        # profile — ANTHROPIC_PROFILE / ANTHROPIC_CONFIG_DIR — that failed to
        # parse; per the SDK's own docs an *explicitly* selected profile's
        # errors propagate rather than being swallowed internally). Logged
        # with the traceback so a corrupted explicit profile is diagnosable
        # from the logs rather than looking identical to "no key configured".
        logger.info(
            "Anthropic credentials unavailable; AI enrichment disabled",
            exc_info=True,
        )
        return None

    try:
        has_credentials = bool(client.api_key or client.auth_token or client.credentials)
    except AttributeError:
        logger.error(
            "anthropic.Anthropic no longer exposes the api_key/auth_token/"
            "credentials attributes this client's credential detection relies "
            "on (installed anthropic version: %s). AI enrichment is being "
            "disabled as a precaution, but this may be masking a genuinely "
            "working key -- check the anthropic SDK version pinned in "
            "pyproject.toml.",
            getattr(anthropic, "__version__", "unknown"),
            exc_info=True,
        )
        return None

    if not has_credentials:
        logger.info("Anthropic credentials unavailable; AI enrichment disabled")
        return None

    def creator(**kwargs: Any) -> Any:
        return client.messages.parse(output_format=_Enrichment, **kwargs)

    return creator


class ClaudeClient:
    def __init__(
        self,
        message_creator: Callable[..., Any] | None = None,
        model: str = "claude-opus-5",
        _force_disabled: bool = False,
    ) -> None:
        self._model = model
        if _force_disabled:
            self._creator = None
        else:
            self._creator = message_creator or _build_default_creator(model)

    @property
    def enabled(self) -> bool:
        return self._creator is not None

    def enrich(self, title: str, summary: str) -> Enrichment | _Refusal | None:
        """Summarise and classify one article. Never raises.

        Returns ``REFUSED`` for an explicit Claude refusal (deterministic;
        callers should not retry it), ``None`` for every other failure
        (transient; safe to retry), or an ``Enrichment`` on success.
        """
        if self._creator is None:
            return None

        article = f"Headline: {title}\n\nBlurb: {summary}".strip()
        try:
            response = self._creator(
                model=self._model,
                max_tokens=512,
                system=_SYSTEM,
                messages=[{"role": "user", "content": article}],
            )
            if getattr(response, "stop_reason", None) == "refusal":
                logger.warning("Claude refused to enrich an article; not retrying")
                return REFUSED
            parsed = getattr(response, "parsed_output", None)
            if parsed is None:
                logger.warning("Claude returned no parsed output")
                return None
            sentiment = str(getattr(parsed, "sentiment", "")).strip().lower()
            if sentiment not in VALID_SENTIMENTS:
                sentiment = "neutral"
            return Enrichment(summary=str(parsed.summary).strip(), sentiment=sentiment)
        except Exception:
            logger.warning("Claude enrichment failed", exc_info=True)
            return None
