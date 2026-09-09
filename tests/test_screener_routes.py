from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app


class ExplodingClient:
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


class ExplodingFred:
    enabled = True

    def fetch_upcoming(self, days=30):
        raise AssertionError("routes must never call FRED")


def make_client(db_path, fred=None):
    app = create_app(
        cfg=Config(db_path=db_path), client=ExplodingClient(),
        ai_client=None, fred_client=fred if fred is not None else ExplodingFred(),
        start_poller=False,
    )
    return TestClient(app)


def seed_fundamentals(db_path):
    with database.get_conn(db_path) as conn:
        database.seed_universe(conn, ["CHEAP", "RICH"])
        database.upsert_fundamentals(conn, "CHEAP", pe_ratio=8.0, market_cap=1e10,
                                     sector="Energy")
        database.upsert_fundamentals(conn, "RICH", pe_ratio=60.0, market_cap=1e12,
                                     sector="Technology")


def test_screener_filters_and_never_fetches(db_path):
    seed_fundamentals(db_path)
    with make_client(db_path) as c:
        body = c.post("/api/screener", json={"pe_max": 20}).json()
    assert [m["ticker"] for m in body["matches"]] == ["CHEAP"]


def test_screener_reports_coverage(db_path):
    seed_fundamentals(db_path)
    with make_client(db_path) as c:
        cov = c.post("/api/screener", json={}).json()["coverage"]
    assert cov["universe"] == 2
    assert cov["screened"] == 2


def test_screener_empty_body_returns_all(db_path):
    seed_fundamentals(db_path)
    with make_client(db_path) as c:
        assert len(c.post("/api/screener", json={}).json()["matches"]) == 2


def test_screener_rejects_inverted_range_with_400(db_path):
    seed_fundamentals(db_path)
    with make_client(db_path) as c:
        r = c.post("/api/screener", json={"pe_min": 50, "pe_max": 10})
    assert r.status_code == 400


def test_screener_with_no_coverage_says_so(db_path):
    with make_client(db_path) as c:
        body = c.post("/api/screener", json={"pe_max": 20}).json()
    assert body["matches"] == []
    assert body["coverage"]["screened"] == 0


def test_preset_save_list_delete(db_path):
    with make_client(db_path) as c:
        assert c.post("/api/screener/presets",
                      json={"name": "cheap", "filters": {"pe_max": 15}}).status_code == 201
        presets = c.get("/api/screener/presets").json()["presets"]
        assert presets[0]["name"] == "cheap"
        assert c.delete("/api/screener/presets/cheap").status_code == 204
        assert c.get("/api/screener/presets").json()["presets"] == []


def test_delete_missing_preset_404(db_path):
    with make_client(db_path) as c:
        assert c.delete("/api/screener/presets/nope").status_code == 404


def test_macro_calendar_serves_cached_events(db_path):
    with database.get_conn(db_path) as conn:
        database.upsert_macro_event(conn, "1|2099-01-01", 1, "Consumer Price Index",
                                    "2099-01-01", "high")
    with make_client(db_path) as c:
        body = c.get("/api/macro/calendar").json()
    assert body["events"][0]["release_name"] == "Consumer Price Index"
    assert body["events"][0]["impact"] == "high"
    assert body["fred_enabled"] is True


def test_macro_calendar_reports_disabled_fred(db_path):
    class DisabledFred:
        enabled = False

        def fetch_upcoming(self, days=30):
            return []

    with make_client(db_path, fred=DisabledFred()) as c:
        body = c.get("/api/macro/calendar").json()
    assert body["fred_enabled"] is False
    assert body["events"] == []


def test_health_exposes_fred_and_coverage(db_path):
    seed_fundamentals(db_path)
    with make_client(db_path) as c:
        h = c.get("/api/health").json()
    assert h["fred_enabled"] is True
    assert h["universe_coverage"]["universe"] == 2
