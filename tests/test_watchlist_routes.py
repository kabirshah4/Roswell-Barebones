import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app


class FakeClient:
    def __init__(self, valid=("AAPL", "MSFT"), unavailable=()):
        self._valid = set(valid)
        self._unavailable = set(unavailable)

    def fetch_quotes(self, tickers):
        return {}

    def fetch_intraday(self, ticker):
        return []

    def validate_ticker(self, ticker):
        return ticker in self._valid

    def check_ticker(self, ticker):
        if ticker in self._unavailable:
            return "unavailable"
        return "ok" if ticker in self._valid else "unknown"


def make_client(db_path, client=None):
    app = create_app(cfg=Config(db_path=db_path), client=client or FakeClient(), start_poller=False)
    return TestClient(app)


def test_empty_watchlist(db_path):
    with make_client(db_path) as c:
        assert c.get("/api/watchlist").json() == {"tickers": []}


def test_add_ticker(db_path):
    with make_client(db_path) as c:
        r = c.post("/api/watchlist", json={"ticker": "AAPL"})
        assert r.status_code == 201
        assert r.json()["ticker"] == "AAPL"   # `warming` also present; asserted separately
        assert c.get("/api/watchlist").json() == {"tickers": ["AAPL"]}


def test_ticker_is_normalised_to_uppercase(db_path):
    with make_client(db_path) as c:
        assert c.post("/api/watchlist", json={"ticker": "  aapl "}).status_code == 201
        assert c.get("/api/watchlist").json() == {"tickers": ["AAPL"]}


def test_unresolvable_ticker_rejected_with_400(db_path):
    with make_client(db_path) as c:
        r = c.post("/api/watchlist", json={"ticker": "ZZZZZZ"})
        assert r.status_code == 400
        assert "ZZZZZZ" in r.json()["detail"]
        assert c.get("/api/watchlist").json() == {"tickers": []}


def test_upstream_unavailable_rejected_with_503_not_400(db_path):
    """A Yahoo rate-limit or transient outage must not be reported as an
    unknown ticker — the symbol may be perfectly valid."""
    client = FakeClient(valid=("AAPL", "MSFT"), unavailable=("AAPL",))
    with make_client(db_path, client=client) as c:
        r = c.post("/api/watchlist", json={"ticker": "AAPL"})
        assert r.status_code == 503
        assert "AAPL" not in r.json()["detail"] or "unavailable" in r.json()["detail"].lower()
        assert c.get("/api/watchlist").json() == {"tickers": []}


def test_genuinely_unknown_ticker_still_rejected_with_400(db_path):
    client = FakeClient(valid=("AAPL", "MSFT"))
    with make_client(db_path, client=client) as c:
        r = c.post("/api/watchlist", json={"ticker": "ZZZZZZ"})
        assert r.status_code == 400
        assert "ZZZZZZ" in r.json()["detail"]


def test_duplicate_ticker_rejected_with_409(db_path):
    with make_client(db_path) as c:
        c.post("/api/watchlist", json={"ticker": "AAPL"})
        assert c.post("/api/watchlist", json={"ticker": "AAPL"}).status_code == 409


def test_blank_ticker_rejected_with_422(db_path):
    with make_client(db_path) as c:
        assert c.post("/api/watchlist", json={"ticker": "   "}).status_code == 422


@pytest.mark.parametrize(
    "ticker",
    ["<script>", "AAPL;DROP", "AA PL", "AA/PL", "AA'PL", 'AA"PL', "AA\\PL"],
)
def test_ticker_with_invalid_characters_rejected_with_422(db_path, ticker):
    with make_client(db_path) as c:
        assert c.post("/api/watchlist", json={"ticker": ticker}).status_code == 422


def test_ticker_with_dot_and_hyphen_is_accepted(db_path):
    """Real symbols use these: BRK.B (dot), BF-B (hyphen)."""
    client = FakeClient(valid=("BRK.B", "BF-B"))
    with make_client(db_path, client=client) as c:
        assert c.post("/api/watchlist", json={"ticker": "BRK.B"}).status_code == 201
        assert c.post("/api/watchlist", json={"ticker": "BF-B"}).status_code == 201


