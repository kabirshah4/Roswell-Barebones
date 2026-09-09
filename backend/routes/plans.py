"""A trade plan for every watchlist ticker.

`/api/signals` lists setups that actually fired, which on a normal day is none
of them — and "no signals" tells the user nothing about what they hold. This
computes the levels for every ticker either way, with an explicit verdict, so a
2-of-7 chart is visible as the weak thing it is rather than silently omitted.

Pure SQL plus a pure function. No network, in keeping with the read-route rule.
"""

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from backend.db import database
from backend.routes import get_db_path
from backend.services import freshness
from backend.services import horizons as horizon_defs
from backend.services import signal_engine
from backend.services.price_poller import _frames_for

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.get("/horizons")
async def list_horizons(db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        selected = database.get_setting(conn, "horizon")
    return {
        "selected": selected or horizon_defs.DEFAULT_HORIZON,
        "horizons": [
            {
                "key": h.key, "label": h.label, "hold": h.hold,
                "frames": list(h.frames), "refresh_seconds": h.refresh_seconds,
            }
            for h in horizon_defs.HORIZONS.values()
        ],
    }


@router.get("")
async def list_plans(
    request: Request, horizon: str = "", db_path: Path = Depends(get_db_path)
) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    plans = []

    with database.get_conn(db_path) as conn:
        # An explicit query wins; otherwise use whatever was last chosen, so a
        # reload does not silently drop back to swing.
        chosen = horizon.strip().lower() or database.get_setting(conn, "horizon")
        spec = horizon_defs.get(chosen)
        previous = database.get_setting(conn, "horizon")
        if horizon.strip():
            database.set_setting(conn, "horizon", spec.key)
        tickers = database.list_watchlist(conn)
        measured = database.all_backtest_stats(conn)
        prices = database.get_prices(conn, tickers)
        by_ticker = {p["ticker"]: p for p in prices}

        for ticker in tickers:
            frames = _frames_for(conn, ticker, spec)
            earnings_at = database.next_earnings(conn, ticker, now_iso)

            # Enough bars is not the same as recent bars, and only the second
            # one is dangerous: too few produces no plan, while old ones
            # produce a plan indistinguishable from a good one.
            stale = freshness.stale_intervals(
                database.newest_bar_timestamps(conn, ticker), spec.frames
            )
            if frames is not None and stale:
                plan = signal_engine._blank_plan(
                    ticker, "insufficient_data", freshness.describe(stale),
                    earnings_at,
                )
            elif frames is None:
                plan = signal_engine._blank_plan(
                    ticker, "insufficient_data",
                    f"Not enough {'/'.join(spec.frames)} bars cached yet. "
                    f"The engine needs 200 on each for its EMA200; they arrive "
                    f"on the next bar cycle.",
                    earnings_at,
                )
            else:
                plan = signal_engine.analyse(
                    ticker, frames, earnings_at=earnings_at, now_iso=now_iso
                )

            passed = sorted(k for k, v in plan.factors.items() if v)
            failed = sorted(k for k, v in plan.factors.items() if not v)
            quote = by_ticker.get(ticker) or {}

            plans.append({
                "ticker": plan.ticker,
                "direction": plan.direction,
                "verdict": plan.verdict,
                "reason": plan.reason,
                "grade": plan.grade,
                "score": plan.score,
                "factors": passed,
                "factors_failed": failed,
                "entry": plan.entry,
                "stop": plan.stop,
                "target1": plan.target1,
                "target2": plan.target2,
                "risk_reward": plan.risk_reward,
                "timeframes": plan.timeframes,
                "earnings_at": plan.earnings_at,
                "last_price": quote.get("price"),
                # The grade never travels without its record; measurement showed
                # A+ does not reliably outperform B.
                "measured": (measured.get(ticker) or {}).get(plan.grade or ""),
            })

    # Actionable first, then by confluence: the point is to see what is worth
    # doing without hunting for it.
    # A horizon the user just switched to needs its bars now, not on the next
    # bar cycle. Scheduled in the background so this response stays fast.
    if horizon.strip() and spec.key != previous:
        poller = getattr(request.app.state, "poller", None)
        if getattr(request.app.state, "poller_task", None) is not None and poller:
            import asyncio

            try:
                asyncio.create_task(poller.warm_horizon(spec.key))
            except RuntimeError:
                pass

    order = {"tradeable": 0, "weak": 1, "blackout": 2, "insufficient_data": 3}
    plans.sort(key=lambda p: (order.get(p["verdict"], 9), -p["score"], p["ticker"]))
    return {
        "plans": plans,
        # Computed fresh on every request, so this is the moment these exact
        # levels were derived -- not when the page happened to load.
        "as_of": now_iso,
        "horizon": {
            "key": spec.key, "label": spec.label, "hold": spec.hold,
            "frames": list(spec.frames), "refresh_seconds": spec.refresh_seconds,
        },
    }
