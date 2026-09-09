"""The forecast must be arithmetic, not a guess dressed up as one."""

import math

import pandas as pd
import pytest

from backend.services import forecast


def series(values):
    idx = pd.date_range("2024-01-01", periods=len(values), freq="D", tz="UTC")
    return pd.DataFrame({"Close": values}, index=idx)


def flat(n=300, price=100.0):
    return series([price] * n)


def drifting(n=300, start=100.0, daily=0.001):
    return series([start * math.exp(daily * i) for i in range(n)])


def noisy(n=300, start=100.0, amp=0.02):
    # Deterministic zig-zag: a real standard deviation, no RNG in tests.
    return series([start * math.exp(amp * ((-1) ** i)) for i in range(n)])


# --- guardrails -------------------------------------------------------------

def test_too_little_history_returns_none():
    """A band from ten bars looks identical to one from two years."""
    assert forecast.build("X", flat(10)) is None


def test_exactly_the_minimum_is_accepted():
    assert forecast.build("X", flat(forecast.MIN_BARS)) is not None


def test_missing_close_column_returns_none():
    df = pd.DataFrame({"Open": [1.0] * 100})
    assert forecast.build("X", df) is None


def test_none_input_returns_none():
    assert forecast.build("X", None) is None


def test_zero_price_returns_none():
    assert forecast.build("X", flat(100, price=0.0)) is None


# --- volatility -------------------------------------------------------------

def test_a_flat_series_has_zero_volatility():
    out = forecast.build("X", flat())
    assert out.annualised_vol == 0.0


def test_a_noisy_series_has_positive_volatility():
    out = forecast.build("X", noisy())
    assert out.annualised_vol > 0


def test_more_movement_means_a_wider_band():
    calm = forecast.build("X", noisy(amp=0.005)).bands[0]
    wild = forecast.build("X", noisy(amp=0.05)).bands[0]
    assert (wild.high_95 - wild.low_95) > (calm.high_95 - calm.low_95)


def test_the_95_band_is_wider_than_the_68_band():
    b = forecast.build("X", noisy()).bands[0]
    assert b.low_95 < b.low_68 <= b.high_68 < b.high_95


def test_a_longer_horizon_is_a_wider_band():
    """Uncertainty grows with the square root of time, so it must not shrink."""
    bands = forecast.build("X", noisy()).bands
    widths = [b.high_95 - b.low_95 for b in bands]
    assert widths == sorted(widths)


def test_the_band_widens_with_root_time_not_linearly():
    out = forecast.build("X", noisy())
    b5, b21 = out.bands[0], out.bands[1]
    ratio = (b21.high_68 - b21.expected) / (b5.high_68 - b5.expected)
    assert 1.5 < ratio < 3.0  # sqrt(21/5) ~= 2.05, not 4.2


# --- the band is lognormal, not symmetric on price --------------------------

def test_the_band_is_asymmetric_because_prices_cannot_go_below_zero():
    out = forecast.build("X", noisy(amp=0.06))
    b = out.bands[-1]
    up = b.high_95 - b.expected
    down = b.expected - b.low_95
    assert up > down


def test_the_downside_never_goes_negative():
    out = forecast.build("X", noisy(amp=0.20))
    assert all(b.low_95 > 0 for b in out.bands)


# --- drift ------------------------------------------------------------------

def test_an_uptrend_projects_upward():
    out = forecast.build("X", drifting(daily=0.002))
    assert out.trend_pct_per_day > 0
    assert out.bands[0].expected > out.last_close


def test_a_downtrend_projects_downward():
    out = forecast.build("X", drifting(daily=-0.002))
    assert out.bands[0].expected < out.last_close


def test_trend_extrapolation_is_damped():
    """Undamped, a 0.2%/day run compounds into a fantasy over a quarter."""
    daily = 0.002
    out = forecast.build("X", drifting(daily=daily))
    assert out.trend_pct_per_day < daily


def test_a_flat_series_has_no_drift():
    assert forecast.build("X", flat()).trend_pct_per_day == 0.0


# --- probabilities ----------------------------------------------------------

def test_the_current_price_is_about_a_coin_flip():
    out = forecast.build("X", noisy())
    p = forecast.probability_above(out, out.last_close, 21)
    assert 0.4 < p < 0.6


def test_a_far_higher_price_is_unlikely():
    out = forecast.build("X", noisy())
    assert forecast.probability_above(out, out.last_close * 3, 5) < 0.05


def test_a_far_lower_price_is_very_likely_to_be_exceeded():
    out = forecast.build("X", noisy())
    assert forecast.probability_above(out, out.last_close * 0.3, 5) > 0.95


def test_probabilities_stay_in_range():
    out = forecast.build("X", noisy())
    for mult in (0.1, 0.5, 1.0, 2.0, 10.0):
        p = forecast.probability_above(out, out.last_close * mult, 21)
        assert 0.0 <= p <= 1.0


def test_a_longer_horizon_makes_a_distant_target_more_reachable():
    out = forecast.build("X", noisy())
    near = forecast.probability_above(out, out.last_close * 1.5, 5)
    far = forecast.probability_above(out, out.last_close * 1.5, 63)
    assert far > near


def test_probability_rejects_nonsense_inputs():
    out = forecast.build("X", noisy())
    assert forecast.probability_above(out, 0, 21) is None
    assert forecast.probability_above(out, 100, 0) is None


def test_a_zero_volatility_series_has_no_probability():
    """With no variance there is no distribution to integrate."""
    out = forecast.build("X", flat())
    assert forecast.probability_above(out, 110, 21) is None


# --- context ----------------------------------------------------------------

def test_range_position_is_high_at_the_top_of_the_range():
    out = forecast.build("X", drifting(daily=0.003))
    assert out.range_position > 0.9


def test_range_position_is_low_at_the_bottom():
    out = forecast.build("X", drifting(daily=-0.003))
    assert out.range_position < 0.1


def test_momentum_note_reads_an_uptrend():
    assert "uptrend" in forecast.build("X", drifting(daily=0.003)).momentum_note


def test_momentum_note_reads_a_downtrend():
    assert "downtrend" in forecast.build("X", drifting(daily=-0.003)).momentum_note


# --- purity -----------------------------------------------------------------

def test_the_module_performs_no_io():
    """Same constraint as indicators and signal_engine: it makes this testable.

    Checked against the parsed imports rather than the raw text, so the prose
    explaining the rule cannot trip the rule.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("backend/services/forecast.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {"math", "dataclasses", "pandas"}, (
        f"forecast.py must stay pure; unexpected imports: "
        f"{imported - {'math', 'dataclasses', 'pandas'}}"
    )


def test_it_is_deterministic():
    a = forecast.build("X", noisy())
    b = forecast.build("X", noisy())
    assert a == b
