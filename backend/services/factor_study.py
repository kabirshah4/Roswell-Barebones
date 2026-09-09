"""Which of the seven confluence factors actually predicts anything?

The engine scores all seven as if they carry equal weight. Nobody had checked.
This replays `analyse` over cached daily bars, records each factor's verdict
alongside what happened next, and reports the win rate and average R with the
factor present versus absent.

Pure over its inputs — frames in, numbers out — like `indicators`,
`signal_engine` and `backtest`, which is what lets it be run against any set of
bars without touching the network.

**A stated limitation, because it changes how one row should be read.** Like
the backtest, this passes the same daily frame for 1h, 4h and 1d. The
`multi_timeframe` factor therefore compares a frame against itself and
collapses to `trend AND momentum` on daily bars. Its number here is not
evidence about multi-timeframe confirmation; it is an artefact of the harness.
Every other factor reads a distinct signal and is measured honestly.
"""

from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

from backend.services import indicators as ind
from backend.services import signal_engine as eng

LOOKAHEAD = 40


@dataclass(frozen=True)
class Split:
    """Outcomes with a factor present, and without it."""

    factor: str
    with_n: int
    with_win: float
    with_avg_r: float
    without_n: int
    without_win: float
    without_avg_r: float

    @property
    def win_edge(self) -> float:
        """Percentage points of win rate the factor adds. May be negative."""
        return round((self.with_win - self.without_win) * 100, 2)

    @property
    def r_edge(self) -> float:
        return round(self.with_avg_r - self.without_avg_r, 4)


def _outcome(plan, future: pd.DataFrame) -> float | None:
    """+2R if the first target hits first, -1R if the stop does, None if open.

    The stop is checked first within each bar: with only OHLC there is no way
    to know which came first intrabar, and assuming the target is how a
    backtest flatters itself.
    """
    for _, bar in future.iterrows():
        if bar["Low"] <= plan.stop:
            return -1.0
        if bar["High"] >= plan.target1:
            return 2.0
    return None


def study_frame(ticker: str, daily: pd.DataFrame, lookahead: int = LOOKAHEAD):
    """Replay one ticker. Returns (per-factor outcomes, per-score outcomes)."""
    by_factor: dict[str, dict[bool, list[float]]] = defaultdict(
        lambda: {True: [], False: []}
    )
    by_score: dict[int, list[float]] = defaultdict(list)

    if len(daily) < ind.MIN_BARS + lookahead + 10:
        return by_factor, by_score

    for i in range(ind.MIN_BARS, len(daily) - lookahead - 1):
        # Sliced strictly up to i: the engine must never see a future bar.
        window = daily.iloc[: i + 1]
        plan = eng.analyse(ticker, {"1h": window, "4h": window, "1d": window})
        if plan.entry is None or not plan.factors:
            continue
        result = _outcome(plan, daily.iloc[i + 1 : i + 1 + lookahead])
        if result is None:
            continue
        for name, present in plan.factors.items():
            by_factor[name][bool(present)].append(result)
        by_score[plan.score].append(result)

    return by_factor, by_score


def _summarise(outcomes: list[float]) -> tuple[int, float, float]:
    if not outcomes:
        return 0, 0.0, 0.0
    wins = sum(1 for o in outcomes if o > 0)
    return (
        len(outcomes),
        round(wins / len(outcomes), 4),
        round(sum(outcomes) / len(outcomes), 4),
    )


def combine(per_ticker: list[tuple]) -> dict:
    """Merge several tickers' results into one report, best factor first."""
    factors: dict[str, dict[bool, list[float]]] = defaultdict(
        lambda: {True: [], False: []}
    )
    scores: dict[int, list[float]] = defaultdict(list)
    for by_factor, by_score in per_ticker:
        for name, sides in by_factor.items():
            factors[name][True].extend(sides[True])
            factors[name][False].extend(sides[False])
        for score, outcomes in by_score.items():
            scores[score].extend(outcomes)

    splits = []
    for name, sides in factors.items():
        with_n, with_win, with_r = _summarise(sides[True])
        without_n, without_win, without_r = _summarise(sides[False])
        if with_n == 0 or without_n == 0:
            # A factor that never varied says nothing either way.
            continue
        splits.append(Split(name, with_n, with_win, with_r,
                            without_n, without_win, without_r))
    splits.sort(key=lambda s: s.r_edge, reverse=True)

    score_rows = []
    for score in sorted(scores):
        n, win, avg_r = _summarise(scores[score])
        score_rows.append({"score": score, "n": n, "win_rate": win, "avg_r": avg_r})

    total = sum(len(s[True]) for s in factors.values()) // max(len(factors), 1)
    return {
        "factors": [
            {
                "factor": s.factor,
                "with": {"n": s.with_n, "win_rate": s.with_win, "avg_r": s.with_avg_r},
                "without": {"n": s.without_n, "win_rate": s.without_win,
                            "avg_r": s.without_avg_r},
                "win_edge_pp": s.win_edge,
                "r_edge": s.r_edge,
            }
            for s in splits
        ],
        "by_score": score_rows,
        "resolved": sum(r["n"] for r in score_rows),
        "caveat": (
            "The harness passes the same daily frame for 1h, 4h and 1d, so "
            "multi_timeframe compares a frame against itself and collapses to "
            "trend AND momentum. Read that row as an artefact, not evidence."
        ),
    }
