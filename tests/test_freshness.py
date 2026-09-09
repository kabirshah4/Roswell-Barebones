"""Whether cached bars are recent enough to compute a plan from.

The engine only ever asked whether it had *enough* bars. Too few produces no
plan; too old produces a plan that looks exactly like a good one, carrying a
grade and a measured win rate. Measured on a live watchlist, every ticker's
newest 5-minute bar was a session behind and one was eight days behind — TSLA
356.99 against a live 382.12.
"""

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services import freshness


# --- the rule ----------------------------------------------------------------

def test_an_intraday_bar_behind_the_last_session_is_stale():
    assert freshness.is_stale("5m", "2026-09-02T19:55", "2026-09-03T04:00")


def test_an_intraday_bar_from_the_current_session_is_fine():
    assert not freshness.is_stale("5m", "2026-09-03T14:30", "2026-09-03T04:00")


def test_a_daily_bar_is_never_stale_for_being_a_day_old():
    """That is what a daily bar is. Applying an intraday rule to it would
    refuse every plan overnight."""
    assert not freshness.is_stale("1d", "2026-09-02T04:00", "2026-09-03T04:00")
    assert not freshness.is_stale("1wk", "2026-08-31T04:00", "2026-09-03T04:00")
    assert not freshness.is_stale("1mo", "2026-08-01T04:00", "2026-09-03T04:00")


def test_a_weekend_does_not_make_friday_stale():
    """A clock rule would reject Friday's bars on Monday morning. Comparing
    against the ticker's own newest daily bar handles closures, holidays and
    timezones without a market calendar."""
    assert not freshness.is_stale("5m", "2026-08-28T19:55", "2026-08-28T04:00")


def test_every_intraday_interval_is_covered():
    for interval in ("1m", "5m", "15m", "30m", "1h", "4h"):
        assert freshness.is_stale(interval, "2026-09-01T19:55", "2026-09-03T04:00"), \
            interval


def test_no_bars_is_not_reported_as_staleness():
    """That is the count check's job, and it has a different message."""
    assert not freshness.is_stale("5m", None, "2026-09-03T04:00")


def test_without_a_daily_reference_nothing_is_claimed():
    """The reference is what makes this work; guessing without it would refuse
    a ticker that has only ever had intraday bars."""
    assert not freshness.is_stale("5m", "2026-09-02T19:55", None)


def test_a_stale_daily_reference_does_not_bless_stale_intraday():
    """Both behind together would otherwise agree with each other. The
    backstop catches a reference that has itself stopped updating."""
    assert freshness.is_stale("5m", "2026-01-01T19:55", "2026-01-01T04:00") is False
    assert freshness.is_stale("5m", "2026-09-03T19:55", "2026-01-01T04:00") is True


def test_malformed_timestamps_do_not_raise():
    for bad in ("", "not-a-date", "2026-13-45T00:00", "x"):
        assert freshness.is_stale("5m", bad, "2026-09-03T04:00") in (True, False)


def test_only_the_frames_a_horizon_needs_are_checked():
    newest = {"1m": "2026-09-02T19:59", "5m": "2026-09-02T19:55",
              "1h": "2026-09-03T19:30", "1d": "2026-09-03T04:00"}
    assert freshness.stale_intervals(newest, ("1m", "5m")) == ["1m", "5m"]
    assert freshness.stale_intervals(newest, ("1h", "1d")) == []


def test_the_message_names_the_frames_and_says_it_resolves():
    text = freshness.describe(["1m", "5m"])
    assert "1m/5m" in text
    assert "session behind" in text
    assert "next cycle" in text


def test_freshness_touches_nothing():
    import inspect

    source = inspect.getsource(freshness)
    for banned in ("import os", "sqlite3", "httpx", "requests", "open("):
        assert banned not in source, banned


# --- the playbook ------------------------------------------------------------

class ExplodingClient:
    def fetch_quotes(self, t): raise AssertionError("routes must never fetch")
    def fetch_intraday(self, t): raise AssertionError("routes must never fetch")
    def fetch_news(self, t): raise AssertionError("routes must never fetch")
    def fetch_fundamentals(self, t): raise AssertionError("routes must never fetch")
    def fetch_bars(self, t, p, i): raise AssertionError("routes must never fetch")
    def fetch_earnings_dates(self, t, limit=8): raise AssertionError("routes must never fetch")
    def validate_ticker(self, t): return True


@pytest.fixture
def client(db_path):
    app = create_app(cfg=Config(db_path=db_path), client=ExplodingClient(),
                     start_poller=False)
    with TestClient(app) as c:
        yield c


def seed(db_path, ticker="AAPL", interval="1d", n=260, day_offset=0,
         start_price=100.0):
    """Enough bars to clear the count check, dated from a chosen day."""
    from datetime import datetime, timedelta

    base = datetime(2026, 9, 3) - timedelta(days=day_offset)
    rows = []
    for i in range(n):
        ts = (base - timedelta(minutes=5 * (n - i))).isoformat()
        price = start_price + (i % 17) * 0.5
        rows.append({"ts": ts, "open": price, "high": price + 1,
                     "low": price - 1, "close": price, "volume": 1000})
    with database.get_conn(db_path) as conn:
        if ticker not in database.list_watchlist(conn):
            database.add_watchlist_ticker(conn, ticker)
        database.upsert_bars(conn, ticker, interval, rows)


def test_stale_intraday_bars_produce_a_refusal_not_levels(client, db_path):
    """The bug this exists for. Switching to a fine horizon returned a full
    playbook of confident levels computed from a market that had already
    moved, because the warm runs in the background and the response does
    not wait for it."""
    for interval in ("1m", "5m", "15m"):
        seed(db_path, interval=interval, day_offset=1)
    seed(db_path, interval="1d", day_offset=0)

    plan = client.get("/api/plans?horizon=scalp").json()["plans"][0]
    assert plan["verdict"] == "insufficient_data"
    assert "session behind" in plan["reason"]
    assert plan["entry"] is None, "no levels may be published from stale bars"


def test_fresh_bars_still_produce_a_plan(client, db_path):
    """The guard must not refuse everything."""
    for interval in ("1m", "5m", "15m", "1d"):
        seed(db_path, interval=interval, day_offset=0)
    plan = client.get("/api/plans?horizon=scalp").json()["plans"][0]
    assert plan["verdict"] != "insufficient_data"


def test_a_stale_frame_is_distinguishable_from_a_missing_one(client, db_path):
    """"Warming" and "never had the data" need different messages — one
    resolves on its own and the other does not."""
    seed(db_path, ticker="NODATA", interval="1d", n=260)
    plans = {p["ticker"]: p for p in
             client.get("/api/plans?horizon=scalp").json()["plans"]}
    assert "Not enough" in plans["NODATA"]["reason"]
    assert "session behind" not in plans["NODATA"]["reason"]
