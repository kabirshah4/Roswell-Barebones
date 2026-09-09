import sqlite3

import pytest

from backend.db import database


def test_init_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"watchlist", "price_cache", "sparkline_cache"} <= names


def test_init_is_idempotent(db_path):
    database.init_db(db_path)
    database.init_db(db_path)
    with database.get_conn(db_path) as conn:
        assert database.list_watchlist(conn) == []


def test_wal_mode_is_enabled(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_add_and_list_watchlist(conn):
    database.add_watchlist_ticker(conn, "AAPL")
    database.add_watchlist_ticker(conn, "MSFT")
    assert database.list_watchlist(conn) == ["AAPL", "MSFT"]


def test_duplicate_ticker_raises(conn):
    database.add_watchlist_ticker(conn, "AAPL")
    with pytest.raises(sqlite3.IntegrityError):
        database.add_watchlist_ticker(conn, "AAPL")


def test_remove_ticker(conn):
    database.add_watchlist_ticker(conn, "AAPL")
    assert database.remove_watchlist_ticker(conn, "AAPL") is True
    assert database.list_watchlist(conn) == []


def test_remove_missing_ticker_returns_false(conn):
    assert database.remove_watchlist_ticker(conn, "NOPE") is False


def test_upsert_price_replaces_rather_than_appends(conn):
    database.upsert_price(conn, "AAPL", 100.0, 99.0, 1.01, 500, "USD")
    database.upsert_price(conn, "AAPL", 101.0, 99.0, 2.02, 600, "USD")
    rows = database.get_prices(conn)
    assert len(rows) == 1
    assert rows[0]["price"] == 101.0
    assert rows[0]["volume"] == 600


def test_upsert_price_clears_stale_flag(conn):
    database.upsert_price(conn, "AAPL", 100.0, 99.0, 1.01, 500, "USD")
    database.mark_stale(conn, ["AAPL"])
    assert database.get_prices(conn)[0]["is_stale"] == 1
    database.upsert_price(conn, "AAPL", 102.0, 99.0, 3.03, 700, "USD")
    assert database.get_prices(conn)[0]["is_stale"] == 0


def test_mark_stale_preserves_last_known_values(conn):
    database.upsert_price(conn, "AAPL", 100.0, 99.0, 1.01, 500, "USD")
    database.mark_stale(conn, ["AAPL"])
    row = database.get_prices(conn)[0]
    assert row["price"] == 100.0
    assert row["is_stale"] == 1


def test_get_prices_filters_by_ticker(conn):
    database.upsert_price(conn, "AAPL", 100.0, 99.0, 1.01, 1, "USD")
    database.upsert_price(conn, "MSFT", 200.0, 199.0, 0.5, 2, "USD")
    rows = database.get_prices(conn, ["MSFT"])
    assert [r["ticker"] for r in rows] == ["MSFT"]


def test_sparkline_roundtrip(conn):
    database.upsert_sparkline(conn, "AAPL", [1.0, 2.0, 3.0])
    assert database.get_sparkline(conn, "AAPL") == [1.0, 2.0, 3.0]


def test_sparkline_missing_returns_none(conn):
    assert database.get_sparkline(conn, "NOPE") is None


def test_upsert_price_column_placement(conn):
    """Verify each column value lands in the correct column (catch transposition bugs)."""
    database.upsert_price(
        conn,
        ticker="TEST",
        price=1.0,
        prev_close=2.0,
        change_pct=3.0,
        volume=4,
        currency="XYZ"
    )
    row = database.get_prices(conn, ["TEST"])[0]
    assert row["price"] == 1.0, "price should be 1.0"
    assert row["prev_close"] == 2.0, "prev_close should be 2.0"
    assert row["change_pct"] == 3.0, "change_pct should be 3.0"
    assert row["volume"] == 4, "volume should be 4"
    assert row["currency"] == "XYZ", "currency should be XYZ"


def test_get_conn_rollback_on_exception(db_path):
    """Verify that writes are rolled back when an exception occurs inside the context manager."""
    # Write initial data
    with database.get_conn(db_path) as conn:
        database.upsert_price(conn, "AAPL", 100.0, 99.0, 1.01, 500, "USD")

    # Trigger an exception inside the context manager to test rollback
    try:
        with database.get_conn(db_path) as conn:
            database.upsert_price(conn, "AAPL", 200.0, 199.0, 2.02, 600, "USD")
            raise ValueError("Intentional error to trigger rollback")
    except ValueError:
        pass

    # Verify the second write was rolled back (price should still be 100.0)
    with database.get_conn(db_path) as conn:
        rows = database.get_prices(conn, ["AAPL"])
        assert len(rows) == 1
        assert rows[0]["price"] == 100.0, "Price should be rolled back to 100.0"


def test_remove_ticker_cascades_to_price_and_sparkline(conn):
    """Verify that removing a ticker deletes it from watchlist, price_cache, and sparkline_cache."""
    # Add ticker to watchlist
    database.add_watchlist_ticker(conn, "AAPL")

    # Add price cache row
    database.upsert_price(conn, "AAPL", 100.0, 99.0, 1.01, 500, "USD")

    # Add sparkline cache row
    database.upsert_sparkline(conn, "AAPL", [1.0, 2.0, 3.0])

    # Verify all three rows exist
    assert database.list_watchlist(conn) == ["AAPL"]
    assert len(database.get_prices(conn, ["AAPL"])) == 1
    assert database.get_sparkline(conn, "AAPL") == [1.0, 2.0, 3.0]

    # Remove the ticker
    assert database.remove_watchlist_ticker(conn, "AAPL") is True

    # Verify all three rows are deleted
    assert database.list_watchlist(conn) == []
    assert len(database.get_prices(conn, ["AAPL"])) == 0
    assert database.get_sparkline(conn, "AAPL") is None
