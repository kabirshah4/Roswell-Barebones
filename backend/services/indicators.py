"""Pure indicator maths.

Deliberately free of I/O: no network, no database, no filesystem. Everything
takes pandas objects and returns pandas objects, which is what lets the signal
engine be replayed over thousands of historical bars by the backtest without
touching anything external.
"""

import pandas as pd

# EMA200 is the longest lookback any factor uses, so a frame shorter than this
# cannot produce a trustworthy trend reading.
MIN_BARS = 200


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    out = 100.0 - (100.0 / (1.0 + rs))
    # An unbroken run of gains gives avg_loss == 0; RSI is 100 there by definition.
    return out.fillna(100.0).where(avg_gain > 0, 0.0).astype(float)


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series]:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average true range.

    True range uses the previous close as well as the bar's own range, so an
    overnight gap widens it. A stop sized from a range that ignored gaps would
    understate risk exactly when it matters most.
    """
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    true_range = ranges.max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()


def swing_low(low: pd.Series, lookback: int = 20) -> float | None:
    window = low.tail(lookback).dropna()
    return float(window.min()) if len(window) else None


def swing_high(high: pd.Series, lookback: int = 20) -> float | None:
    window = high.tail(lookback).dropna()
    return float(window.max()) if len(window) else None


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate finer bars into coarser ones (1h -> 4h).

    yfinance has no native 4h interval, and one 730d 1h fetch resamples to ~1704
    4h bars — comfortably past MIN_BARS — so this avoids a second network call.

    Bins are anchored to a fixed reference (`origin="epoch"`), not to the first
    timestamp of `df`. The poller re-fetches bars on every scan and the window
    start drifts as old bars age out, so anchoring to `df`'s own start would
    make bucket boundaries — and therefore every 4h candle — shift from one
    fetch to the next for reasons that have nothing to do with price. Epoch
    anchoring keeps bucket starts on a stable wall-clock grid (00:00, 04:00,
    08:00, ... UTC) that's identical regardless of where the fetch window
    happens to begin, which also happens to align sensibly with the US
    session. This matters because the 4h timeframe feeds the `multi_timeframe`
    factor, which distinguishes an A+ grade from an A.
    """
    out = df.resample(rule, origin="epoch").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return out.dropna(subset=["Open", "High", "Low", "Close"])
