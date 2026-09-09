"""Trading horizons: which timeframes to analyse, and how often to look.

A scalper and a position trader want different answers about the same chart.
The engine's arithmetic does not change — ATR for the stop, swing structure for
the trend, targets at 2x and 3x risk — but the *timeframes* it runs on decide
what those numbers mean. A stop sized from 1-minute ATR is a scalp stop; the
same formula on daily bars is a swing stop measured in dollars, not cents.

Every interval here was checked against live yfinance for the 200 bars the
EMA200 in `_trend_up` requires. `4h` is the only non-native interval and is
resampled from `1h`; everything else is fetched directly.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Horizon:
    key: str
    label: str
    hold: str                      # what this horizon is for, in plain words
    frames: tuple[str, str, str]   # fine, middle, coarse
    fetch: dict[str, str]          # interval -> yfinance period
    refresh_seconds: int
    resample: dict[str, str]       # target interval -> source interval


HORIZONS: dict[str, Horizon] = {
    "scalp": Horizon(
        key="scalp", label="SCALP", hold="seconds to minutes",
        frames=("1m", "5m", "15m"),
        # Measured: 1m/7d = 2730 bars, 5m/60d = 4680, 15m/60d = 1560.
        fetch={"1m": "7d", "5m": "60d", "15m": "60d"},
        refresh_seconds=15,
        resample={},
    ),
    "intraday": Horizon(
        key="intraday", label="INTRADAY", hold="minutes to hours",
        frames=("5m", "30m", "1h"),
        fetch={"5m": "60d", "30m": "60d", "1h": "730d"},
        refresh_seconds=60,
        resample={},
    ),
    "swing": Horizon(
        key="swing", label="SWING", hold="days to weeks",
        frames=("1h", "4h", "1d"),
        # 4h is not a yfinance interval; resampled from 1h with an epoch
        # origin so bucket edges do not drift with the fetch window.
        fetch={"1h": "730d", "1d": "2y"},
        refresh_seconds=300,
        resample={"4h": "1h"},
    ),
    "position": Horizon(
        key="position", label="POSITION", hold="weeks to months",
        frames=("1d", "1wk", "1mo"),
        fetch={"1d": "max", "1wk": "max", "1mo": "max"},
        refresh_seconds=3600,
        resample={},
    ),
}

DEFAULT_HORIZON = "swing"


def get(key: str | None) -> Horizon:
    """Resolve a horizon key, falling back to the default rather than raising."""
    return HORIZONS.get((key or "").strip().lower(), HORIZONS[DEFAULT_HORIZON])


def all_intervals(keys: list[str] | None = None) -> set[str]:
    """Every interval that must be cached to serve these horizons."""
    chosen = [HORIZONS[k] for k in (keys or HORIZONS) if k in HORIZONS]
    return {interval for h in chosen for interval in h.fetch}
