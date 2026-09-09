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

    def fetch_news(self, ticker):
        raise AssertionError("routes must never fetch")

    def fetch_fundamentals(self, ticker):
        raise AssertionError("routes must never fetch")

    def check_ticker(self, ticker):
        return "ok"

    def validate_ticker(self, ticker):
        return True


class ExplodingAI:
    enabled = True

    def enrich(self, title, summary):
        raise AssertionError("routes must never call the AI provider")


def make_client(db_path, ai=None):
    app = create_app(
        cfg=Config(db_path=db_path), client=ExplodingClient(),
        ai_client=ai if ai is not None else ExplodingAI(), start_poller=False,
    )
    return TestClient(app)


def seed_article(db_path, article_id="a1", ticker="AAPL", enriched=False):
    with database.get_conn(db_path) as conn:
        if ticker not in database.list_watchlist(conn):
            database.add_watchlist_ticker(conn, ticker)
        database.upsert_news_article(
            conn, article_id, ticker, "Headline", "Reuters",
            "http://x", "2026-08-10T00:00:00Z", "Blurb",
        )
        if enriched:
            database.set_enrichment(conn, article_id, "A one-liner.", "bullish")


def test_news_returns_cached_articles(db_path):
    seed_article(db_path, enriched=True)
    with make_client(db_path) as c:
        body = c.get("/api/news/AAPL").json()
    assert body["ticker"] == "AAPL"
    article = body["articles"][0]
    assert article["title"] == "Headline"
    assert article["ai_summary"] == "A one-liner."
    assert article["sentiment"] == "bullish"


def test_news_reads_never_hit_the_network(db_path):
    seed_article(db_path)
    with make_client(db_path) as c:
        assert c.get("/api/news/AAPL").status_code == 200


def test_unenriched_article_has_null_ai_fields(db_path):
    seed_article(db_path, enriched=False)
    with make_client(db_path) as c:
        article = c.get("/api/news/AAPL").json()["articles"][0]
    assert article["ai_summary"] is None
    assert article["sentiment"] is None


def test_news_empty_for_watchlist_ticker_with_no_articles(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    with make_client(db_path) as c:
        assert c.get("/api/news/AAPL").json()["articles"] == []


def test_news_404_for_unwatched_ticker(db_path):
    with make_client(db_path) as c:
        assert c.get("/api/news/NOPE").status_code == 404


def test_news_ticker_is_normalised(db_path):
    seed_article(db_path)
    with make_client(db_path) as c:
        assert c.get("/api/news/aapl").status_code == 200


def test_fundamentals_returns_cached_row(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_fundamentals(conn, "AAPL", pe_ratio=35.9, sector="Technology")
    with make_client(db_path) as c:
        body = c.get("/api/fundamentals/AAPL").json()
    assert body["fundamentals"]["pe_ratio"] == 35.9
    assert body["fundamentals"]["sector"] == "Technology"


def test_fundamentals_null_before_first_fetch(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    with make_client(db_path) as c:
        assert c.get("/api/fundamentals/AAPL").json()["fundamentals"] is None


def test_fundamentals_404_for_unwatched_ticker(db_path):
    with make_client(db_path) as c:
        assert c.get("/api/fundamentals/NOPE").status_code == 404


def test_health_reports_ai_enabled_true(db_path):
    with make_client(db_path) as c:
        assert c.get("/api/health").json()["ai_enabled"] is True


def test_health_reports_ai_enabled_false(db_path):
    class DisabledAI:
        enabled = False

        def enrich(self, title, summary):
            return None

    with make_client(db_path, ai=DisabledAI()) as c:
        assert c.get("/api/health").json()["ai_enabled"] is False
