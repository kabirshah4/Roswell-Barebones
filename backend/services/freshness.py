"""Is this cached bar recent enough to compute a plan from?

The engine only ever asked whether it had *enough* bars, never whether they
were *recent*. Those are different questions and the second one matters more:
too few bars produces no plan, while old bars produce a plan that looks exactly
like a good one. Measured on a live watchlist, the newest cached 5-minute bar
was a full session behind the tape on every ticker and eight days behind on
one — TSLA 356.99 against a live 382.12, META 576.11 against 610.68. A scalp
plan built on those is not slightly wrong; it is a plan for a different market,
carrying a grade and a measured win rate.

The test is not "younger than N hours". Markets close: on a Monday morning the
newest 5-minute bar is legitimately three days old, and a clock-based rule
either rejects that or accepts a genuinely stale Wednesday. Instead an intraday
frame is compared against the ticker's own newest daily bar, which is the
cheapest available answer to "when was the last session" and needs no market
calendar, no holiday table and no timezone reasoning.

Pure. No I/O.
"""

# Frames that must keep up with the session. Daily and slower are exempt: a
# daily bar being a day old is not staleness, it is what a daily bar is.
INTRADAY = frozenset({"1m", "5m", "15m", "30m", "1h", "4h"})

# A backstop for the case where the daily reference is itself stale, so the
# comparison below would compare two old things and call them agreed. Long
# weekends and holidays fit inside this comfortably.
MAX_INTRADAY_AGE_DAYS = 5


def _date_of(ts: str | None) -> str | None:
    """The calendar date of a bar timestamp, as stored."""
    if not ts:
        return None
    text = str(ts)
    return text[:10] if len(text) >= 10 else None


def _days_between(earlier: str, later: str) -> int:
    from datetime import date

    try:
        a = date.fromisoformat(earlier)
        b = date.fromisoformat(later)
    except ValueError:
        return 0
    return (b - a).days


def is_stale(interval: str, newest_ts: str | None,
             daily_ts: str | None) -> bool:
    """True when this frame is a session or more behind.

    `daily_ts` is the newest daily bar for the same ticker, which stands in for
    "when did the market last trade".
    """
    if interval not in INTRADAY:
        return False
    newest = _date_of(newest_ts)
    if newest is None:
        # No bars at all is a different failure, reported by the count check.
        return False

    reference = _date_of(daily_ts)
    if reference is None:
        return False

    if _days_between(newest, reference) > 0:
        return True
    # Both could be stale together, which the comparison above cannot see.
    return _days_between(newest, reference) < -MAX_INTRADAY_AGE_DAYS


def stale_intervals(newest_by_interval: dict[str, str | None],
                    frames: tuple[str, ...] | list[str]) -> list[str]:
    """Which of the frames a horizon needs are behind the session."""
    daily = newest_by_interval.get("1d")
    return [i for i in frames if is_stale(i, newest_by_interval.get(i), daily)]


def describe(stale: list[str]) -> str:
    """What to tell someone instead of showing them the levels."""
    which = "/".join(stale)
    return (
        f"The cached {which} bars are a session behind, so any levels computed "
        f"from them would describe a market that has already moved. Fetching "
        f"them now; this resolves on the next cycle."
    )
