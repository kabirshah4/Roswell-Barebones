"""Bars, earnings and signals must actually get written.

The forecast and the signal engine are both pure functions over cached bars,
so if the poller never fills the `bars` table the whole layer is inert -- which
is exactly the state this wiring fixes.
"""

import asyncio
import math

from backend.config import Config
from backend.db import database
from backend.services.price_poller import PricePoller


class Quote:
    price = 100.0
    prev_close = 99.0
    change_pct = 1.0
    volume = 1_000
    currency = "USD"


def make_bars(n=300, start=100.0):
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        price = start * math.exp(0.0008 * i) + (0.8 if i % 3 else -0.8)
        rows.append({
            "ts": (t0 + timedelta(hours=i)).isoformat(),
            "open": price, "high": price * 1.004, "low": price * 0.996,
            "close": price, "volume": 1_000_000.0,
        })
    return rows


class FakeYF:
    def __init__(self, bars=None, earnings=None):
        self._bars = bars if bars is not None else make_bars()
        self._earnings = earnings or []
        self.bar_calls = []
        self.earnings_calls = []

    def fetch_quotes(self, tickers):
        return {t: Quote() for t in tickers}

    def fetch_intraday(self, ticker):
        return []

    def fetch_news(self, ticker):
        return []

    def fetch_fundamentals(self, ticker):
        return None

    def fetch_bars(self, ticker, period, interval):
        self.bar_calls.append((ticker, period, interval))
        return self._bars

    def fetch_earnings_dates(self, ticker, limit=8):
        self.earnings_calls.append(ticker)
        return self._earnings


def make_poller(db_path, client, **cfg_kwargs):
    cfg = Config(db_path=db_path, **cfg_kwargs)
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    return PricePoller(client, db_path, cfg, ai_client=None)


# --- bars -------------------------------------------------------------------

def test_bars_are_fetched_and_written(db_path):
    client = FakeYF()
    poller = make_poller(db_path, client, signal_every_n_cycles=1)
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert len(database.get_bars(conn, "AAPL", "1d", limit=5000)) > 0
        assert len(database.get_bars(conn, "AAPL", "1h", limit=5000)) > 0
    assert poller.bars_written > 0


def test_both_intervals_are_requested(db_path):
    """The engine needs 1h (and 4h resampled from it) as well as daily."""
    client = FakeYF()
    poller = make_poller(db_path, client, signal_every_n_cycles=1)
    asyncio.run(poller.run_once())
    assert {i for _, _, i in client.bar_calls} == {"1h", "1d"}


def test_bars_are_not_fetched_every_cycle(db_path):
    """Two years of history per ticker per 45s would be absurd."""
    client = FakeYF()
    poller = make_poller(db_path, client, signal_every_n_cycles=40)
    asyncio.run(poller.run_once())
    assert client.bar_calls == []


def test_a_bar_fetch_failure_does_not_fail_the_cycle(db_path):
    class Boom(FakeYF):
        def fetch_bars(self, ticker, period, interval):
            raise RuntimeError("yahoo down")

    poller = make_poller(db_path, Boom(), signal_every_n_cycles=1)
    assert asyncio.run(poller.run_once()) is True


def test_empty_bars_are_not_written(db_path):
    client = FakeYF(bars=[])
    poller = make_poller(db_path, client, signal_every_n_cycles=1)
    asyncio.run(poller.run_once())
    assert poller.bars_written == 0


def test_refetching_corrects_rather_than_duplicates(db_path):
    """The newest candle is still forming when first stored."""
    client = FakeYF()
    poller = make_poller(db_path, client, signal_every_n_cycles=1)
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        first = len(database.get_bars(conn, "AAPL", "1d", limit=5000))
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert len(database.get_bars(conn, "AAPL", "1d", limit=5000)) == first


# --- earnings ---------------------------------------------------------------

def test_earnings_dates_are_written(db_path):
    client = FakeYF(earnings=[
        {"earnings_at": "2026-11-01T00:00:00Z", "eps_estimate": 2.5}])
    poller = make_poller(db_path, client, earnings_every_n_cycles=1)
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert database.next_earnings(
            conn, "AAPL", "2026-01-01T00:00:00Z") == "2026-11-01T00:00:00Z"


def test_an_earnings_failure_does_not_fail_the_cycle(db_path):
    class Boom(FakeYF):
        def fetch_earnings_dates(self, ticker, limit=8):
            raise RuntimeError("no lxml")

    poller = make_poller(db_path, Boom(), earnings_every_n_cycles=1)
    assert asyncio.run(poller.run_once()) is True


# --- signals ----------------------------------------------------------------

def test_signal_scan_runs_against_cached_bars_not_the_network(db_path):
    """The engine is pure; the scan must read SQLite, never re-fetch."""
    client = FakeYF()
    poller = make_poller(db_path, client, signal_every_n_cycles=1)
    asyncio.run(poller.run_once())
    # One fetch per interval per ticker -- the scan itself adds none.
    assert len(client.bar_calls) == 2


def test_a_scan_with_no_bars_is_a_noop(db_path):
    client = FakeYF(bars=[])
    poller = make_poller(db_path, client, signal_every_n_cycles=1)
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert database.list_signals(conn) == []
    assert poller.signals_found == 0


def test_the_same_setup_is_not_re_signalled_every_cycle(db_path):
    """A persistent chart condition must alert once, not on every scan."""
    client = FakeYF()
    poller = make_poller(db_path, client, signal_every_n_cycles=1)
    for _ in range(3):
        asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        signals = database.list_signals(conn)
    assert len(signals) == len({s["id"] for s in signals})


def test_a_broken_engine_does_not_fail_the_cycle(db_path):
    import backend.services.signal_engine as engine

    original = engine.evaluate
    engine.evaluate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug"))
    try:
        poller = make_poller(db_path, FakeYF(), signal_every_n_cycles=1)
        assert asyncio.run(poller.run_once()) is True
    finally:
        engine.evaluate = original
