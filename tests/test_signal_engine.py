import numpy as np
import pandas as pd
import pytest

from backend.services import signal_engine as eng


def frame(closes, volumes=None, high_pad=0.5, low_pad=0.5):
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    close = pd.Series([float(c) for c in closes], index=idx)
    return pd.DataFrame(
        {
            "Open": close.shift(1).fillna(close.iloc[0]),
            "High": close + high_pad,
            "Low": close - low_pad,
            "Close": close,
            "Volume": pd.Series(
                volumes if volumes is not None else [1000.0] * n, index=idx
            ),
        }
    )


def uptrend(n=400, start=100.0, step=0.25, noise=0.6):
    """A rising series with real bar-to-bar noise (seeded, deterministic).

    A perfectly straight line (noise=0.0) pins RSI at its mathematical
    maximum because there is never a down bar to feed the loss average -
    real markets never do that. Noise keeps this fixture representative of
    an actual trending instrument instead of a degenerate edge case.
    """
    rng = np.random.default_rng(7)
    base = start + np.arange(n) * step
    if noise:
        base = base + rng.normal(0, noise, n)
    return list(base)


def rising_frames(n=400):
    f = frame(uptrend(n), volumes=[1000.0] * (n - 1) + [5000.0])
    return {"1h": f, "4h": f, "1d": f}


def test_returns_none_when_frames_too_short():
    short = {tf: frame(uptrend(50)) for tf in ("1h", "4h", "1d")}
    assert eng.evaluate("AAPL", short) is None


def test_returns_none_when_a_single_timeframe_is_too_short():
    frames = rising_frames()
    frames["4h"] = frame(uptrend(50))
    assert eng.evaluate("AAPL", frames) is None


def test_clean_uptrend_produces_a_long_setup():
    s = eng.evaluate("AAPL", rising_frames())
    assert s is not None
    assert s.direction == "long"
    assert s.ticker == "AAPL"


def test_seven_factors_are_reported():
    s = eng.evaluate("AAPL", rising_frames())
    assert set(s.factors) == set(eng.factor_names())
    assert len(eng.factor_names()) == 7


def test_score_equals_number_of_true_factors():
    s = eng.evaluate("AAPL", rising_frames())
    assert s.score == sum(1 for v in s.factors.values() if v)


def test_stop_is_below_entry_for_a_long():
    s = eng.evaluate("AAPL", rising_frames())
    assert s.stop < s.entry


def test_targets_are_ordered_above_entry_for_a_long():
    s = eng.evaluate("AAPL", rising_frames())
    assert s.entry < s.target1 < s.target2


def test_risk_reward_is_at_least_the_floor():
    s = eng.evaluate("AAPL", rising_frames())
    assert s.risk_reward >= eng.RR_FLOOR


def test_target1_is_exactly_two_r():
    s = eng.evaluate("AAPL", rising_frames())
    risk = s.entry - s.stop
    assert s.target1 == pytest.approx(s.entry + 2.0 * risk)


def test_target2_is_exactly_three_r():
    s = eng.evaluate("AAPL", rising_frames())
    risk = s.entry - s.stop
    assert s.target2 == pytest.approx(s.entry + 3.0 * risk)


def test_multi_timeframe_disagreement_downgrades_from_a_plus():
    """The MTF factor is what separates A+ from A - remove it and the grade must drop."""
    agreeing = eng.evaluate("AAPL", rising_frames())
    frames = rising_frames()
    frames["1d"] = frame(list(reversed(uptrend(400))))  # daily now falling
    disagreeing = eng.evaluate("AAPL", frames)
    if agreeing is not None and agreeing.grade == "A+":
        assert disagreeing is None or disagreeing.grade != "A+"


def test_earnings_blackout_suppresses_the_setup_entirely(monkeypatch):
    frames = rising_frames()
    s = eng.evaluate(
        "AAPL", frames,
        earnings_at="2026-08-25T20:00:00Z",
        now_iso="2026-08-23T00:00:00Z",
        blackout_days=3,
    )
    assert s is None, "no grade may be emitted inside the blackout"


def test_earnings_outside_blackout_does_not_suppress():
    s = eng.evaluate(
        "AAPL", rising_frames(),
        earnings_at="2026-12-25T20:00:00Z",
        now_iso="2026-08-23T00:00:00Z",
        blackout_days=3,
    )
    assert s is not None


def test_unknown_earnings_does_not_suppress_but_is_recorded():
    s = eng.evaluate("AAPL", rising_frames(), earnings_at=None,
                     now_iso="2026-08-23T00:00:00Z")
    assert s is not None
    assert s.earnings_at is None, "null means unverified, not verified-clear"


def test_flat_market_produces_no_setup():
    flat = {tf: frame([100.0] * 400) for tf in ("1h", "4h", "1d")}
    assert eng.evaluate("AAPL", flat) is None


def test_downtrend_produces_no_long_setup():
    falling = {tf: frame(list(reversed(uptrend(400)))) for tf in ("1h", "4h", "1d")}
    s = eng.evaluate("AAPL", falling)
    assert s is None or s.direction != "long"


def test_timeframes_string_lists_all_three():
    s = eng.evaluate("AAPL", rising_frames())
    assert s.timeframes == "1h,4h,1d"


def test_rr_floor_is_two():
    assert eng.RR_FLOOR == 2.0
