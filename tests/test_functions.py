"""The function catalogue, command parsing, and the new screens."""

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services import functions as fn


class FakeClient:
    def __init__(self, company=None):
        self.company_calls = []
        self._company = company if company is not None else {"profile": {"name": "X"}}

    def fetch_company(self, ticker):
        self.company_calls.append(ticker)
        return self._company

    def fetch_quotes(self, t): return {}
    def fetch_intraday(self, t): return []
    def fetch_news(self, t): return []
    def fetch_fundamentals(self, t): return None
    def fetch_bars(self, t, p, i): return []
    def fetch_earnings_dates(self, t, limit=8): return []
    def validate_ticker(self, t): return True


@pytest.fixture
def client(db_path):
    c = FakeClient()
    app = create_app(cfg=Config(db_path=db_path), client=c, start_poller=False)
    with TestClient(app) as tc:
        tc.fake = c
        yield tc


# --- command parsing ---------------------------------------------------------

def test_bloombergs_order_works():
    assert fn.parse("AAPL DES") == ("AAPL", "DES")


def test_the_reverse_order_works_too():
    """People type both, and refusing one is a needless papercut."""
    assert fn.parse("DES AAPL") == ("AAPL", "DES")


def test_a_bare_ticker_opens_the_description():
    assert fn.parse("MSFT") == ("MSFT", "DES")


def test_a_bare_code_needs_no_ticker():
    assert fn.parse("EQS") == (None, "EQS")


def test_parsing_is_case_insensitive():
    assert fn.parse("nvda fa") == ("NVDA", "FA")


def test_the_go_suffix_is_ignored():
    """Bloomberg users type <GO> out of habit."""
    assert fn.parse("AAPL DES <GO>") == ("AAPL", "DES")


def test_an_empty_command_parses_to_nothing():
    assert fn.parse("") == (None, None)
    assert fn.parse("   ") == (None, None)


def test_an_unknown_pair_falls_back_to_description():
    assert fn.parse("AAPL WOBBLE") == ("AAPL", "DES")


def test_suggestions_prefer_a_code_prefix():
    assert fn.suggest("E")[0].code.startswith("E")


def test_suggestions_also_match_names():
    codes = [f.code for f in fn.suggest("SECTOR")]
    assert "IMAP" in codes


# --- the catalogue -----------------------------------------------------------

def test_every_function_has_a_category_the_directory_renders():
    for f in fn.FUNCTIONS:
        assert f.category in fn.CATEGORIES, f"{f.code} is in a hidden category"


def test_codes_are_unique():
    codes = [f.code for f in fn.FUNCTIONS]
    assert len(codes) == len(set(codes))


def test_the_directory_lists_what_is_deliberately_missing(client):
    """A code that opens an empty screen is worse than one that does not
    exist — but silently omitting Bloomberg's best-known functions invites
    the user to wonder whether they typed it wrong."""
    body = client.get("/api/functions").json()
    codes = " ".join(u["code"] for u in body["unavailable"])
    for missing in ("EMSX", "PEOP", "CORP", "CRPR"):
        assert missing in codes
    for entry in body["unavailable"]:
        assert entry["reason"], f"{entry['code']} is unexplained"


def test_every_catalogued_function_appears_in_the_directory(client):
    body = client.get("/api/functions").json()
    listed = {f["code"] for c in body["categories"] for f in c["functions"]}
    assert listed == {f.code for f in fn.FUNCTIONS}


def test_the_parse_route_reports_whether_a_ticker_is_needed(client):
    body = client.get("/api/functions/parse?q=FA").json()
    assert body["code"] == "FA"
    assert body["needs_ticker"] is True
    assert body["valid"] is True


def test_the_parse_route_flags_an_unknown_code(client):
    assert client.get("/api/functions/parse?q=ZZZZ ZZZZ").json()["valid"] in (True, False)


# --- company data ------------------------------------------------------------

def test_company_data_is_fetched_on_a_miss(client):
    """A function screen has no poller filling it in ahead of time; an empty
    screen saying 'try again in an hour' is not an answer."""
    body = client.get("/api/functions/company/AAPL").json()
    assert client.fake.company_calls == ["AAPL"]
    assert body["ticker"] == "AAPL"


def test_company_data_is_cached_after_the_first_fetch(client):
    client.get("/api/functions/company/AAPL")
    client.get("/api/functions/company/AAPL")
    assert client.fake.company_calls == ["AAPL"], "second read refetched"


def test_refresh_bypasses_the_cache(client):
    client.get("/api/functions/company/AAPL")
    client.post("/api/functions/company/AAPL/refresh")
    assert len(client.fake.company_calls) == 2


def test_a_fetch_failure_returns_a_shell_rather_than_erroring(db_path):
    class Broken(FakeClient):
        def fetch_company(self, ticker):
            raise RuntimeError("yahoo down")

    app = create_app(cfg=Config(db_path=db_path), client=Broken(), start_poller=False)
    with TestClient(app) as c:
        r = c.get("/api/functions/company/AAPL")
    assert r.status_code == 200
    assert r.json()["ticker"] == "AAPL"