def test_delete_ticker(db_path):
    with make_client(db_path) as c:
        c.post("/api/watchlist", json={"ticker": "AAPL"})
        assert c.delete("/api/watchlist/AAPL").status_code == 204
        assert c.get("/api/watchlist").json() == {"tickers": []}


def test_delete_ticker_lowercase_path_param_is_normalised(db_path):
    with make_client(db_path) as c:
        c.post("/api/watchlist", json={"ticker": "AAPL"})
        assert c.delete("/api/watchlist/aapl").status_code == 204
        assert c.get("/api/watchlist").json() == {"tickers": []}


def test_delete_ticker_with_whitespace_in_path_param(db_path):
    with make_client(db_path) as c:
        c.post("/api/watchlist", json={"ticker": "AAPL"})
        assert c.delete("/api/watchlist/ aapl ").status_code == 204
        assert c.get("/api/watchlist").json() == {"tickers": []}


def test_delete_missing_ticker_returns_404(db_path):
    with make_client(db_path) as c:
        assert c.delete("/api/watchlist/NOPE").status_code == 404


def test_delete_also_clears_cached_price(db_path):
    with database.get_conn(db_path) as conn:
        database.upsert_price(conn, "AAPL", 1.0, 1.0, 0.0, 1, "USD")
    with make_client(db_path) as c:
        c.post("/api/watchlist", json={"ticker": "AAPL"})
        c.delete("/api/watchlist/AAPL")
    with database.get_conn(db_path) as conn:
        assert database.get_prices(conn) == []


def test_watchlist_persists_across_app_restarts(db_path):
    with make_client(db_path) as c:
        c.post("/api/watchlist", json={"ticker": "AAPL"})
    with make_client(db_path) as c:
        assert c.get("/api/watchlist").json() == {"tickers": ["AAPL"]}


# --- immediate warm on add ---------------------------------------------------
#
# Without this a new ticker showed an empty news panel and no fundamentals
# until the next cadence -- fifteen minutes for news, an hour for fundamentals.
# An empty panel reads as "this ticker has no news", not "we have not looked".

def test_adding_does_not_warm_when_the_poller_is_not_running(db_path):
    """A caller that opted out of background work opted out of the network."""
    with make_client(db_path) as c:
        assert c.post("/api/watchlist", json={"ticker": "AAPL"}).json()["warming"] is False


def test_the_warm_fetches_everything_the_panels_need(db_path):
    """One pass must cover quote, sparkline, news, fundamentals, bars and
    earnings -- a partial warm still leaves a panel looking broken."""
    import asyncio

    from backend.config import Config
    from backend.services.price_poller import PricePoller

    calls = []

    class Quote:
        price = prev_close = 100.0
        change_pct = 0.0
        volume = 1
        currency = "USD"

    class Article:
        id = "n1"
        ticker = "AAPL"
        title = "T"
        publisher = "R"
        url = "https://x.test/1"
        published_at = "2026-08-01T00:00:00Z"
        summary = "s"

    class Fundamentals:
        ticker = "AAPL"
        pe_ratio = 30.0
        forward_pe = market_cap = eps = revenue = None
        sector = industry = None
        dividend_yield = beta = week52_high = week52_low = None

    class Recorder:
        def fetch_quotes(self, t): calls.append("quotes"); return {x: Quote() for x in t}
        def fetch_intraday(self, t): calls.append("sparkline"); return [1.0, 2.0]
        def fetch_news(self, t): calls.append("news"); return [Article()]
        def fetch_fundamentals(self, t): calls.append("fundamentals"); return Fundamentals()
        def fetch_bars(self, t, p, i): calls.append(f"bars:{i}"); return []
        def fetch_earnings_dates(self, t, limit=8): calls.append("earnings"); return []

    poller = PricePoller(Recorder(), db_path, Config(db_path=db_path), ai_client=None)
    asyncio.run(poller.warm_ticker("AAPL"))

    for step in ("quotes", "sparkline", "news", "fundamentals", "earnings"):
        assert step in calls, f"warm never fetched {step}"
    assert any(c.startswith("bars:") for c in calls)

    with database.get_conn(db_path) as conn:
        assert database.list_news(conn, "AAPL")
        assert database.get_fundamentals(conn, "AAPL") is not None


