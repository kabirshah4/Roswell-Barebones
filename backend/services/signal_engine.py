"""Confluence scoring and risk arithmetic.

No I/O of any kind. `evaluate` takes prepared frames and returns a Setup or
None, which is exactly what lets `backtest.py` replay it bar by bar over years
of history offline.

Every price level here is computed from ATR and swing structure. Nothing in
this module consults a language model, and nothing downstream may substitute a
model-generated number for one of these.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from backend.services import indicators as ind

RR_FLOOR = 2.0
_STOP_ATR_MULT = 1.5
_MIN_SCORE = 4
_MOMENTUM_FLOOR = 45.0
_MOMENTUM_CEILING = 80.0
_NOT_EXTENDED_ATR_MULT = 2.0
_STRUCTURE_LOOKBACK = 20

_REQUIRED = ("1h", "4h", "1d")

_FACTORS = (
    "trend",
    "momentum",
    "macd_cross",
    "volume",
    "structure",
    "not_extended",
    "multi_timeframe",
)


def factor_names() -> tuple[str, ...]:
    return _FACTORS


@dataclass(frozen=True)
class Setup:
    ticker: str
    direction: str
    grade: str
    score: int
    factors: dict[str, bool]
    entry: float
    stop: float
    target1: float
    target2: float
    risk_reward: float
    timeframes: str
    earnings_at: str | None


def _trend_up(df: pd.DataFrame) -> bool:
    close = df["Close"]
    e20, e50, e200 = ind.ema(close, 20), ind.ema(close, 50), ind.ema(close, 200)
    rising = e20.iloc[-1] > e20.iloc[-5]
    return bool(
        close.iloc[-1] > e20.iloc[-1] > e50.iloc[-1] > e200.iloc[-1] and rising
    )


def _momentum_ok(df: pd.DataFrame) -> bool:
    """True when RSI shows bullish momentum that isn't exhausted.

    The floor (45) screens out weak/negative momentum. The ceiling (80)
    screens genuine exhaustion - real strong trends do print RSI into the
    70s, but a reading that high and climbing is stretched. `_not_extended`
    independently screens distance-from-mean using ATR distance from the
    20-EMA; the two are complementary, not redundant - RSI captures the
    speed of recent moves, `_not_extended` captures how far price has run
    from its own average.
    """
    r = ind.rsi(df["Close"]).iloc[-1]
    return bool(_MOMENTUM_FLOOR <= r <= _MOMENTUM_CEILING)


def _structure_ok(df: pd.DataFrame, lookback: int = _STRUCTURE_LOOKBACK) -> bool:
    """True when the market is printing higher lows (intact uptrend structure).

    Splits the lookback window in half and compares the swing low of the
    earlier half against the more recent half - a rising sequence of lows is
    the textbook definition of bullish structure. A naive "is price within
    one ATR of the window's single lowest low" check is nearly always false
    in a genuine trend (a strong rally is, by definition, moving away from
    its recent low) and nearly always true in a selloff (today's low usually
    *is* the window low), which is backwards for a long-setup filter.
    """
    low = df["Low"]
    half = lookback // 2
    prior = ind.swing_low(low.iloc[-lookback:-half], lookback=half)
    recent = ind.swing_low(low.iloc[-half:], lookback=half)
    return bool(prior is not None and recent is not None and recent > prior)


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _in_blackout(
    earnings_at: str | None, now_iso: str | None, blackout_days: int
) -> bool:
    """True when a confirmed earnings date falls within the window ahead.

    An earnings gap routinely exceeds a 1.5x ATR stop, which makes the whole
    risk calculation meaningless across the event. Unknown earnings never
    triggers the blackout - it is recorded as unverified instead.
    """
    if not earnings_at:
        return False
    now = _parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
    return now <= _parse_iso(earnings_at) <= now + timedelta(days=blackout_days)


@dataclass(frozen=True)
class Plan:
    """What a trade in this ticker would look like right now, and whether it
    is worth taking.

    `evaluate()` answers "is there a setup"; most tickers most of the time get
    None, which tells the user nothing. A Plan always carries the levels -- they
    come from the same ATR and swing arithmetic either way -- plus an explicit
    verdict, so a 2-of-7 chart is shown as the weak thing it is instead of
    being silently omitted or dressed up as a signal.
    """

    ticker: str
    direction: str
    verdict: str          # tradeable | weak | blackout | insufficient_data
    reason: str
    score: int
    factors: dict[str, bool]
    grade: str | None
    entry: float | None
    stop: float | None
    target1: float | None
    target2: float | None
    risk_reward: float | None
    timeframes: str
    earnings_at: str | None

    @property
    def actionable(self) -> bool:
        return self.verdict == "tradeable"


def _blank_plan(ticker: str, verdict: str, reason: str,
                earnings_at: str | None = None,
                timeframes: tuple[str, ...] = _REQUIRED) -> Plan:
    return Plan(
        ticker=ticker, direction="long", verdict=verdict, reason=reason,
        score=0, factors={}, grade=None, entry=None, stop=None,
        target1=None, target2=None, risk_reward=None,
        timeframes=",".join(timeframes), earnings_at=earnings_at,
    )


def analyse(
    ticker: str,
    frames: dict[str, pd.DataFrame],
    earnings_at: str | None = None,
    now_iso: str | None = None,
    blackout_days: int = 3,
) -> Plan:
    """The full read on one ticker, always. Never returns None.

    `evaluate()` is a filter over this, so the two can never disagree about
    the levels.
    """
    # Whatever frames the caller supplied ARE the timeframes to analyse. This
    # used to be hardcoded to the swing set, which silently made every other
    # horizon report "insufficient_data" no matter how much data it had.
    required = tuple(frames) if frames else _REQUIRED
    if len(required) < 3 or any(
        frames.get(tf) is None or len(frames[tf]) < ind.MIN_BARS for tf in required
    ):
        return _blank_plan(
            ticker, "insufficient_data",
            f"Needs {ind.MIN_BARS} bars on "
            f"{', '.join(required) if required else 'each timeframe'}; "
            f"still caching.",
            earnings_at, required,
        )

    if _in_blackout(earnings_at, now_iso, blackout_days):
        return _blank_plan(
            ticker, "blackout",
            f"Earnings on {str(earnings_at)[:10]} — a gap routinely exceeds a "
            f"1.5x ATR stop, so the risk calculation does not hold across it.",
            earnings_at, required,
        )

    # The finest frame drives entry, ATR and structure; the others confirm.
    base = frames[required[0]]
    close = base["Close"]
    last = float(close.iloc[-1])

    e20 = ind.ema(close, 20)
    atr_series = ind.atr(base["High"], base["Low"], close)
    atr_now = float(atr_series.iloc[-1])
    if atr_now <= 0:
        return _blank_plan(
            ticker, "insufficient_data",
            "ATR is zero — no measurable range to size a stop against.",
            earnings_at, required,
        )

    macd_line, signal_line = ind.macd(close)
    crossed = bool(
        (macd_line.iloc[-4:] > signal_line.iloc[-4:]).any()
        and macd_line.iloc[-5] <= signal_line.iloc[-5]
    )

    vol = base["Volume"]
    vol_ok = bool(vol.iloc[-1] > 1.2 * vol.tail(20).mean())

    structure = _structure_ok(base)

    not_extended = bool(
        abs(last - float(e20.iloc[-1])) <= _NOT_EXTENDED_ATR_MULT * atr_now
    )

    mtf = all(_trend_up(frames[tf]) and _momentum_ok(frames[tf]) for tf in required)

    factors = {
        "trend": _trend_up(base),
        "momentum": _momentum_ok(base),
        "macd_cross": crossed,
        "volume": vol_ok,
        "structure": structure,
        "not_extended": not_extended,
        "multi_timeframe": mtf,
    }
    score = sum(1 for v in factors.values() if v)

    entry = last
    stop = entry - _STOP_ATR_MULT * atr_now
    risk = entry - stop
    target1 = entry + 2.0 * risk
    target2 = entry + 3.0 * risk
    rr = (target1 - entry) / risk if risk > 0 else 0.0

    if score >= 6 and factors["multi_timeframe"]:
        grade = "A+"
    elif score >= 5:
        grade = "A"
    elif score >= _MIN_SCORE:
        grade = "B"
    else:
        grade = None

    missing = sorted(k for k, v in factors.items() if not v)
    # Note: with targets defined as multiples of risk, `rr` is always exactly
    # 2.0 and `risk` is always positive (atr_now > 0 was checked above), so
    # neither branch below is reachable today. They are kept as structural
    # guards for the day the target multiples become configurable -- at which
    # point they start doing real work.
    if risk <= 0:
        verdict, reason = "weak", "Stop would sit at or above entry."
    elif rr < RR_FLOOR:
        verdict, reason = "weak", f"Reward:risk {rr:.2f} is below the {RR_FLOOR:.1f} floor."
    elif grade is None:
        verdict = "weak"
        reason = (
            f"Only {score} of 7 confluences — missing {', '.join(missing)}. "
            f"Below the {_MIN_SCORE}-factor minimum, so these levels are what a "
            f"trade would look like, not a reason to take one."
        )
    else:
        verdict = "tradeable"
        reason = (
            f"{score} of 7 confluences at grade {grade}."
            + (f" Missing {', '.join(missing)}." if missing else "")
        )

    return Plan(
        ticker=ticker, direction="long", verdict=verdict, reason=reason,
        score=score, factors=factors, grade=grade,
        entry=round(entry, 4), stop=round(stop, 4),
        target1=round(target1, 4), target2=round(target2, 4),
        risk_reward=round(rr, 2),
        timeframes=",".join(required), earnings_at=earnings_at,
    )


def evaluate(
    ticker: str,
    frames: dict[str, pd.DataFrame],
    earnings_at: str | None = None,
    now_iso: str | None = None,
    blackout_days: int = 3,
) -> Setup | None:
    """A tradeable setup, or None. A thin filter over `analyse`.

    `actionable` and `grade is None` currently coincide -- a plan is weak
    exactly when it has no grade -- so this reads as belt and braces. It is
    not: the verdict is what the interface acts on, and the grade is what the
    alert prints, so requiring both keeps them from drifting apart silently.
    """
    plan = analyse(ticker, frames, earnings_at, now_iso, blackout_days)
    if not plan.actionable or plan.grade is None:
        return None
    return Setup(
        ticker=plan.ticker, direction=plan.direction, grade=plan.grade,
        score=plan.score, factors=plan.factors, entry=plan.entry,
        stop=plan.stop, target1=plan.target1, target2=plan.target2,
        risk_reward=plan.risk_reward, timeframes=plan.timeframes,
        earnings_at=plan.earnings_at,
    )
