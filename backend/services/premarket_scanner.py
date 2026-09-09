"""Scans the whole US market for liquid movers, then grades the best of them.

Two stages, because they answer different questions:

1. **Narrow.** A market screener filters every US stock server-side on price
   and ten-day average volume, and returns the pre-market gap and the volume
   behind it. This is the only practical way to look at every symbol —
   fetching twelve thousand quotes from Yahoo is not. Configure the endpoint
   with ``SCREENER_SCAN_URL``; without it this stage yields nothing and the
   panel says so.

2. **Grade.** The survivors are ranked on a session-appropriate score, and the
   top few get the real signal engine run over their cached bars, producing the
   same entry/stop/T1/T2 as everything else in the app.

The ranking is arithmetic on measured fields, not a guess: a gap nobody traded
is noise, so gap size is weighted by the volume behind it, and a name already
overbought is marked down because the stop sits further away.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.services.screener_client import scanner_url

logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)

_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

COLUMNS = [
    "name", "description", "close", "average_volume_10d_calc",
    "premarket_change", "premarket_volume", "premarket_close",
    "change", "volume", "relative_volume_10d_calc",
    "market_cap_basic", "ATR", "RSI", "sector",
]

# The user's screen: liquid enough to get filled, priced high enough that a
# 1.5x ATR stop is not a rounding error.
DEFAULT_MIN_PRICE = 15.0
DEFAULT_MIN_AVG_VOLUME = 10_000_000

MAX_CANDIDATES = 40


@dataclass(frozen=True)
class Candidate:
    ticker: str
    name: str
    price: float
    avg_volume_10d: float
    premarket_change: float | None
    premarket_volume: float | None
    change: float | None
    volume: float | None
    relative_volume: float | None
    market_cap: float | None
    atr: float | None
    rsi: float | None
    sector: str | None
    score: float
    why: list[str] = field(default_factory=list)


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return None if out != out else out   # NaN
    except (TypeError, ValueError):
        return None


def _default_poster(url: str, payload: dict) -> Any:
    import httpx

    with httpx.Client(timeout=30.0, headers=_HEADERS) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


def rank(candidate: dict, session_name: str) -> tuple[float, list[str]]:
    """Score one row, and say why. Higher is better.

    Every term is a measured field, weighted for what actually matters at this
    hour. Nothing here is arbitrary: a gap with no volume behind it does not
    hold, relative volume is what separates a real move from drift, and an
    already-extended name is marked down because entering there puts the stop
    further away for the same target.
    """
    reasons: list[str] = []
    score = 0.0

    premarket = session_name == "premarket"
    move = _num(candidate.get("premarket_change") if premarket
                else candidate.get("change")) or 0.0

    # DIRECTIONAL, not absolute. The signal engine is long-only -- every one of
    # its seven factors wants an uptrend -- so ranking by |move| fills the list
    # with big decliners that then score 0/7 and crowd out the names actually
    # worth grading. Measured: an unweighted |move| put a -39% stock top.
    #
    # Capped at 15%: beyond that a gap is usually a halt, an offering or a
    # reverse split rather than something to trade.
    magnitude = min(abs(move), 15.0)
    if move > 0:
        score += magnitude * 2.5
        if move >= 1.0:
            reasons.append(f"{'gapping' if premarket else 'moving'} +{move:.1f}%")
    else:
        # Not excluded outright: a controlled pullback in an uptrend is a
        # legitimate long entry, and the engine decides. Just not promoted.
        score -= magnitude * 1.5
        if move <= -1.0:
            reasons.append(f"down {move:.1f}% — long setups need the trend intact")

    # Volume behind the move. In pre-market the absolute figure matters; once
    # open, relative volume is the honest measure.
    if premarket:
        pm_vol = _num(candidate.get("premarket_volume")) or 0.0
        avg = _num(candidate.get("average_volume_10d_calc")) or 1.0
        participation = pm_vol / avg
        score += min(participation * 100.0, 30.0)
        if pm_vol >= 100_000:
            reasons.append(f"{pm_vol / 1_000:.0f}k pre-market shares")
        elif pm_vol < 10_000:
            # A 5% gap on 200 shares is a quote, not a market.
            score -= 15.0
            reasons.append("almost no pre-market volume — gap may not hold")
    else:
        rvol = _num(candidate.get("relative_volume_10d_calc")) or 0.0
        score += min(rvol, 5.0) * 6.0
        if rvol >= 1.5:
            reasons.append(f"{rvol:.1f}x normal volume")
        elif rvol < 0.7:
            score -= 10.0
            reasons.append(f"only {rvol:.1f}x normal volume")

    # Room to run. RSI above 75 or below 25 means the stop is far from entry.
    rsi = _num(candidate.get("RSI"))
    if rsi is not None:
        if 40.0 <= rsi <= 70.0:
            score += 8.0
            reasons.append(f"RSI {rsi:.0f}, not extended")
        elif rsi > 80.0 or rsi < 20.0:
            score -= 12.0
            reasons.append(f"RSI {rsi:.0f} — extended, stop sits far from entry")

    # Liquidity beyond the floor is genuinely better: easier fills, tighter
    # spreads. Logarithmic, because 100M is not ten times better than 10M.
    avg_volume = _num(candidate.get("average_volume_10d_calc")) or 0.0
    if avg_volume > 0:
        import math

        score += min(math.log10(avg_volume / 1_000_000) * 4.0, 12.0)

    return round(score, 2), reasons


class PremarketScanner:
    def __init__(self, poster: Any = None) -> None:
        self._poster = poster or _default_poster

    def scan(
        self,
        session_name: str = "premarket",
        min_price: float = DEFAULT_MIN_PRICE,
        min_avg_volume: float = DEFAULT_MIN_AVG_VOLUME,
        limit: int = MAX_CANDIDATES,
    ) -> list[Candidate]:
        """Liquid movers, best first. Returns [] on failure; never raises."""
        sort_by = "premarket_change" if session_name == "premarket" else "change"
        payload = {
            "filter": [
                {"left": "type", "operation": "equal", "right": "stock"},
                {"left": "close", "operation": "greater", "right": min_price},
                {"left": "average_volume_10d_calc", "operation": "greater",
                 "right": min_avg_volume},
            ],
            "columns": COLUMNS,
            # Pull well beyond `limit`: the scanner sorts by raw move, and the
            # ranking below reorders on volume and extension, so the best name
            # is routinely not in the top few by gap alone.
            "range": [0, 300],
            "sort": {"sortBy": sort_by, "sortOrder": "desc"},
        }
        url = scanner_url("america")
        if url is None:
            logger.info("No SCREENER_SCAN_URL configured; skipping market scan")
            return []

        try:
            body = self._poster(url, payload)
        except Exception as exc:
            logger.warning("Pre-market scan failed (%s)", type(exc).__name__)
            return []

        out: list[Candidate] = []
        for row in (body or {}).get("data") or []:
            try:
                values = dict(zip(COLUMNS, row["d"]))
                score, why = rank(values, session_name)
                out.append(
                    Candidate(
                        ticker=str(row["s"]).split(":")[-1].replace(".", "-"),
                        name=str(values.get("description") or ""),
                        price=_num(values.get("close")) or 0.0,
                        avg_volume_10d=_num(values.get("average_volume_10d_calc")) or 0.0,
                        premarket_change=_num(values.get("premarket_change")),
                        premarket_volume=_num(values.get("premarket_volume")),
                        change=_num(values.get("change")),
                        volume=_num(values.get("volume")),
                        relative_volume=_num(values.get("relative_volume_10d_calc")),
                        market_cap=_num(values.get("market_cap_basic")),
                        atr=_num(values.get("ATR")),
                        rsi=_num(values.get("RSI")),
                        sector=values.get("sector"),
                        score=score,
                        why=why,
                    )
                )
            except Exception:
                continue

        out.sort(key=lambda c: c.score, reverse=True)
        return out[:limit]