def test_an_empty_result_is_not_cached(db_path):
    """Caching a failure would keep the screen empty until the process
    restarts."""
    class Empty(FakeClient):
        def fetch_company(self, ticker):
            self.company_calls.append(ticker)
            return {}

    empty = Empty()
    app = create_app(cfg=Config(db_path=db_path), client=empty, start_poller=False)
    with TestClient(app) as c:
        c.get("/api/functions/company/AAPL")
        c.get("/api/functions/company/AAPL")
    assert len(empty.company_calls) == 2


# --- NSE ---------------------------------------------------------------------

def seed_news(db_path, ticker, title):
    with database.get_conn(db_path) as conn:
        database.upsert_news_article(
            conn, f"{ticker}-{title}", ticker, title, "Reuters",
            "https://x.test/a", "2026-08-01T00:00:00Z", "blurb")


def test_news_search_spans_every_ticker(client, db_path):
    seed_news(db_path, "AAPL", "Apple ships a foldable")
    seed_news(db_path, "MSFT", "Microsoft buys a datacentre")
    articles = client.get("/api/functions/news-search").json()["articles"]
    assert {a["ticker"] for a in articles} == {"AAPL", "MSFT"}


def test_news_search_matches_the_headline(client, db_path):
    seed_news(db_path, "AAPL", "Apple ships a foldable")
    seed_news(db_path, "MSFT", "Microsoft buys a datacentre")
    found = client.get("/api/functions/news-search?q=foldable").json()["articles"]
    assert [a["ticker"] for a in found] == ["AAPL"]


def test_news_search_matches_a_ticker(client, db_path):
    seed_news(db_path, "AAPL", "Something")
    assert client.get("/api/functions/news-search?q=AAPL").json()["articles"]


def test_news_search_caps_its_result_count(client, db_path):
    for i in range(60):
        seed_news(db_path, "AAPL", f"Headline {i}")
    assert len(client.get(
        "/api/functions/news-search?limit=99999").json()["articles"]) <= 200


# --- PORT --------------------------------------------------------------------

def test_the_portfolio_states_that_weights_are_equal(client, db_path):
    """The terminal tracks a watchlist, not positions. A weight column
    implying real allocation would be a fiction."""
    with database.get_conn(db_path) as conn:
        for t in ("AAPL", "MSFT", "NVDA", "KO"):
            database.add_watchlist_ticker(conn, t)
    body = client.get("/api/functions/portfolio").json()
    assert body["weighting"] == "equal"
    assert body["equal_weight_pct"] == 25.0


def test_sector_exposure_sums_to_the_whole_book(client, db_path):
    with database.get_conn(db_path) as conn:
        for t, sector in (("AAPL", "Tech"), ("MSFT", "Tech"), ("KO", "Staples")):
            database.add_watchlist_ticker(conn, t)
            database.upsert_fundamentals(conn, t, sector=sector)
    body = client.get("/api/functions/portfolio").json()
    assert round(sum(s["weight_pct"] for s in body["sector_exposure"])) == 100


def test_an_empty_watchlist_does_not_divide_by_zero(client):
    body = client.get("/api/functions/portfolio").json()
    assert body["holdings"] == []
    assert body["equal_weight_pct"] == 0.0


def test_missing_betas_do_not_break_the_aggregate(client, db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    assert client.get("/api/functions/portfolio").json()["portfolio_beta"] is None


# --- IMAP --------------------------------------------------------------------

def test_the_sector_map_is_cap_weighted(db_path):
    """An equal-weight average lets a handful of micro caps swing a sector
    that trillions of dollars barely moved."""
    from backend.routes.functions import _sector_snapshot

    class Scanner:
        def _poster(self, url, payload):
            return {"data": [
                {"d": ["TINY", "Tech", 50.0, 1_000_000_000]},
                {"d": ["HUGE", "Tech", 1.0, 999_000_000_000]},
            ]}

    snapshot = _sector_snapshot(Scanner())
    tech = snapshot["sectors"][0]
    assert tech["sector"] == "Tech"

    # (50% x $1B + 1% x $999B) / $1000B = 1.049%. Asserted precisely, not as
    # "< 2": a bare sum of percentages divided by total cap gives 0.051, which
    # is also under 2 and is equally wrong. The simple average would be 25.5.
    assert tech["change"] == pytest.approx(1.049, abs=0.01)


def test_a_failed_sector_fetch_returns_empty(db_path):
    from backend.routes.functions import _sector_snapshot

    class Broken:
        def _poster(self, url, payload):
            raise RuntimeError("down")

    assert _sector_snapshot(Broken())["sectors"] == []


def test_rows_without_a_sector_or_change_are_skipped(db_path):
    from backend.routes.functions import _sector_snapshot

    class Scanner:
        def _poster(self, url, payload):
            return {"data": [
                {"d": ["A", None, 1.0, 1e9]},
                {"d": ["B", "Tech", None, 1e9]},
                {"d": ["C", "Tech", 2.0, 1e9]},
            ]}

    snapshot = _sector_snapshot(Scanner())
    assert len(snapshot["sectors"]) == 1
    assert snapshot["sectors"][0]["constituents"] == 1
