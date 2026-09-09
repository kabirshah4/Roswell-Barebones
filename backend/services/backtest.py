"""Historical replay of the signal engine.

This is what makes the grades falsifiable. Without it the factor thresholds are
assertions; with it they are measurements. Stated limitation: daily bars only,
fills assumed at the computed entry, no slippage or commission - it measures
whether the logic discriminates, not what an account would have earned.
"""

from dataclasses import dataclass

import pandas as pd

from backend.services import indicators as ind
from backend.services import signal_engine as eng


@dataclass(frozen=True)
class GradeStats:
    grade: str
    signals: int
    wins: int
    losses: int
    open_trades: int
    win_rate: float
    avg_r: float


def run_backtest(
    ticker: str, daily: pd.DataFrame, lookahead: int = 40
) -> dict[str, GradeStats]:
    """Replay the engine bar by bar. Returns per-grade statistics."""
    if len(daily) < ind.MIN_BARS + 10:
        return {}

    buckets: dict[str, list[float | None]] = {}

    for i in range(ind.MIN_BARS, len(daily) - 1):
        # Slice strictly up to i: the engine must never see a future bar.
        window = daily.iloc[: i + 1]
        frames = {"1h": window, "4h": window, "1d": window}
        setup = eng.evaluate(ticker, frames)
        if setup is None:
            continue

        future = daily.iloc[i + 1 : i + 1 + lookahead]
        outcome: float | None = None
        for _, bar in future.iterrows():
            if bar["Low"] <= setup.stop:
                outcome = -1.0
                break
            if bar["High"] >= setup.target1:
                outcome = 2.0
                break
        buckets.setdefault(setup.grade, []).append(outcome)

    stats: dict[str, GradeStats] = {}
    for grade, outcomes in buckets.items():
        wins = sum(1 for o in outcomes if o is not None and o > 0)
        losses = sum(1 for o in outcomes if o is not None and o < 0)
        open_trades = sum(1 for o in outcomes if o is None)
        resolved = wins + losses
        realised = [o for o in outcomes if o is not None]
        stats[grade] = GradeStats(
            grade=grade,
            signals=len(outcomes),
            wins=wins,
            losses=losses,
            open_trades=open_trades,
            win_rate=round(wins / resolved, 4) if resolved else 0.0,
            avg_r=round(sum(realised) / len(realised), 4) if realised else 0.0,
        )
    return stats
