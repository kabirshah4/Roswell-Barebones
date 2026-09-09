"""A grade must never be displayed without the numbers behind it.

Measurement showed A+ did not reliably outperform B, so a bare letter is a
claim the evidence does not support.
"""

import asyncio
import math

from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services.price_poller import PricePoller


def bars(n=400, start=100.0):
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "ts": (t0 + timedelta(days=i)).isoformat(),
            "open": (p := start * math.exp(0.0009 * i) + (1.2 if i % 3 else -1.2)),
            "high": p * 1.012, "low": p * 0.988, "close": p,
            "volume": 1_000_000.0 * (1.4 if i % 5 == 0 else 1.0),
        }
        for i in range(n)
    ]


class Quote:
    price = prev_close = 100.0
    change_pct = 0.0
    volume = 1
    currency = "USD"


class YF:
    def fetch_quotes(self, ts): return {t: Quote() for t in ts}
    def fetch_intraday(self, t): return []
    def fetch_news(self, t): return []
    def fetch_fundamentals(self, t): return None
    def fetch_bars(self, t, p, i): return []
    def fetch_earnings_dates(self, t, limit=8): return []


def seeded(db_path, ticker="AAPL"):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, ticker)
        database.upsert_bars(conn, ticker, "1d", bars())
    return PricePoller(
        YF(), db_path, Config(db_path=db_path, signal_every_n_cycles=1),
        ai_client=None,
    )


def client(db_path):
    return TestClient(create_app(cfg=Config(db_path=db_path), start_poller=False))


# --- caching ----------------------------------------------------------------

def test_the_poller_caches_a_measured_record(db_path):
    asyncio.run(seeded(db_path).run_once())
    with database.get_conn(db_path) as conn:
        stats = database.get_backtest_stats(conn, "AAPL")
    assert stats, "no measured record was cached"
    for grade, row in stats.items():
        assert row["signals"] >= 0
        assert 0.0 <= row["win_rate"] <= 1.0


def test_recomputing_updates_rather_than_duplicates(db_path):
    poller = seeded(db_path)
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        first = conn.execute("SELECT COUNT(*) c FROM backtest_stats").fetchone()["c"]
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM backtest_stats").fetchone()["c"] == first


def test_too_little_history_caches_nothing(db_path):
    """A win rate from thin history looks identical to one from two years.

    240 bars deliberately: below that the engine's own 200-bar minimum blocks
    the replay anyway, so a smaller fixture would pass with the guard removed
    and prove nothing. At 240 the replay yields ~27 signals — few enough to be
    meaningless, plentiful enough to look authoritative.
    """
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_bars(conn, "AAPL", "1d", bars(n=240))
    poller = PricePoller(
        YF(), db_path, Config(db_path=db_path, signal_every_n_cycles=1),
        ai_client=None)
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert database.get_backtest_stats(conn, "AAPL") == {}


def test_a_backtest_failure_does_not_fail_the_cycle(db_path):
    import backend.services.backtest as bt

    original = bt.run_backtest
    bt.run_backtest = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug"))
    try:
        assert asyncio.run(seeded(db_path).run_once()) is True
    finally:
        bt.run_backtest = original


# --- routes -----------------------------------------------------------------

def test_the_route_serves_the_cache_without_replaying(db_path):
    asyncio.run(seeded(db_path).run_once())
    with client(db_path) as c:
        body = c.get("/api/backtest/AAPL").json()
    assert body["ticker"] == "AAPL"
    assert body["stats"]


def test_an_unmeasured_ticker_returns_empty_not_an_error(db_path):
    with client(db_path) as c:
        r = c.get("/api/backtest/NOPE")
    assert r.status_code == 200
    assert r.json()["stats"] == {}


def test_the_bulk_route_is_keyed_by_ticker_then_grade(db_path):
    asyncio.run(seeded(db_path).run_once())
    with client(db_path) as c:
        stats = c.get("/api/backtest").json()["stats"]
    assert "AAPL" in stats
    assert all(isinstance(v, dict) for v in stats["AAPL"].values())


def test_the_route_is_case_insensitive(db_path):
    asyncio.run(seeded(db_path).run_once())
    with client(db_path) as c:
        assert c.get("/api/backtest/aapl").json()["stats"]


# --- the UI must show it ----------------------------------------------------

def test_the_panel_fetches_the_measured_record(db_path):
    with client(db_path) as c:
        js = c.get("/static/app.js").text
    assert "/api/backtest" in js


def test_a_grade_without_a_record_says_so(db_path):
    """Silence would read as endorsement."""
    with client(db_path) as c:
        js = c.get("/static/app.js").text
    assert "unvalidated" in js


def test_the_panel_shows_the_win_rate_and_average_r(db_path):
    """One renderer for all three lists: the fired setups, the playbook and
    the scanner. Three copies of this wording drifted apart once already."""
    with client(db_path) as c:
        js = c.get("/static/app.js").text
    block = js[js.index("function measuredLine"):js.index("async function renderPlans")]
    assert "win_rate" in block and "avg_r" in block
    assert "win rate" in block

    delegate = js[js.index("function gradeRecord"):]
    delegate = delegate[:delegate.index("\n}")]
    assert "measuredLine(" in delegate, "gradeRecord should not re-implement it"


def test_the_readme_states_the_grades_do_not_reliably_rank(db_path):
    """The measured result contradicts the obvious reading of A+/A/B, and the
    entry point to the project should not let a user assume otherwise."""
    readme = __import__("pathlib").Path("README.md").read_text(encoding="utf-8")
    assert "do not reliably rank" in readme


def test_the_readme_is_not_still_describing_phase_1(db_path):
    readme = __import__("pathlib").Path("README.md").read_text(encoding="utf-8")
    assert "Phase 1: watchlist" not in readme
