"""Grounded price projections.

The point of this module is to let the analyst answer "where might this go?"
with arithmetic instead of vibes. A language model asked to guess a price will
produce a confident number with nothing behind it; the same model handed a
measured volatility band will reason about a real distribution.

Pure functions only -- no I/O, no network, no database. Same constraint as
`indicators.py` and `signal_engine.py`, and for the same reason: it makes the
output reproducible and testable against known inputs.

Nothing here predicts the future. It describes what the recent past implies
about the range of outcomes, which is a different and much more defensible
claim.
"""

import math
from dataclasses import dataclass, field

import pandas as pd

# Below this the volatility estimate is noise. Two months of daily bars is the
# minimum that gives a usable standard deviation.
MIN_BARS = 40

TRADING_DAYS = 252

# Horizons the outlook is reported over, in trading days.
DEFAULT_HORIZONS = (5, 21, 63)  # ~1 week, ~1 month, ~1 quarter


@dataclass(frozen=True)
class Band:
    """The projected range for one horizon."""

    horizon_days: int
    expected: float      # drift-projected central estimate
    low_68: float        # +/- 1 sigma
    high_68: float
    low_95: float        # +/- 2 sigma
    high_95: float


@dataclass(frozen=True)
class Outlook:
    ticker: str
    last_close: float
    annualised_vol: float        # as a fraction, e.g. 0.34 == 34%
    daily_vol: float
    trend_pct_per_day: float     # measured drift, damped
    momentum_note: str
    range_position: float | None  # 0 == 52w low, 1 == 52w high
    bands: list[Band] = field(default_factory=list)


def _returns(close: pd.Series) -> pd.Series:
    """Log returns. Log rather than simple so they add across time."""
    return (close / close.shift(1)).apply(
        lambda x: math.log(x) if x and x > 0 else float("nan")
    ).dropna()


def realised_vol(close: pd.Series, lookback: int = 60) -> float:
    """Annualised standard deviation of daily log returns."""
    rets = _returns(close).tail(lookback)
    if len(rets) < 2:
        return 0.0
    return float(rets.std(ddof=1) * math.sqrt(TRADING_DAYS))


def drift(close: pd.Series, lookback: int = 60, damping: float = 0.35) -> float:
    """Average daily log return, heavily damped.

    Undamped trend extrapolation is how a projection turns into a fantasy: a
    stock that ran 40% in a quarter did not thereby earn a 160% annual forecast.
    The damping keeps recent direction visible without letting it dominate the
    volatility band, which is the part actually supported by evidence.
    """
    rets = _returns(close).tail(lookback)
    if len(rets) < 2:
        return 0.0
    return float(rets.mean()) * damping


def _band(last: float, mu: float, sigma_daily: float, days: int) -> Band:
    """Project one horizon under a lognormal random walk.

    Prices are bounded below by zero and compound multiplicatively, so the
    band is built in log space and exponentiated. A symmetric band drawn
    directly on price would put the same distance below as above, which
    understates upside and overstates downside.
    """
    horizon_sigma = sigma_daily * math.sqrt(days)
    centre = mu * days

    def at(z: float) -> float:
        return round(last * math.exp(centre + z * horizon_sigma), 2)

    return Band(
        horizon_days=days,
        expected=at(0.0),
        low_68=at(-1.0), high_68=at(1.0),
        low_95=at(-2.0), high_95=at(2.0),
    )


def _momentum_note(close: pd.Series) -> str:
    """A plain-language read of where price sits against its own averages."""
    if len(close) < 50:
        return "not enough history to judge trend"
    last = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma50 = float(close.tail(50).mean())
    if last > ma20 > ma50:
        return "above both the 20 and 50 day averages (uptrend)"
    if last < ma20 < ma50:
        return "below both the 20 and 50 day averages (downtrend)"
    if last > ma50:
        return "above the 50 day average but not the 20 (chop, upward bias)"
    return "below the 50 day average (chop, downward bias)"


def _range_position(close: pd.Series) -> float | None:
    """Where price sits in its own 52-week range, 0 (low) to 1 (high)."""
    window = close.tail(TRADING_DAYS)
    if len(window) < MIN_BARS:
        return None
    lo, hi = float(window.min()), float(window.max())
    if hi <= lo:
        return None
    return round((float(close.iloc[-1]) - lo) / (hi - lo), 3)


def probability_above(outlook: Outlook, price: float, days: int) -> float | None:
    """Chance of finishing above `price` in `days`, under the same walk.

    This is what turns "could it hit 250?" into a number with a stated model
    behind it, rather than an opinion.
    """
    if outlook.last_close <= 0 or price <= 0 or days <= 0:
        return None
    sigma = outlook.daily_vol * math.sqrt(days)
    if sigma <= 0:
        return None
    centre = outlook.trend_pct_per_day * days
    z = (math.log(price / outlook.last_close) - centre) / sigma
    # Survival function of the standard normal.
    return round(0.5 * math.erfc(z / math.sqrt(2)), 4)


def build(
    ticker: str,
    daily: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> Outlook | None:
    """Compute the outlook from daily bars, or None if there is too little data.

    Returning None rather than a low-confidence guess is deliberate: a band
    computed from ten bars looks identical to one computed from two years, and
    the caller cannot tell them apart.
    """
    if daily is None or len(daily) < MIN_BARS or "Close" not in daily:
        return None

    close = daily["Close"].astype(float)
    last = float(close.iloc[-1])
    if last <= 0:
        return None

    annual = realised_vol(close)
    sigma_daily = annual / math.sqrt(TRADING_DAYS) if annual > 0 else 0.0
    mu = drift(close)

    return Outlook(
        ticker=ticker,
        last_close=round(last, 2),
        annualised_vol=round(annual, 4),
        daily_vol=round(sigma_daily, 6),
        trend_pct_per_day=round(mu, 6),
        momentum_note=_momentum_note(close),
        range_position=_range_position(close),
        bands=[_band(last, mu, sigma_daily, d) for d in horizons],
    )
