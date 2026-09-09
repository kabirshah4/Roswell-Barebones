"""The forward test's engine room: arm plans, then let later bars decide.

Reads plans from the signal engine and bars from SQLite, and writes what
happened. Deliberately dull — every judgement call lives in `paper.py`, which
is pure and tested; this file only moves rows.

Runs on the poller's cycle, so the record accumulates whether or not anyone is
looking at it. That matters: a forward test you have to remember to run is a
forward test with a survivorship problem.
"""

import json
import logging
from datetime import datetime, timezone

from backend.db import database
from backend.services import freshness
from backend.services import horizons as horizon_defs
from backend.services import paper
from backend.services import signal_engine

log = logging.getLogger(__name__)

# The engine grades every ticker every cycle. Only setups it actually calls
# tradeable are armed — the record is meant to answer "does acting on this
# work", not "what would happen if I took everything".
ARM_VERDICT = "tradeable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def arm(conn, ticker: str, plan, horizon_key: str, interval: str) -> int | None:
    """Write a tradeable plan down, exactly as computed."""
    if plan.verdict != ARM_VERDICT:
        return None
    if plan.entry is None or plan.stop is None or plan.target1 is None:
        return None
    if not paper.viable(plan.entry, plan.stop):
        # A stop inside the spread cannot be held by a real order, so recording
        # its result would be recording arithmetic rather than a trade.
        return None
    if database.has_live_paper_trade(conn, ticker, horizon_key):
        return None
    return database.arm_paper_trade(
        conn,
        ticker=ticker,
        direction=plan.direction,
        horizon=horizon_key,
        interval=interval,
        plan_grade=plan.grade,
        plan_score=plan.score,
        # Stored as the passing factor names, so a later change to the factor
        # set cannot silently reinterpret an old record.
        factors=json.dumps(sorted(k for k, v in plan.factors.items() if v)),
        planned_entry=plan.entry,
        stop=plan.stop,
        target1=plan.target1,
        target2=plan.target2,
        armed_at=_now(),
    )


def resolve(conn, trade: dict, bars: list[dict]) -> str | None:
    """Advance one trade as far as the available bars allow.

    Returns what happened, or None if it is still waiting.
    """
    if trade["entry_at"] is None:
        # Still armed. Only bars that closed after the plan existed may fill
        # it — this is the no-look-ahead guarantee.
        candidates = paper.bars_after(bars, trade["armed_at"])
        if not candidates:
            return None
        fill = paper.fill_entry(
            trade["planned_entry"], trade["direction"], candidates
        )
        if fill is None:
            if len(candidates) >= paper.DEFAULT_ARM_BARS:
                # The setup it described has gone. Not a loss — recorded as
                # expired so the fill rate stays visible.
                database.close_paper_trade(
                    conn, trade["id"], None, candidates[-1]["ts"], "expired"
                )
                return "expired"
            return None
        if not paper.viable(fill.price, trade["stop"]):
            # A gap down onto the stop fills the trade with almost no distance
            # left to it. The risk is not small — the stop is meaningless, and
            # dividing by it manufactures enormous R from an ordinary move.
            # This is where a −28R appeared in the first replay.
            database.close_paper_trade(
                conn, trade["id"], None, fill.at, "unviable"
            )
            return "unviable"
        database.fill_paper_trade(
            conn, trade["id"], fill.price, fill.at, fill.gapped
        )
        trade = {**trade, "entry_price": fill.price, "entry_at": fill.at}

    after = paper.bars_after(bars, trade["entry_at"])
    if not after:
        return None
    exit_ = paper.find_exit(
        trade["entry_price"], trade["stop"], trade["target1"],
        trade["direction"], after,
    )
    if exit_ is None:
        return None
    database.close_paper_trade(
        conn, trade["id"], exit_.price, exit_.at, exit_.reason, exit_.gapped
    )
    return exit_.reason


def run_cycle(db_path, horizon_key: str | None = None) -> dict:
    """One pass: resolve what is live, then arm anything new.

    Resolving first is not cosmetic — it frees a ticker whose trade closed this
    cycle to arm again in the same pass.
    """
    spec = horizon_defs.HORIZONS.get(
        horizon_key or horizon_defs.DEFAULT_HORIZON
    )
    interval = spec.frames[-1]
    armed = resolved = 0

    with database.get_conn(db_path) as conn:
        for trade in database.live_paper_trades(conn):
            bars = database.get_bars(conn, trade["ticker"], trade["interval"],
                                     limit=400)
            try:
                if resolve(conn, trade, bars):
                    resolved += 1
            except Exception:
                log.exception("paper: could not resolve %s", trade["ticker"])

        for ticker in database.list_watchlist(conn):
            frames = _frames(conn, ticker, spec)
            if frames is None:
                continue
            # A plan armed from a session-old bar would be judged against bars
            # it was never really computed from, which is worse than not
            # recording it at all -- the whole record exists to be trusted.
            if freshness.stale_intervals(
                database.newest_bar_timestamps(conn, ticker), spec.frames
            ):
                continue
            try:
                plan = signal_engine.analyse(
                    ticker, frames,
                    earnings_at=database.next_earnings(conn, ticker, _now()),
                    now_iso=_now(),
                )
            except Exception:
                log.exception("paper: could not analyse %s", ticker)
                continue
            if arm(conn, ticker, plan, spec.key, interval) is not None:
                armed += 1

    return {"armed": armed, "resolved": resolved, "horizon": spec.key}


def _frames(conn, ticker: str, spec):
    """The bars the engine needs, or None when they are not cached yet."""
    from backend.services.price_poller import _frames_for

    return _frames_for(conn, ticker, spec)
