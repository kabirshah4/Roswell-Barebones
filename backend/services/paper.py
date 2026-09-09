"""Forward testing: what the engine's own plans actually did.

Every claim this app makes about grades comes from `backtest.py` and
`factor_study.py`, and both are in-sample. The factor study already found the
tell — 4-of-7 confluence outperforms 7-of-7, which is what overfitting looks
like from the inside. No further backtesting settles that. The only honest test
is to freeze a plan, wait, and see.

So: when the engine marks a setup tradeable, the plan is written down and left
alone. Later bars decide whether it filled, and whether it hit the stop or the
target. Nothing is re-derived, nothing is tuned, and the bars used are strictly
those that arrived after the plan existed.

Pure. No I/O, no clock — the caller supplies the bars.

Where the modelling is generous, it says so, because a forward test that
flatters itself is worth less than no forward test at all.
"""

from dataclasses import dataclass

# A stop closer than this to the entry is not a stop, it is noise: the spread
# and one tick of slippage already exceed it, so no real order could hold that
# risk and every R computed against it is fiction.
#
# Found by replaying: a single NVDA plan with a stop five cents from a $43.92
# entry — 0.11% — produced −28R on a gap and dragged the whole sample's mean to
# −202R. The trade was not unusually bad, the denominator was unusually small.
MIN_STOP_FRACTION = 0.0025


def viable(entry: float | None, stop: float | None) -> bool:
    """Can this plan be modelled honestly at all?"""
    if entry is None or stop is None or entry <= 0:
        return False
    return abs(entry - stop) / entry >= MIN_STOP_FRACTION


# A plan that never trades is not a loss, but it cannot stay armed forever
# either — the setup it described has gone. Five bars on the plan's own
# timeframe.
DEFAULT_ARM_BARS = 5

# Neither stop nor target after this many bars: closed at the last price. A
# trade that never resolves is a real outcome and reporting it as "still open"
# forever would quietly drop every mediocre result from the record.
DEFAULT_MAX_HOLD_BARS = 40


@dataclass(frozen=True)
class Fill:
    price: float
    at: str
    gapped: bool


@dataclass(frozen=True)
class Exit:
    price: float
    at: str
    reason: str        # stop | target1 | time
    gapped: bool


def _low(bar) -> float:
    return float(bar["low"])


def _high(bar) -> float:
    return float(bar["high"])


def fill_entry(planned_entry: float, direction: str, bars: list[dict],
               arm_bars: int = DEFAULT_ARM_BARS) -> Fill | None:
    """Did price reach the entry, and at what price would it have filled?

    A long fills when a bar trades down to the entry. If the bar *opens* below
    it the trade fills at the open instead — better than planned, and the one
    place this model is allowed to be generous, because that is genuinely what
    a resting limit order does.
    """
    for bar in bars[:arm_bars]:
        opened = float(bar["open"])
        if direction == "short":
            if opened >= planned_entry:
                return Fill(opened, bar["ts"], gapped=opened > planned_entry)
            if _high(bar) >= planned_entry:
                return Fill(planned_entry, bar["ts"], gapped=False)
        else:
            if opened <= planned_entry:
                return Fill(opened, bar["ts"], gapped=opened < planned_entry)
            if _low(bar) <= planned_entry:
                return Fill(planned_entry, bar["ts"], gapped=False)
    return None


def find_exit(entry_price: float, stop: float, target: float, direction: str,
              bars: list[dict],
              max_hold: int = DEFAULT_MAX_HOLD_BARS) -> Exit | None:
    """Walk forward until the stop or the target is hit.

    Two rules decide whether this is honest:

    1. **The stop is checked first within each bar.** With only OHLC there is
       no way to know which came first intrabar, and assuming the target is
       exactly how a backtest flatters itself. Same convention as
       `factor_study._outcome`.

    2. **A gap through the stop fills at the open, not at the stop.** Assuming
       the stop price would have held through a gap is the single most common
       way a paper record overstates itself — it is where real losses worse
       than −1R come from, and pretending otherwise makes every drawdown look
       survivable.
    """
    for bar in bars[:max_hold]:
        opened = float(bar["open"])

        if direction == "short":
            if opened >= stop:
                return Exit(opened, bar["ts"], "stop", gapped=opened > stop)
            if _high(bar) >= stop:
                return Exit(stop, bar["ts"], "stop", gapped=False)
            if opened <= target:
                return Exit(opened, bar["ts"], "target1", gapped=opened < target)
            if _low(bar) <= target:
                return Exit(target, bar["ts"], "target1", gapped=False)
        else:
            if opened <= stop:
                return Exit(opened, bar["ts"], "stop", gapped=opened < stop)
            if _low(bar) <= stop:
                return Exit(stop, bar["ts"], "stop", gapped=False)
            if opened >= target:
                return Exit(opened, bar["ts"], "target1", gapped=opened > target)
            if _high(bar) >= target:
                return Exit(target, bar["ts"], "target1", gapped=False)

    if len(bars) >= max_hold and max_hold > 0:
        last = bars[max_hold - 1]
        return Exit(float(last["close"]), last["ts"], "time", gapped=False)
    return None


def bars_after(bars: list[dict], iso: str) -> list[dict]:
    """Only bars that closed strictly after the moment given.

    This is the whole no-look-ahead guarantee in one function. A plan computed
    from a bar must never be filled by that same bar.
    """
    return [b for b in bars if str(b["ts"]) > iso]


# The known ways this model is kinder than a broker. Stated here rather than in
# a docstring nobody opens, and surfaced in the UI beside the results.
CAVEATS = (
    "Fills are assumed at the plan's exact entry price. A real limit order at "
    "a busy level may not fill at all, and the trades it misses are not evenly "
    "distributed — the ones that run away without you are the good ones.",
    "No commission, no spread, no slippage on the way in.",
    "Gaps through the stop exit at the open, which is right, but the size of "
    "a gap depends on liquidity this model does not have.",
    "One position per ticker at a time, so a plan that re-fires while a trade "
    "is open is skipped rather than pyramided.",
)
