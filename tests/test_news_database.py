from backend.db import database


def test_upsert_news_inserts_and_reports_new(conn):
    is_new = database.upsert_news_article(
        conn, "a1", "AAPL", "Title", "Reuters", "http://x", "2026-08-10T00:00:00Z", "sum"
    )
    assert is_new is True
    rows = database.list_news(conn, "AAPL")
    assert len(rows) == 1
    assert rows[0]["title"] == "Title"
    assert rows[0]["enrich_state"] == "pending"


def test_upsert_news_second_time_reports_not_new(conn):
    database.upsert_news_article(conn, "a1", "AAPL", "T", "R", "u", "2026-08-10T00:00:00Z", "s")
    assert database.upsert_news_article(
        conn, "a1", "AAPL", "T", "R", "u", "2026-08-10T00:00:00Z", "s"
    ) is False


def test_upsert_news_never_clobbers_enrichment(conn):
    """Re-fetching the same article must not wipe an AI summary we already paid for."""
    database.upsert_news_article(conn, "a1", "AAPL", "T", "R", "u", "2026-08-10T00:00:00Z", "s")
    database.set_enrichment(conn, "a1", "One-liner.", "bullish")
    database.upsert_news_article(conn, "a1", "AAPL", "T", "R", "u", "2026-08-10T00:00:00Z", "s")
    row = database.list_news(conn, "AAPL")[0]
    assert row["ai_summary"] == "One-liner."
    assert row["sentiment"] == "bullish"
    assert row["enrich_state"] == "done"


def test_list_news_is_newest_first(conn):
    database.upsert_news_article(conn, "old", "AAPL", "Old", "R", "u", "2026-08-01T00:00:00Z", "s")
    database.upsert_news_article(conn, "new", "AAPL", "New", "R", "u", "2026-08-09T00:00:00Z", "s")
    assert [r["title"] for r in database.list_news(conn, "AAPL")] == ["New", "Old"]


def test_list_news_filters_by_ticker(conn):
    database.upsert_news_article(conn, "a", "AAPL", "A", "R", "u", "2026-08-01T00:00:00Z", "s")
    database.upsert_news_article(conn, "m", "MSFT", "M", "R", "u", "2026-08-01T00:00:00Z", "s")
    assert [r["title"] for r in database.list_news(conn, "AAPL")] == ["A"]


def test_pending_enrichments_respects_limit_and_state(conn):
    for i in range(5):
        database.upsert_news_article(
            conn, f"a{i}", "AAPL", "T", "R", "u", "2026-08-01T00:00:00Z", "s"
        )
    database.set_enrichment(conn, "a0", "done", "neutral")
    pending = database.pending_enrichments(conn, limit=2)
    assert len(pending) == 2
    assert all(p["id"] != "a0" for p in pending)


def test_mark_enrich_failed_after_attempts(conn):
    database.upsert_news_article(conn, "a1", "AAPL", "T", "R", "u", "2026-08-01T00:00:00Z", "s")
    database.mark_enrich_failed(conn, "a1")
    assert database.pending_enrichments(conn, limit=10) == []


def test_skipped_can_be_reset_to_pending(conn):
    """Adding an API key later must pick up articles skipped while it was absent."""
    database.upsert_news_article(conn, "a1", "AAPL", "T", "R", "u", "2026-08-01T00:00:00Z", "s")
    database.mark_enrich_skipped(conn, "a1")
    assert database.pending_enrichments(conn, limit=10) == []
    assert database.reset_skipped_to_pending(conn) == 1
    assert len(database.pending_enrichments(conn, limit=10)) == 1


def test_reset_skipped_leaves_failed_alone(conn):
    database.upsert_news_article(conn, "f", "AAPL", "T", "R", "u", "2026-08-01T00:00:00Z", "s")
    database.mark_enrich_failed(conn, "f")
    assert database.reset_skipped_to_pending(conn) == 0
    assert database.pending_enrichments(conn, limit=10) == []


def test_fundamentals_roundtrip(conn):
    database.upsert_fundamentals(
        conn, "AAPL", pe_ratio=35.9, forward_pe=32.4, market_cap=4.5e12,
        eps=8.57, revenue=4.6e11, sector="Technology", industry="Consumer Electronics",
        dividend_yield=0.35, beta=1.086, week52_high=344.57, week52_low=223.78,
    )
    f = database.get_fundamentals(conn, "AAPL")
    assert f["pe_ratio"] == 35.9
    assert f["sector"] == "Technology"
    assert f["is_stale"] == 0


def test_fundamentals_missing_returns_none(conn):
    assert database.get_fundamentals(conn, "NOPE") is None


def test_fundamentals_upsert_replaces(conn):
    database.upsert_fundamentals(conn, "AAPL", pe_ratio=10.0)
    database.upsert_fundamentals(conn, "AAPL", pe_ratio=20.0)
    assert database.get_fundamentals(conn, "AAPL")["pe_ratio"] == 20.0
