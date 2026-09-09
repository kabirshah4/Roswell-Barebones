"""The screener-sourced symbol catalogue."""

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services.screener_client import (
    Symbol,
    ScreenerClient,
    to_yahoo_symbol,
)


def row(tv, name, exchange="NASDAQ", kind="stock", cap=1e9):
    return {"s": tv, "d": [tv.split(":")[-1], name, exchange, kind, cap]}


def poster(payload):
    def f(url, body):
        return payload

    return f


# --- symbol conversion -------------------------------------------------------

def test_the_exchange_prefix_is_stripped():
    assert to_yahoo_symbol("NASDAQ:AAPL") == "AAPL"


def test_a_share_class_dot_becomes_a_dash():
    """Yahoo returns a quote with no market cap for BRK.B rather than failing,
    so the wrong form slips through unnoticed."""
    assert to_yahoo_symbol("NYSE:BRK.B") == "BRK-B"


def test_a_plain_symbol_is_unchanged():
    assert to_yahoo_symbol("AAPL") == "AAPL"


def test_conversion_is_case_normalising():
    assert to_yahoo_symbol("nasdaq:aapl") == "AAPL"


# --- fetching ----------------------------------------------------------------

def fetched(client, kinds=("stock",)):
    """Everything the client returned except the curated index list.

    Indices are not fetched -- a stock screener returns nothing
    for type=index -- so they are always present and would mask what the
    fetch actually produced.
    """
    return [s for s in client.fetch_symbols(kinds=kinds) if s.kind != "index"]


def test_symbols_are_parsed():
    client = ScreenerClient(poster=poster({"data": [row("NASDAQ:AAPL", "Apple Inc.")]}))
    got = fetched(client)
    assert got[0].symbol == "AAPL"
    assert got[0].name == "Apple Inc."
    assert got[0].source_symbol == "NASDAQ:AAPL"


def test_a_network_failure_returns_empty_never_raises():
    def boom(url, body):
        raise RuntimeError("403")

    assert fetched(ScreenerClient(poster=boom), kinds=("stock", "fund")) == []


def test_one_malformed_row_does_not_discard_the_rest():
    payload = {"data": [{"broken": True}, row("NASDAQ:AAPL", "Apple Inc.")]}
    got = fetched(ScreenerClient(poster=poster(payload)))
    assert [s.symbol for s in got] == ["AAPL"]


def test_duplicate_yahoo_symbols_collapse():
    """Several screener symbols can map to one Yahoo symbol."""
    payload = {"data": [row("NASDAQ:AAPL", "Apple"), row("NYSE:AAPL", "Apple dup")]}
    got = fetched(ScreenerClient(poster=poster(payload)))
    assert len(got) == 1


def test_an_empty_response_is_not_a_crash():
    assert fetched(ScreenerClient(poster=poster({})), kinds=("stock",)) == []


def test_the_curated_indices_are_always_present():
    """The america scanner returns nothing for type=index, so these are the
    only way ^GSPC and friends reach the catalogue."""
    got = ScreenerClient(poster=poster({})).fetch_symbols(kinds=())
    assert {s.symbol for s in got} >= {"^GSPC", "^DJI", "^IXIC"}
    assert all(s.kind == "index" for s in got)


def test_the_fetcher_does_not_disguise_itself():
    """No spoofed browser User-Agent: the client identifies itself honestly."""
    from backend.services import screener_client

    assert "User-Agent" not in screener_client._HEADERS
    assert screener_client._HEADERS["Content-Type"] == "application/json"


def test_disabled_without_a_configured_endpoint(monkeypatch):
    monkeypatch.delenv("SCREENER_SCAN_URL", raising=False)
    assert ScreenerClient(poster=poster({})).enabled is False


def test_enabled_once_an_endpoint_is_configured(monkeypatch):
    monkeypatch.setenv("SCREENER_SCAN_URL", "https://screener.example/scan")
    assert ScreenerClient(poster=poster({})).enabled is True


def test_unconfigured_fetch_yields_only_the_curated_indices(monkeypatch):
    """No endpoint means no network call -- but the indices are hardcoded.

    They are curated rather than fetched, so search still finds ^GSPC and the
    other nine with no screener configured at all.
    """
    monkeypatch.delenv("SCREENER_SCAN_URL", raising=False)

    def explode(url, payload):
        raise AssertionError("must not call out with no endpoint configured")

    got = ScreenerClient(poster=explode).fetch_symbols(kinds=("stock", "forex"))
    assert got
    assert all(s.kind == "index" for s in got)


def test_market_is_substituted_into_the_url_template(monkeypatch):
    from backend.services.screener_client import scanner_url

    monkeypatch.setenv("SCREENER_SCAN_URL", "https://s.example/{market}/scan")
    assert scanner_url("america") == "https://s.example/america/scan"

    monkeypatch.setenv("SCREENER_SCAN_URL", "https://s.example/scan")
    assert scanner_url("america") == "https://s.example/scan"


# --- storage and search ------------------------------------------------------

