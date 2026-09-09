"""Pre-market and intraday scanner.

Stage 1 narrows the whole US market to liquid movers via the configured
screener's server-side filter (`SCREENER_SCAN_URL`; unset means stage 1
returns nothing and the panel says so). Stage 2 runs the real signal engine over cached bars for
the best of them, so the levels come from the same arithmetic as everything
else in the app rather than a second, parallel idea of where a stop goes.

Stage 1 is a network call, so this route is explicitly user-triggered — like
`POST /api/symbols/refresh` — and its results are cached for the poller to
refresh on a cadence.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from backend.db import database
from backend.routes import get_db_path
from backend.services import market_session, signal_engine
from backend.services import horizons as horizon_defs
from backend.services.premarket_scanner import (
    DEFAULT_MIN_AVG_VOLUME,
    DEFAULT_MIN_PRICE,
)

router = APIRouter(prefix="/api/scanner", tags=["scanner"])

CACHE_KEY = "scanner_results"

# How many survivors get bars fetched and the engine run over them. Each costs
# a Yahoo round trip, so this is the line between "graded" and "listed".
GRADE_TOP_N = 8


def _cached(db_path: Path) -> dict | None:
    with database.get_conn(db_path) as conn:
        raw = database.get_setting(conn, CACHE_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


@router.get("")
async def read_scan(db_path: Path = Depends(get_db_path)) -> dict:
    """The last scan. Never fetches — refreshing is an explicit POST."""
    session = market_session.current()
    cached = _cached(db_path) or {"candidates": [], "as_of": None}
    cached["session"] = {
        "name": session.name, "label": session.label,
        "is_tradeable": session.is_tradeable,
        "ranks_on": session.ranks_on,
    }
    return cached


@router.post("/refresh")
async def refresh_scan(
    request: Request,
    min_price: float = DEFAULT_MIN_PRICE,
    min_avg_volume: float = DEFAULT_MIN_AVG_VOLUME,
    db_path: Path = Depends(get_db_path),
) -> dict:
    """Rescan now."""
    session = market_session.current()
    scanner = request.app.state.scanner
    client = request.app.state.client

    candidates = await asyncio.to_thread(
        scanner.scan, session.name, min_price, min_avg_volume
    )

    graded = await _grade(candidates[:GRADE_TOP_N], client, db_path)
    rest = [_plain(c) for c in candidates[GRADE_TOP_N:]]

    payload = {
        "candidates": graded + rest,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "filters": {"min_price": min_price, "min_avg_volume": min_avg_volume},
        "graded": len(graded),
        "session": {
            "name": session.name, "label": session.label,
            "is_tradeable": session.is_tradeable,
            # Which move field is live right now. `premarket_change` holds
            # yesterday's gap once the session opens, so displaying it then is
            # a stale number presented as today's.
            "ranks_on": session.ranks_on,
        },
    }
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, CACHE_KEY, json.dumps(payload))
    return payload


async def _measured(db_path: Path, ticker: str, grade: str | None) -> dict | None:
    """The measured record for this ticker's grade, replaying if need be.

    Scanner candidates are rarely on the watchlist, so the poller has never
    backtested them. The replay costs about 0.4s and only runs for the handful
    of names actually being graded.
    """
    if not grade:
        return None
    with database.get_conn(db_path) as conn:
        cached = database.get_backtest_stats(conn, ticker)
    if cached.get(grade):
        return cached[grade]

    try:
        import pandas as pd

        from backend.services import backtest

        with database.get_conn(db_path) as conn:
            rows = database.get_bars(conn, ticker, "1d", limit=2000)
        if len(rows) < 250:
            return None
        frame = pd.DataFrame(rows)
        frame.index = pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
        frame = frame.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume"})
        stats = await asyncio.to_thread(backtest.run_backtest, ticker, frame)
        if stats:
            with database.get_conn(db_path) as conn:
                database.upsert_backtest_stats(conn, ticker, stats)
        hit = stats.get(grade)
        return None if hit is None else vars(hit)
    except Exception:
        # A missing record is reported as unvalidated, never as a number.
        return None


def _plain(candidate) -> dict:
    return {
        "ticker": candidate.ticker,
        "name": candidate.name,
        "price": candidate.price,
        "avg_volume_10d": candidate.avg_volume_10d,
        "premarket_change": candidate.premarket_change,
        "premarket_volume": candidate.premarket_volume,
        "change": candidate.change,
        "relative_volume": candidate.relative_volume,
        "rsi": candidate.rsi,
        "sector": candidate.sector,
        "score": candidate.score,
        "why": candidate.why,
        "plan": None,
        "measured": None,
    }


async def _grade(candidates: list, client, db_path: Path) -> list[dict]:
    """Fetch bars for the best candidates and run the engine over them.

    Uses the intraday horizon: a name selected for gapping this morning is not
    a two-week swing idea, and sizing its stop off daily ATR would put it
    dollars away.
    """
    from backend.services.price_poller import _frames_for

    spec = horizon_defs.get("intraday")
    out: list[dict] = []

    for candidate in candidates:
        row = _plain(candidate)
        try:
            for interval, period in spec.fetch.items():
                bars = await asyncio.to_thread(
                    client.fetch_bars, candidate.ticker, period, interval
                )
                if bars:
                    with database.get_conn(db_path) as conn:
                        database.upsert_bars(conn, candidate.ticker, interval, bars)

            # Daily bars as well: the backtest replays on them, and a grade
            # without its measured record is the bare letter this app refuses
            # to show anywhere else.
            daily = await asyncio.to_thread(
                client.fetch_bars, candidate.ticker, "2y", "1d"
            )
            if daily:
                with database.get_conn(db_path) as conn:
                    database.upsert_bars(conn, candidate.ticker, "1d", daily)

            with database.get_conn(db_path) as conn:
                frames = _frames_for(conn, candidate.ticker, spec)
            if frames is not None:
                plan = signal_engine.analyse(candidate.ticker, frames)
                row["measured"] = await _measured(
                    db_path, candidate.ticker, plan.grade
                )
                row["plan"] = {
                    "verdict": plan.verdict,
                    "reason": plan.reason,
                    "grade": plan.grade,
                    "score": plan.score,
                    "factors": sorted(k for k, v in plan.factors.items() if v),
                    "factors_failed": sorted(
                        k for k, v in plan.factors.items() if not v
                    ),
                    "entry": plan.entry,
                    "stop": plan.stop,
                    "target1": plan.target1,
                    "target2": plan.target2,
                    "risk_reward": plan.risk_reward,
                    "timeframes": plan.timeframes,
                }
        except Exception:
            # A candidate we could not grade is still worth listing; it just
            # arrives without levels.
            pass
        out.append(row)
    return out
