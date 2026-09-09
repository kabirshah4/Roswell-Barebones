import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app


class ExplodingClient:
    """Any network call from a read route is a design violation."""

    def fetch_quotes(self, tickers):
        raise AssertionError("routes must never fetch")

    def fetch_intraday(self, ticker):
        raise AssertionError("routes must never fetch")

    def validate_ticker(self, ticker):
        return True


def make_client(db_path):
    app = create_app(
        cfg=Config(db_path=db_path), client=ExplodingClient(), start_poller=False
    )
    return TestClient(app)


def test_empty_prices(db_path):
    with make_client(db_path) as c:
        assert c.get("/api/prices").json() == {"prices": []}


def test_prices_returns_cached_row(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_price(conn, "AAPL", 110.0, 100.0, 10.0, 42, "USD")
    with make_client(db_path) as c:
        row = c.get("/api/prices").json()["prices"][0]
    assert row["ticker"] == "AAPL"
    assert row["price"] == 110.0
    assert row["change_pct"] == 10.0
    assert row["volume"] == 42
    assert row["is_stale"] is False


def test_prices_reads_never_hit_the_network(db_path):
    """ExplodingClient asserts if touched; reaching this endpoint must not."""
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    with make_client(db_path) as c:
        assert c.get("/api/prices").status_code == 200


def test_unpolled_ticker_appears_with_null_price(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    with make_client(db_path) as c:
        row = c.get("/api/prices").json()["prices"][0]
    assert row["ticker"] == "AAPL"
    assert row["price"] is None
    assert row["is_stale"] is True


def test_prices_follow_watchlist_order(db_path):
    with database.get_conn(db_path) as conn:
        for t in ("MSFT", "AAPL", "NVDA"):
            database.add_watchlist_ticker(conn, t)
    with make_client(db_path) as c:
        order = [r["ticker"] for r in c.get("/api/prices").json()["prices"]]
    assert order == ["MSFT", "AAPL", "NVDA"]


def test_stale_flag_is_exposed_as_bool(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_price(conn, "AAPL", 110.0, 100.0, 10.0, 1, "USD")
        database.mark_stale(conn, ["AAPL"])
    with make_client(db_path) as c:
        assert c.get("/api/prices").json()["prices"][0]["is_stale"] is True


def test_sparkline_returns_points(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_sparkline(conn, "AAPL", [1.0, 2.0, 3.0])
    with make_client(db_path) as c:
        assert c.get("/api/prices/AAPL/sparkline").json() == {
            "ticker": "AAPL",
            "points": [1.0, 2.0, 3.0],
        }


def test_sparkline_empty_before_first_refresh(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    with make_client(db_path) as c:
        assert c.get("/api/prices/AAPL/sparkline").json()["points"] == []


def test_sparkline_404_for_unwatched_ticker(db_path):
    with make_client(db_path) as c:
        assert c.get("/api/prices/NOPE/sparkline").status_code == 404