def seed(conn, *pairs):
    symbols = [
        Symbol(symbol=s, source_symbol=f"NASDAQ:{s}", name=n, exchange="NASDAQ",
               kind="stock", market_cap=cap)
        for s, n, cap in pairs
    ]
    return database.replace_symbols(conn, symbols)


def test_the_catalogue_is_replaced_not_merged(conn):
    """A delisted name should disappear, not linger forever."""
    seed(conn, ("OLD", "Old Co", 1e9))
    seed(conn, ("NEW", "New Co", 1e9))
    assert database.symbol_count(conn) == 1
    assert database.get_symbol(conn, "OLD") is None


def test_an_empty_fetch_does_not_wipe_the_catalogue(conn):
    """A failed refresh must not leave the user with nothing to search."""
    seed(conn, ("AAPL", "Apple", 1e12))
    assert database.replace_symbols(conn, []) == 0
    assert database.symbol_count(conn) == 1


def test_an_exact_ticker_match_ranks_first(conn):
    seed(conn, ("AA", "Alcoa", 1e9), ("AAPL", "Apple Inc.", 3e12))
    assert database.search_symbols(conn, "AA")[0]["symbol"] == "AA"


def test_a_prefix_match_beats_a_name_match(conn):
    seed(conn, ("ZZZZ", "AAPL Holdings", 9e12), ("AAPL", "Apple Inc.", 1e9))
    assert database.search_symbols(conn, "AAPL")[0]["symbol"] == "AAPL"


def test_larger_companies_rank_first_within_a_tier(conn):
    seed(conn, ("AAA", "Tiny", 1e6), ("AAB", "Huge", 1e12))
    assert database.search_symbols(conn, "AA")[0]["symbol"] == "AAB"


def test_search_matches_company_names(conn):
    seed(conn, ("MSFT", "Microsoft Corporation", 3e12))
    assert database.search_symbols(conn, "microsoft")[0]["symbol"] == "MSFT"


def test_search_is_case_insensitive(conn):
    seed(conn, ("AAPL", "Apple Inc.", 3e12))
    assert database.search_symbols(conn, "aapl")
    assert database.search_symbols(conn, "AaPl")


def test_an_empty_query_returns_nothing(conn):
    seed(conn, ("AAPL", "Apple", 3e12))
    assert database.search_symbols(conn, "") == []
    assert database.search_symbols(conn, "   ") == []


def test_the_result_limit_is_capped(conn):
    seed(conn, *[(f"A{i:03d}", f"Co {i}", 1e9) for i in range(200)])
    assert len(database.search_symbols(conn, "A", limit=10_000)) <= 100


# --- routes ------------------------------------------------------------------

class ExplodingScreener:
    enabled = True

    def fetch_symbols(self, kinds=None):
        raise AssertionError("read routes must never fetch the catalogue")


class ExplodingClient:
    def fetch_quotes(self, t): raise AssertionError("routes must never fetch")
    def fetch_intraday(self, t): raise AssertionError("routes must never fetch")
    def fetch_news(self, t): raise AssertionError("routes must never fetch")
    def fetch_fundamentals(self, t): raise AssertionError("routes must never fetch")
    def validate_ticker(self, t): return True


def client(db_path, tv=None):
    return TestClient(create_app(
        cfg=Config(db_path=db_path), client=ExplodingClient(),
        screener_client=tv or ExplodingScreener(), start_poller=False))


def test_search_route_serves_the_cache_without_fetching(db_path):
    with database.get_conn(db_path) as conn:
        seed(conn, ("AAPL", "Apple Inc.", 3e12))
    with client(db_path) as c:
        body = c.get("/api/symbols/search?q=AAPL").json()
    assert body["results"][0]["symbol"] == "AAPL"


def test_search_with_no_query_is_not_an_error(db_path):
    with client(db_path) as c:
        r = c.get("/api/symbols/search")
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_the_refresh_route_stores_what_it_pulls(db_path):
    class FakeTV:
        enabled = True

        def fetch_symbols(self, kinds=None):
            return [Symbol("AAPL", "NASDAQ:AAPL", "Apple", "NASDAQ", "stock", 3e12)]

    with client(db_path, FakeTV()) as c:
        body = c.post("/api/symbols/refresh").json()
    assert body == {"count": 1, "refreshed": True}


def test_a_failed_refresh_keeps_the_old_catalogue(db_path):
    with database.get_conn(db_path) as conn:
        seed(conn, ("AAPL", "Apple", 3e12))

    class DeadTV:
        enabled = True

        def fetch_symbols(self, kinds=None):
            return []

    with client(db_path, DeadTV()) as c:
        body = c.post("/api/symbols/refresh").json()
    assert body["refreshed"] is False
    assert body["count"] == 1


def test_health_reports_the_catalogue_size(db_path):
    with client(db_path) as c:
        assert "symbols_cached" in c.get("/api/health").json()



