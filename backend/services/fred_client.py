"""The only module in the project that knows FRED exists.

Optional: with no API key the client reports `enabled = False` and every call
is a no-op, so the app runs fully without a FRED credential.
"""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from backend.data.macro_releases import IMPACT

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.stlouisfed.org/fred/releases/dates"
_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

# The Treasury constant-maturity curve, short end first. These are the
# benchmarks quoted everywhere; the ordering is the curve's own, not
# alphabetical, because the shape is the whole point of looking at it.
YIELD_SERIES = (
    ("DGS1MO", "1M"), ("DGS3MO", "3M"), ("DGS6MO", "6M"), ("DGS1", "1Y"),
    ("DGS2", "2Y"), ("DGS5", "5Y"), ("DGS7", "7Y"), ("DGS10", "10Y"),
    ("DGS20", "20Y"), ("DGS30", "30Y"),
)


@dataclass(frozen=True)
class YieldPoint:
    series_id: str
    label: str
    percent: float
    observed: str
    month_ago: float | None


@dataclass(frozen=True)
class MacroEvent:
    id: str
    release_id: int
    release_name: str
    event_date: str
    impact: str


# httpx logs every request URL at INFO level, and FRED takes the API key as a
# query parameter - so leaving this at INFO prints the key in plaintext on every
# successful poll, not just on failures. This is the only module using httpx, so
# scoping the silence here is safe.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _default_getter(url: str, params: dict[str, str]) -> Any:
    import httpx

    response = httpx.get(url, params=params, timeout=20.0)
    response.raise_for_status()
    return response.json()


class FredClient:
    def __init__(
        self,
        http_getter: Callable[[str, dict[str, str]], Any] | None = None,
        api_key: str | None = None,
    ) -> None:
        self._getter = http_getter or _default_getter
        self._api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY")

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def fetch_upcoming(self, days: int = 30) -> list[MacroEvent]:
        """Upcoming curated releases. Never raises; [] when disabled or failing."""
        if not self.enabled:
            return []

        today = date.today()
        params = {
            "api_key": self._api_key or "",
            "file_type": "json",
            "realtime_start": today.isoformat(),
            "realtime_end": (today + timedelta(days=days)).isoformat(),
            "include_release_dates_with_no_data": "true",
            "sort_order": "asc",
            "limit": "1000",
        }

        try:
            data = self._getter(_BASE_URL, params)
        except Exception as exc:
            # Never log the exception object or a traceback here. The API key is
            # a query parameter, and httpx.HTTPStatusError.__str__ embeds the
            # full request URL - so exc_info=True would print the key in
            # plaintext to stdout on every failed call.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None:
                logger.warning("FRED request failed with HTTP %s", status)
            else:
                logger.warning("FRED request failed (%s)", type(exc).__name__)
            return []

        if not isinstance(data, dict) or "error_message" in data:
            logger.warning("FRED returned an error payload")
            return []

        events: list[MacroEvent] = []
        for entry in data.get("release_dates") or []:
            try:
                name = entry.get("release_name")
                impact = IMPACT.get(name)
                if impact is None:
                    continue  # uncurated: filtered out, not shown at low priority
                event_date = entry.get("date")
                release_id = int(entry.get("release_id"))
                events.append(
                    MacroEvent(
                        id=f"{release_id}|{event_date}",
                        release_id=release_id,
                        release_name=name,
                        event_date=str(event_date),
                        impact=impact,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Skipping malformed FRED entry (%s)", type(exc).__name__
                )
        return events


    def fetch_yield_curve(self) -> list[YieldPoint]:
        """The Treasury curve, latest close per maturity.

        Never raises: a maturity that fails or has no recent print is dropped
        rather than taking the whole curve with it, because a curve missing its
        7-year is still worth reading and a blank screen is not.
        """
        if not self.enabled:
            return []

        points: list[YieldPoint] = []
        for series_id, label in YIELD_SERIES:
            try:
                payload = self._getter(_OBSERVATIONS_URL, {
                    "series_id": series_id,
                    "api_key": self._api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    # Enough rows to still find a print about a month back
                    # after holidays and the "." FRED writes for a market
                    # closure.
                    "limit": "45",
                })
            except Exception:
                logger.warning("FRED: could not read %s", series_id)
                continue

            observations = [
                o for o in (payload or {}).get("observations", [])
                if o.get("value") not in (".", "", None)
            ]
            if not observations:
                continue
            try:
                latest = observations[0]
                prior = observations[min(len(observations) - 1, 20)]
                points.append(YieldPoint(
                    series_id=series_id,
                    label=label,
                    percent=float(latest["value"]),
                    observed=latest["date"],
                    month_ago=float(prior["value"]) if prior is not latest else None,
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return points