def test_a_failing_step_does_not_abort_the_rest(db_path):
    """One dead endpoint must not leave every other panel empty too."""
    import asyncio

    from backend.config import Config
    from backend.services.price_poller import PricePoller

    class Fundamentals:
        ticker = "AAPL"
        pe_ratio = 12.0
        forward_pe = market_cap = eps = revenue = None
        sector = industry = None
        dividend_yield = beta = week52_high = week52_low = None

    class Broken:
        def fetch_quotes(self, t): raise RuntimeError("quotes down")
        def fetch_intraday(self, t): raise RuntimeError("sparkline down")
        def fetch_news(self, t): raise RuntimeError("news down")
        def fetch_fundamentals(self, t): return Fundamentals()
        def fetch_bars(self, t, p, i): raise RuntimeError("bars down")
        def fetch_earnings_dates(self, t, limit=8): raise RuntimeError("earnings down")

    poller = PricePoller(Broken(), db_path, Config(db_path=db_path), ai_client=None)
    asyncio.run(poller.warm_ticker("AAPL"))
    with database.get_conn(db_path) as conn:
        assert database.get_fundamentals(conn, "AAPL") is not None


def test_the_warming_set_is_cleared_afterwards(db_path):
    """A ticker stuck in `warming` would show 'fetching' forever."""
    import asyncio

    from backend.config import Config
    from backend.services.price_poller import PricePoller

    class Broken:
        def fetch_quotes(self, t): raise RuntimeError("down")
        def fetch_intraday(self, t): raise RuntimeError("down")
        def fetch_news(self, t): raise RuntimeError("down")
        def fetch_fundamentals(self, t): raise RuntimeError("down")
        def fetch_bars(self, t, p, i): raise RuntimeError("down")
        def fetch_earnings_dates(self, t, limit=8): raise RuntimeError("down")

    poller = PricePoller(Broken(), db_path, Config(db_path=db_path), ai_client=None)
    asyncio.run(poller.warm_ticker("AAPL"))
    assert poller.warming == set()


def test_health_reports_what_is_warming(db_path):
    with make_client(db_path) as c:
        assert "warming" in c.get("/api/health").json()


def test_a_step_that_raises_outright_does_not_abort_the_warm(db_path):
    """The individual refresh methods swallow per-ticker failures, so the
    per-step guard is only reachable when a step itself blows up -- a future
    step, or a bug in an existing one. Tested directly, because a fixture
    whose client raises never gets that far."""
    import asyncio

    from backend.config import Config
    from backend.services.price_poller import PricePoller

    class Fundamentals:
        ticker = "AAPL"
        pe_ratio = 42.0
        forward_pe = market_cap = eps = revenue = None
        sector = industry = None
        dividend_yield = beta = week52_high = week52_low = None

    class YF:
        def fetch_quotes(self, t): return {}
        def fetch_intraday(self, t): return []
        def fetch_news(self, t): return []
        def fetch_fundamentals(self, t): return Fundamentals()
        def fetch_bars(self, t, p, i): return []
        def fetch_earnings_dates(self, t, limit=8): return []

    poller = PricePoller(YF(), db_path, Config(db_path=db_path), ai_client=None)

    async def boom(tickers):
        raise RuntimeError("this step is broken")

    # Sparklines run before fundamentals; if the guard is removed, the raise
    # escapes and fundamentals never runs.
    poller._refresh_sparklines = boom

    asyncio.run(poller.warm_ticker("AAPL"))
    with database.get_conn(db_path) as conn:
        assert database.get_fundamentals(conn, "AAPL") is not None, (
            "a broken step aborted the rest of the warm"
        )
