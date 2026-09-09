from backend.db import database


def bar(ts, close=10.0):
    return {"ts": ts, "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": 1000.0}


def test_upsert_and_read_bars(conn):
    written = database.upsert_bars(conn, "AAPL", "1h", [
        bar("2026-01-01T10:00:00Z", 10.0), bar("2026-01-01T11:00:00Z", 11.0)])
    assert written == 2
    rows = database.get_bars(conn, "AAPL", "1h")
    assert [r["close"] for r in rows] == [10.0, 11.0], "must be oldest-first"


def test_bars_are_idempotent_on_reingest(conn):
    database.upsert_bars(conn, "AAPL", "1h", [bar("2026-01-01T10:00:00Z", 10.0)])
    database.upsert_bars(conn, "AAPL", "1h", [bar("2026-01-01T10:00:00Z", 12.0)])
    rows = database.get_bars(conn, "AAPL", "1h")
    assert len(rows) == 1
    assert rows[0]["close"] == 12.0, "a re-fetched bar should correct itself"


def test_bars_are_scoped_by_interval(conn):
    database.upsert_bars(conn, "AAPL", "1h", [bar("2026-01-01T10:00:00Z", 10.0)])
    database.upsert_bars(conn, "AAPL", "1d", [bar("2026-01-01T10:00:00Z", 99.0)])
    assert database.get_bars(conn, "AAPL", "1h")[0]["close"] == 10.0
    assert database.get_bars(conn, "AAPL", "1d")[0]["close"] == 99.0


def test_bars_are_scoped_by_ticker(conn):
    database.upsert_bars(conn, "AAPL", "1h", [bar("2026-01-01T10:00:00Z", 10.0)])
    database.upsert_bars(conn, "MSFT", "1h", [bar("2026-01-01T10:00:00Z", 20.0)])
    assert len(database.get_bars(conn, "AAPL", "1h")) == 1


def test_get_bars_limit_returns_the_most_recent(conn):
    database.upsert_bars(conn, "AAPL", "1h", [
        bar(f"2026-01-01T{h:02d}:00:00Z", float(h)) for h in range(10, 20)])
    rows = database.get_bars(conn, "AAPL", "1h", limit=3)
    assert [r["close"] for r in rows] == [17.0, 18.0, 19.0]


def test_get_bars_empty_returns_empty_list(conn):
    assert database.get_bars(conn, "NOPE", "1h") == []


def test_earnings_roundtrip_and_next_lookup(conn):
    database.upsert_earnings(conn, "AAPL", "2026-10-29T20:00:00Z", 1.98)
    database.upsert_earnings(conn, "AAPL", "2026-07-30T20:00:00Z", 1.89)
    nxt = database.next_earnings(conn, "AAPL", "2026-08-23T00:00:00Z")
    assert nxt == "2026-10-29T20:00:00Z", "must pick the soonest FUTURE date"


def test_next_earnings_none_when_all_past(conn):
    database.upsert_earnings(conn, "AAPL", "2020-01-01T00:00:00Z", 1.0)
    assert database.next_earnings(conn, "AAPL", "2026-08-23T00:00:00Z") is None


def test_next_earnings_none_for_unknown_ticker(conn):
    assert database.next_earnings(conn, "NOPE", "2026-08-23T00:00:00Z") is None


def test_earnings_upsert_replaces_estimate(conn):
    database.upsert_earnings(conn, "AAPL", "2026-10-29T20:00:00Z", 1.90)
    database.upsert_earnings(conn, "AAPL", "2026-10-29T20:00:00Z", 2.05)
    nxt = database.next_earnings(conn, "AAPL", "2026-01-01T00:00:00Z")
    assert nxt == "2026-10-29T20:00:00Z"
    row = conn.execute("SELECT eps_estimate FROM earnings WHERE ticker='AAPL'").fetchone()
    assert row["eps_estimate"] == 2.05
