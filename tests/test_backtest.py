import numpy as np
import pandas as pd

from backend.services.backtest import GradeStats, run_backtest


def make_daily(closes):
    n = len(closes)
    idx = pd.date_range("2020-01-01", periods=n, freq="1D", tz="UTC")
    c = pd.Series([float(x) for x in closes], index=idx)
    return pd.DataFrame(
        {"Open": c.shift(1).fillna(c.iloc[0]), "High": c * 1.01,
         "Low": c * 0.99, "Close": c, "Volume": pd.Series([1000.0] * n, index=idx)}
    )


def make_uptrend(n: int = 500, seed: int = 7):
    """A strongly-trending but noisy uptrend.

    A perfectly monotonic series (every bar up by the same fixed amount) is
    not a "clean uptrend" as real markets ever produce one - it is a
    degenerate input under which RSI's average-loss term is exactly zero on
    every bar, which pins RSI at exactly 100.0 for the entire history. That
    sits above the signal engine's momentum ceiling (80), so momentum_ok
    (and, transitively, multi_timeframe) is false on every bar and no grade
    ever reaches the score floor - zero signals, regardless of how strong the
    trend "looks". A realistic uptrend has some down days; this generates one
    with a fixed seed so the test stays deterministic.
    """
    rng = np.random.default_rng(seed)
    steps = rng.normal(loc=0.5, scale=0.6, size=n)
    closes = 100 + np.cumsum(steps)
    return make_daily(closes)


def test_flat_market_yields_no_signals():
    stats = run_backtest("FLAT", make_daily([100.0] * 400))
    assert sum(s.signals for s in stats.values()) == 0


def test_strong_uptrend_yields_signals_and_wins():
    """A series that only rises must resolve to target before stop."""
    stats = run_backtest("UP", make_uptrend())
    total = sum(s.signals for s in stats.values())
    assert total > 0, "a clean uptrend should generate at least one setup"
    wins = sum(s.wins for s in stats.values())
    losses = sum(s.losses for s in stats.values())
    assert wins > losses


def test_stats_are_keyed_by_grade():
    stats = run_backtest("UP", make_uptrend())
    assert set(stats) <= {"A+", "A", "B"}
    for grade, s in stats.items():
        assert isinstance(s, GradeStats)
        assert s.grade == grade


def test_win_rate_is_a_fraction_between_zero_and_one():
    stats = run_backtest("UP", make_uptrend())
    for s in stats.values():
        assert 0.0 <= s.win_rate <= 1.0


def test_counts_are_internally_consistent():
    stats = run_backtest("UP", make_uptrend())
    for s in stats.values():
        assert s.signals == s.wins + s.losses + s.open_trades


def test_short_history_returns_empty_rather_than_raising():
    assert run_backtest("SHORT", make_daily([100.0] * 50)) == {}


def test_backtest_does_not_look_ahead():
    """Evaluate must only see bars up to the decision point.

    If it peeked at future bars, a series that rises then collapses would still
    score wins on the pre-collapse signals.
    """
    rising = list(100 + np.arange(300) * 0.5)
    collapse = list(np.linspace(rising[-1], rising[-1] * 0.5, 200))
    stats = run_backtest("TRAP", make_daily(rising + collapse))
    assert sum(s.losses for s in stats.values()) > 0, (
        "signals fired before the collapse must be able to lose"
    )