# --- asset classes -----------------------------------------------------------
#
# Only classes that map to a symbol yfinance can quote are offered. A CRYPTO
# tab that adds an unpriceable ticker is worse than no tab.

def test_crypto_maps_to_the_yfinance_pair_form():
    payload = {"data": [{"s": "COINBASE:BTCUSD",
                         "d": ["BTCUSD", "BTC", "USD", "Bitcoin / US Dollar", 1e12]}]}
    got = fetched(ScreenerClient(poster=poster(payload)), kinds=("crypto",))
    assert got[0].symbol == "BTC-USD"
    assert got[0].kind == "crypto"


def test_crypto_without_a_currency_pair_is_dropped():
    payload = {"data": [{"s": "X:WEIRD", "d": ["WEIRD", None, None, "?", None]}]}
    assert fetched(ScreenerClient(poster=poster(payload)), kinds=("crypto",)) == []


def test_forex_maps_to_the_yfinance_pair_form():
    payload = {"data": [{"s": "FX_IDC:EURUSD", "d": ["EURUSD", "EURO / US DOLLAR"]}]}
    got = fetched(ScreenerClient(poster=poster(payload)), kinds=("forex",))
    assert got[0].symbol == "EURUSD=X"


def test_a_non_six_letter_forex_row_is_dropped():
    """Anything else does not map to yfinance's XXXYYY=X form."""
    payload = {"data": [{"s": "OANDA:AUDJPY.PRO.OTMS",
                         "d": ["AUDJPY.PRO.OTMS", "odd"]}]}
    assert fetched(ScreenerClient(poster=poster(payload)), kinds=("forex",)) == []


def test_futures_are_not_offered():
    """53,264 rows of EUREX:EAIFJ2027 that yfinance cannot price."""
    from backend.services.screener_client import _MARKETS

    assert "futures" not in _MARKETS


def test_each_class_uses_its_own_scanner_market():
    from backend.services.screener_client import _MARKETS

    assert _MARKETS["crypto"]["market"] == "crypto"
    assert _MARKETS["forex"]["market"] == "forex"
    assert _MARKETS["stock"]["market"] == "america"


def test_search_can_be_filtered_by_kind(conn):
    from backend.services.screener_client import Symbol

    database.replace_symbols(conn, [
        Symbol("BTC-USD", "C:BTCUSD", "Bitcoin", "CRYPTO", "crypto", 1e12),
        Symbol("BTU", "NYSE:BTU", "Peabody Energy", "NYSE", "stock", 1e9),
    ])
    assert [r["symbol"] for r in database.search_symbols(conn, "BT", kind="crypto")] \
        == ["BTC-USD"]
    assert [r["symbol"] for r in database.search_symbols(conn, "BT", kind="stock")] \
        == ["BTU"]


def test_the_search_route_passes_the_kind_through(db_path):
    from backend.services.screener_client import Symbol

    with database.get_conn(db_path) as conn:
        database.replace_symbols(conn, [
            Symbol("BTC-USD", "C:BTCUSD", "Bitcoin", "CRYPTO", "crypto", 1e12),
            Symbol("BTU", "NYSE:BTU", "Peabody", "NYSE", "stock", 1e9),
        ])
    with client(db_path) as c:
        results = c.get("/api/symbols/search?q=BT&kind=crypto").json()["results"]
    assert [r["symbol"] for r in results] == ["BTC-USD"]


def test_major_pairs_lead_the_forex_list(conn):
    """Forex has no market cap, so without an explicit rank the first row is
    AEDAUD -- a FOREX tab nobody would use."""
    from backend.services.screener_client import MAJOR_PAIRS, _parse_row

    rows = [
        {"s": f"FX_IDC:{p}", "d": [p, p]}
        for p in ("AEDAUD", "EURUSD", "ZZZAAA", "USDJPY")
    ]
    database.replace_symbols(conn, [_parse_row(r, "forex") for r in rows])
    order = [r["symbol"] for r in database.browse_symbols(conn, kind="forex")]
    assert order[:2] == ["EURUSD=X", "USDJPY=X"]
    assert MAJOR_PAIRS[0] == "EURUSD"


def test_indices_keep_their_curated_order(conn):
    from backend.services.screener_client import ScreenerClient

    def poster_empty(url, body):
        return {}

    database.replace_symbols(
        conn, ScreenerClient(poster=poster_empty).fetch_symbols(kinds=()))
    assert [r["symbol"] for r in database.browse_symbols(conn, kind="index")][:3] \
        == ["^GSPC", "^DJI", "^IXIC"]


def test_rank_does_not_override_size_for_equities(conn):
    """Stocks carry rank 0; market cap must still decide."""
    from backend.services.screener_client import Symbol

    database.replace_symbols(conn, [
        Symbol("SMALL", "N:SMALL", "Small", "NASDAQ", "stock", 1e6),
        Symbol("BIG", "N:BIG", "Big", "NASDAQ", "stock", 1e12),
    ])
    assert [r["symbol"] for r in database.browse_symbols(conn, kind="stock")] \
        == ["BIG", "SMALL"]
