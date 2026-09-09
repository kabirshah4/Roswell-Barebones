"""Delivers computed setups to a Discord webhook.

The webhook URL IS the credential -- anyone holding it can post to the
channel. Phase 3 shipped a leak where httpx's own INFO logger printed a FRED
key embedded in a request URL on every successful call, so nothing here logs a
URL, and no exception text is echoed either: an httpx error carries the full
request URL in its string form.

The fourth and last module permitted to import httpx.
"""

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO. The webhook URL is a secret.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Discord's own limits. Exceeding either is a 400, not a truncation.
_MAX_EMBEDS = 10
_MAX_FIELD_VALUE = 1024

# Two hues only, matching the terminal's palette: green long, red short.
_COLOUR_LONG = 0x26A65B
_COLOUR_SHORT = 0xE0483E


def _default_poster(url: str, payload: dict) -> int:
    import httpx

    with httpx.Client(timeout=10.0) as client:
        return client.post(url, json=payload).status_code


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def build_payload(setup: Any, stats: dict | None = None) -> dict:
    """Render one setup as a Discord embed.

    Kept pure and separate from sending so the message can be asserted against
    without a network double, and so a formatting bug cannot cost a delivery.
    """
    direction = str(getattr(setup, "direction", "long")).lower()
    long_side = direction != "short"

    risk = None
    entry, stop = getattr(setup, "entry", None), getattr(setup, "stop", None)
    if entry is not None and stop is not None:
        risk = abs(entry - stop)

    lines = [
        f"**Entry** {_fmt(entry)}",
        f"**Stop** {_fmt(stop)}" + (f"  _(risk {_fmt(risk)})_" if risk else ""),
        f"**Target 1** {_fmt(getattr(setup, 'target1', None))}",
        f"**Target 2** {_fmt(getattr(setup, 'target2', None))}",
        f"**R:R** {getattr(setup, 'risk_reward', 0):.2f}",
    ]

    # The engine records factors as {name: passed}. Joining that dict directly
    # yields every key including the ones that FAILED, which would present a
    # 4-of-7 setup as though all seven confluences lined up.
    raw = getattr(setup, "factors", None) or ()
    if isinstance(raw, dict):
        factors = sorted(k for k, v in raw.items() if v)
        missing = sorted(k for k, v in raw.items() if not v)
    else:
        factors, missing = sorted(raw), []
    fields = [
        {"name": "Levels", "value": "\n".join(lines)[:_MAX_FIELD_VALUE]},
        {
            "name": f"Confluence ({len(factors)}/{len(factors) + len(missing)})",
            "value": (
                (", ".join(factors) or "—")
                + (f"\n_missing: {', '.join(missing)}_" if missing else "")
            )[:_MAX_FIELD_VALUE],
            "inline": False,
        },
        {
            "name": "Timeframes",
            "value": str(getattr(setup, "timeframes", "") or "—")[:_MAX_FIELD_VALUE],
            "inline": True,
        },
    ]

    # The grade never travels alone. Measurement showed A+ did not reliably
    # outperform B, so a bare letter in a push notification overstates what is
    # known -- and a push notification is exactly where someone acts on it.
    if stats:
        fields.append({
            "name": f"Measured for grade {getattr(setup, 'grade', '?')}",
            "value": (
                f"{stats.get('signals', 0)} past signals · "
                f"{stats.get('win_rate', 0):.1%} win rate · "
                f"{stats.get('avg_r', 0):+.3f}R average"
            )[:_MAX_FIELD_VALUE],
            "inline": False,
        })
    else:
        fields.append({
            "name": "Measured",
            "value": "No backtest yet for this ticker — the grade is unvalidated.",
            "inline": False,
        })

    earnings = getattr(setup, "earnings_at", None)
    if earnings:
        fields.append({
            "name": "⚠ Earnings",
            "value": f"{earnings} — gap risk the stop does not cover.",
            "inline": False,
        })

    return {
        "embeds": [{
            "title": (
                f"{getattr(setup, 'ticker', '?')} "
                f"{'LONG' if long_side else 'SHORT'} · "
                f"grade {getattr(setup, 'grade', '?')}"
            ),
            "color": _COLOUR_LONG if long_side else _COLOUR_SHORT,
            "fields": fields[:25],
            "footer": {"text": "Computed setup, not advice. No orders placed."},
        }][:_MAX_EMBEDS]
    }


class DiscordClient:
    def __init__(
        self,
        poster: Callable[[str, dict], Any] | None = None,
        webhook_url: str | None = None,
    ) -> None:
        self._url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL") or ""
        self._poster = poster or _default_poster
        if not self._url:
            logger.info("No DISCORD_WEBHOOK_URL; alert delivery disabled")

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def set_webhook_url(self, url: str) -> None:
        """Point at a different webhook without a restart.

        Settings are editable from the UI, and requiring a restart before
        alerts start arriving is the kind of thing people quietly conclude is
        broken. An empty string disables delivery.
        """
        self._url = (url or "").strip()

    def send_message(self, title: str, description: str, long_side: bool = True) -> bool:
        """Deliver a plain embed. Used by user-defined price alerts."""
        if not self._url:
            return False
        payload = {
            "embeds": [{
                "title": title[:256],
                "description": description[:4096],
                "color": _COLOUR_LONG if long_side else _COLOUR_SHORT,
                "footer": {"text": "Computed alert, not advice. No orders placed."},
            }]
        }
        try:
            code = int(self._poster(self._url, payload))
        except Exception as exc:
            logger.warning("Discord delivery failed (%s)", type(exc).__name__)
            return False
        if 200 <= code < 300:
            return True
        logger.warning("Discord rejected the alert (HTTP %d)", code)
        return False

    def send_setup(self, setup: Any, stats: dict | None = None) -> bool:
        """Deliver one setup. True when Discord accepted it. Never raises."""
        if not self._url:
            return False
        try:
            payload = build_payload(setup, stats)
        except Exception:
            # A formatting bug must not look like a delivery failure, or the
            # signal is retried forever against the same broken input.
            logger.warning("Could not format a setup for Discord", exc_info=True)
            return False

        try:
            code = int(self._poster(self._url, payload))
        except Exception as exc:
            # Type only. An httpx exception's string carries the request URL,
            # which here is the credential.
            logger.warning("Discord delivery failed (%s)", type(exc).__name__)
            return False

        if 200 <= code < 300:
            return True
        # Status code only -- never the response body or the URL.
        logger.warning("Discord rejected the alert (HTTP %d)", code)
        return False
