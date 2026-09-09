import numpy as np
import pandas as pd
import pytest

from backend.services import indicators as ind


def series(values):
    return pd.Series([float(v) for v in values])


def test_ema_of_constant_series_is_that_constant():
    result = ind.ema(series([5] * 50), span=10)
    assert result.iloc[-1] == pytest.approx(5.0)


def test_ema_matches_pandas_reference():
    s = series(range(1, 51))
    expected = s.ewm(span=10, adjust=False).mean().iloc[-1]
    assert ind.ema(s, span=10).iloc[-1] == pytest.approx(expected)


def test_ema_reacts_faster_than_a_longer_span():
    s = series([10] * 30 + [20] * 5)
    assert ind.ema(s, span=5).iloc[-1] > ind.ema(s, span=20).iloc[-1]


def test_rsi_of_monotonic_rise_is_high():
    assert ind.rsi(series(range(1, 60))).iloc[-1] > 95


def test_rsi_of_monotonic_fall_is_low():
    assert ind.rsi(series(range(60, 1, -1))).iloc[-1] < 5


def test_rsi_is_bounded_zero_to_hundred():
    rng = np.random.default_rng(0)
    s = series(100 + rng.normal(0, 2, 300).cumsum())
    r = ind.rsi(s).dropna()
    assert r.min() >= 0 and r.max() <= 100


def test_macd_line_positive_when_short_term_stronger():
    s = series(list(range(1, 40)) + list(range(40, 80, 2)))
    macd_line, signal_line = ind.macd(s)
    assert macd_line.iloc[-1] > 0
    assert len(macd_line) == len(signal_line) == len(s)


def test_atr_of_flat_series_is_zero():
    n = 40
    flat = series([10] * n)
    assert ind.atr(flat, flat, flat).iloc[-1] == pytest.approx(0.0)


def test_atr_grows_with_range():
    n = 40
    close = series([10] * n)
    narrow = ind.atr(series([10.5] * n), series([9.5] * n), close).iloc[-1]
    wide = ind.atr(series([13] * n), series([7] * n), close).iloc[-1]
    assert wide > narrow


def test_atr_accounts_for_gaps():
    """True range must use the previous close, not just the bar's own range."""
    high = series([10, 10, 20])
    low = series([9, 9, 19])
    close = series([9.5, 9.5, 19.5])
    tr_last = ind.atr(high, low, close, period=1).iloc[-1]
    assert tr_last > 1.0, "a gap up from 9.5 to 19 must register more than the 1.0 bar range"


def test_swing_low_finds_the_minimum_in_lookback():
    assert ind.swing_low(series([5, 3, 7, 9, 8]), lookback=5) == pytest.approx(3.0)


def test_swing_high_finds_the_maximum_in_lookback():
    assert ind.swing_high(series([5, 3, 7, 9, 8]), lookback=5) == pytest.approx(9.0)


def test_swing_ignores_bars_outside_the_lookback():
    s = series([1] + [5, 6, 7, 8])
    assert ind.swing_low(s, lookback=4) == pytest.approx(5.0)


def test_swing_returns_none_on_empty_series():
    assert ind.swing_low(series([])) is None


def test_resample_1h_to_4h_aggregates_ohlcv_correctly():
    """Buckets are anchored to the epoch grid (00:00/04:00/08:00/... UTC), not
    to the first timestamp in the frame. Bars run 09:00-16:00 UTC, so they
    split across the 08:00, 12:00, and 16:00 grid lines into three buckets:
      [08:00,12:00): 09:00,10:00,11:00 -> Open=1,  High=4, Low=0, Close=3.5, Vol=30
      [12:00,16:00): 12:00,13:00,14:00,15:00 -> Open=4, High=8, Low=3, Close=7.5, Vol=40
      [16:00,20:00): 16:00 -> Open=8, High=9, Low=7, Close=8.5, Vol=10
    """
    idx = pd.date_range("2026-01-01 09:00", periods=8, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "Open": [1, 2, 3, 4, 5, 6, 7, 8],
            "High": [2, 3, 4, 5, 6, 7, 8, 9],
            "Low": [0, 1, 2, 3, 4, 5, 6, 7],
            "Close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5],
            "Volume": [10] * 8,
        },
        index=idx,
    )
    out = ind.resample_ohlcv(df, "4h")
    assert len(out) == 3

    first = out.iloc[0]
    assert first.name == pd.Timestamp("2026-01-01 08:00", tz="UTC")
    assert first.Open == 1 and first.High == 4 and first.Low == 0 and first.Close == 3.5
    assert first.Volume == 30

    second = out.iloc[1]
    assert second.name == pd.Timestamp("2026-01-01 12:00", tz="UTC")
    assert second.Open == 4 and second.High == 8 and second.Low == 3 and second.Close == 7.5
    assert second.Volume == 40

    third = out.iloc[2]
    assert third.name == pd.Timestamp("2026-01-01 16:00", tz="UTC")
    assert third.Open == 8 and third.High == 9 and third.Low == 7 and third.Close == 8.5
    assert third.Volume == 10


def test_resample_drops_empty_buckets():
    idx = pd.DatetimeIndex(["2026-01-01 09:00", "2026-01-02 09:00"], tz="UTC")
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 1]},
        index=idx,
    )
    assert len(ind.resample_ohlcv(df, "4h")) == 2


def test_resample_buckets_are_stable_across_window_shifts():
    """Bucket boundaries must not depend on where the fetch window starts.

    The poller re-fetches with a drifting window; unstable buckets would make
    the 4h timeframe - and the multi_timeframe factor - change for reasons
    unrelated to price.
    """
    idx = pd.date_range("2026-01-01 03:00", periods=24, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "Open": range(1, 25),
            "High": [v + 1 for v in range(1, 25)],
            "Low": [v - 1 for v in range(1, 25)],
            "Close": [v + 0.5 for v in range(1, 25)],
            "Volume": [10] * 24,
        },
        index=idx,
    )
    full = ind.resample_ohlcv(df, "4h")
    shifted = ind.resample_ohlcv(df.iloc[1:], "4h")

    common = full.index.intersection(shifted.index)
    assert len(common) > 0, "expected some overlapping bucket timestamps between the two windows"
    assert (full.loc[common, "Close"] == shifted.loc[common, "Close"]).all()


def test_min_bars_covers_ema200():
    assert ind.MIN_BARS >= 200
