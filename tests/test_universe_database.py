from backend.db import database


def test_seed_universe_is_idempotent(conn):
    assert database.seed_universe(conn, ["AAPL", "MSFT"]) == 2
    assert database.seed_universe(conn, ["AAPL", "MSFT"]) == 0
    assert database.seed_universe(conn, ["AAPL", "NVDA"]) == 1


def test_seed_universe_does_not_reset_warm_state(conn):
    """Re-seeding on every startup must not undo warming work already done."""
    database.seed_universe(conn, ["AAPL"])
    database.mark_universe_done(conn, "AAPL")
    database.seed_universe(conn, ["AAPL"])
    assert database.pending_universe(conn, 10) == []


def test_pending_universe_respects_limit_and_state(conn):
    database.seed_universe(conn, ["A", "B", "C"])
    database.mark_universe_done(conn, "A")
    pending = database.pending_universe(conn, 2)
    assert len(pending) == 2
    assert "A" not in pending


def test_failed_tickers_are_not_retried(conn):
    database.seed_universe(conn, ["BAD"])
    database.mark_universe_failed(conn, "BAD")
    assert database.pending_universe(conn, 10) == []


def test_bump_universe_attempts_increments(conn):
    database.seed_universe(conn, ["AAPL"])
    assert database.bump_universe_attempts(conn, "AAPL") == 1
    assert database.bump_universe_attempts(conn, "AAPL") == 2


def test_coverage_counts_only_rows_with_fundamentals(conn):
    """Coverage must reflect screenable rows, not merely 'done' flags."""
    database.seed_universe(conn, ["AAPL", "MSFT", "NVDA"])
    database.upsert_fundamentals(conn, "AAPL", pe_ratio=30.0)
    cov = database.universe_coverage(conn)
    assert cov["universe"] == 3
    assert cov["screened"] == 1
    assert cov["warming"] is True


def test_coverage_reports_not_warming_when_complete(conn):
    database.seed_universe(conn, ["AAPL"])
    database.upsert_fundamentals(conn, "AAPL", pe_ratio=30.0)
    database.mark_universe_done(conn, "AAPL")
    assert database.universe_coverage(conn)["warming"] is False


def test_macro_event_roundtrip_and_ordering(conn):
    database.upsert_macro_event(conn, "1|2099-01-02", 1, "CPI", "2099-01-02", "high")
    database.upsert_macro_event(conn, "2|2099-01-01", 2, "FOMC", "2099-01-01", "high")
    names = [e["release_name"] for e in database.list_macro_events(conn)]
    assert names == ["FOMC", "CPI"]


def test_macro_events_exclude_past_dates(conn):
    database.upsert_macro_event(conn, "old", 1, "Old", "2000-01-01", "high")
    database.upsert_macro_event(conn, "new", 2, "New", "2099-01-01", "high")
    assert [e["release_name"] for e in database.list_macro_events(conn)] == ["New"]


def test_macro_event_upsert_replaces(conn):
    database.upsert_macro_event(conn, "x", 1, "CPI", "2099-01-01", "low")
    database.upsert_macro_event(conn, "x", 1, "CPI", "2099-01-01", "high")
    events = database.list_macro_events(conn)
    assert len(events) == 1
    assert events[0]["impact"] == "high"


def test_preset_roundtrip(conn):
    database.save_preset(conn, "cheap", '{"pe_max": 15}')
    presets = database.list_presets(conn)
    assert presets[0]["name"] == "cheap"
    assert presets[0]["filter_json"] == '{"pe_max": 15}'


def test_preset_save_overwrites_same_name(conn):
    database.save_preset(conn, "p", '{"pe_max": 15}')
    database.save_preset(conn, "p", '{"pe_max": 20}')
    presets = database.list_presets(conn)
    assert len(presets) == 1
    assert presets[0]["filter_json"] == '{"pe_max": 20}'


def test_delete_preset(conn):
    database.save_preset(conn, "p", "{}")
    assert database.delete_preset(conn, "p") is True
    assert database.delete_preset(conn, "p") is False
