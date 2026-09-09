"""OMON — the option chain and what is derived from it.

The maths is arithmetic on quoted prices, so it is tested against a fixed
chain rather than whatever the market is doing.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services import options


def contract(strike, bid=None, ask=None, iv=None, volume=0, oi=0, last=None):
    return {"contract": f"X{strike}", "strike": strike, "last": last,
            "bid": bid, "ask": ask, "change_pct": 0.0, "volume": volume,
            "open_interest": oi, "iv": iv, "in_the_money": False}


CALLS = [contract(90, 12, 13, 0.40, 10, 100),
         contract(100, 5, 6, 0.30, 50, 500),
         contract(110, 1, 2, 0.35, 20, 200)]
# The open interest totals deliberately differ between the two sides (800 vs
# 1200). An earlier version had both summing to 800, which let a mutation that
# reported the call total for both sides pass unnoticed.
PUTS = [contract(90, 1, 2, 0.38, 5, 300),
        contract(100, 4, 5, 0.32, 25, 400),
        contract(110, 11, 12, 0.45, 15, 500)]


# --- at-the-money implied volatility -----------------------------------------

def test_atm_iv_averages_the_call_and_the_put():
    """A single side's quote can be stale or wide; parity says the two should
    price close together."""
    assert options.atm_iv(CALLS, PUTS, 100) == pytest.approx((0.30 + 0.32) / 2)


def test_atm_iv_picks_the_nearest_strike_not_an_exact_match():
    assert options.atm_iv(CALLS, PUTS, 101) == pytest.approx(0.31)


def test_atm_iv_without_a_spot_is_unknown():
    assert options.atm_iv(CALLS, PUTS, None) is None


def test_atm_iv_ignores_a_side_with_no_quote():
    calls = [contract(100, 5, 6, None)]
    assert options.atm_iv(calls, PUTS, 100) == pytest.approx(0.32)


def test_atm_iv_of_an_empty_chain_is_unknown():
    assert options.atm_iv([], [], 100) is None


# --- implied move ------------------------------------------------------------

def test_the_implied_move_is_the_straddle():
    """Call mid 5.5 + put mid 4.5 = 10 on a 100 spot."""
    pct, absolute = options.implied_move(CALLS, PUTS, 100, 7)
    assert absolute == pytest.approx(10.0)
    assert pct == pytest.approx(10.0)


def test_the_implied_move_falls_back_to_the_last_trade():
    """A contract with no two-sided quote still traded, which beats
    discarding the strike."""
    calls = [contract(100, None, None, 0.3, last=5.0)]
    puts = [contract(100, None, None, 0.3, last=4.0)]
    _, absolute = options.implied_move(calls, puts, 100, 7)
    assert absolute == pytest.approx(9.0)


def test_no_move_without_a_spot_or_an_expiry():
    assert options.implied_move(CALLS, PUTS, None, 7) == (None, None)
    assert options.implied_move(CALLS, PUTS, 100, 0) == (None, None)


def test_no_move_when_neither_side_is_priced():
    calls = [contract(100)]
    puts = [contract(100)]
    assert options.implied_move(calls, puts, 100, 7) == (None, None)


# --- max pain ----------------------------------------------------------------

def test_max_pain_is_pulled_down_by_heavy_call_open_interest():
    """1000 calls at 90 against a handful of puts above. Every dollar above 90
    costs the call writers 1000, so the cheapest settlement is 90.

    Two earlier versions of this test passed while the call payout condition
    was inverted, because the fixtures were symmetric enough that the wrong
    answer tied with the right one. This one is deliberately lopsided."""
    calls = [contract(90, oi=1000)]
    puts = [contract(90, oi=10), contract(100, oi=10), contract(110, oi=10)]
    assert options.max_pain(calls, puts) == 90


def test_max_pain_is_pulled_up_by_heavy_put_open_interest():
    """The mirror image, so neither side can be ignored."""
    calls = [contract(90, oi=10), contract(100, oi=10), contract(110, oi=10)]
    puts = [contract(110, oi=1000)]
    assert options.max_pain(calls, puts) == 110


def test_max_pain_of_an_empty_chain_is_unknown():
    assert options.max_pain([], []) is None


# --- the summary -------------------------------------------------------------

def test_volume_and_open_interest_are_totalled_per_side():
    s = options.summarise(CALLS, PUTS, 100, 7)
    assert s.call_volume == 80 and s.put_volume == 45
    assert s.call_open_interest == 800 and s.put_open_interest == 1200


def test_the_put_call_ratio_is_puts_over_calls():
    s = options.summarise(CALLS, PUTS, 100, 7)
    assert s.put_call_volume == pytest.approx(45 / 80, abs=0.001)
    assert s.put_call_open_interest == pytest.approx(1200 / 800, abs=0.001)


def test_a_ratio_without_calls_is_unknown_not_zero():
    """Zero would read as "no put interest" rather than "no data"."""
    s = options.summarise([], PUTS, 100, 7)
    assert s.put_call_volume is None
    assert s.put_call_open_interest is None


def test_the_summary_survives_an_empty_chain():
    s = options.summarise([], [], None, 0)
    assert s.atm_iv is None and s.max_pain is None
    assert s.call_volume == 0


def test_missing_volume_counts_as_zero_not_a_crash():
    rows = [{"strike": 100, "bid": 1, "ask": 2, "iv": 0.3,
             "volume": None, "open_interest": None}]
    s = options.summarise(rows, rows, 100, 7)
    assert s.call_volume == 0


def test_nothing_here_prices_an_option():
    """Pricing needs a rate, a dividend assumption and a volatility view; a
    number built on those guesses would look more authoritative than it is."""
    import inspect

    source = inspect.getsource(options)
    for banned in ("black_scholes", "norm.cdf", "fair_value", "theoretical"):
        assert banned not in source.lower()


# --- the route ---------------------------------------------------------------

class FakeClient:
    def __init__(self, expirations=("2026-09-04",), chain=None):
        self._expirations = list(expirations)
        self._chain = chain or {"calls": CALLS, "puts": PUTS}
        self.chain_calls = []

    def fetch_expirations(self, ticker):
        return self._expirations

    def fetch_option_chain(self, ticker, expiry):
        self.chain_calls.append((ticker, expiry))
        return self._chain

    def fetch_quotes(self, t): return {}
    def fetch_intraday(self, t): return []
    def fetch_news(self, t): return []
    def fetch_fundamentals(self, t): return None
    def fetch_bars(self, t, p, i): return []
    def fetch_earnings_dates(self, t, limit=8): return []
    def validate_ticker(self, t): return True


@pytest.fixture
def client(db_path):
    fake = FakeClient()
    app = create_app(cfg=Config(db_path=db_path), client=fake, start_poller=False)
    with TestClient(app) as c:
        c.fake = fake
        yield c


def test_the_chain_is_served_with_a_summary(client):
    body = client.get("/api/options/AAPL").json()
    assert body["expiry"] == "2026-09-04"
    assert len(body["calls"]) == 3
    assert body["summary"]["call_volume"] == 80


def test_a_name_with_no_options_says_so(db_path):
    app = create_app(cfg=Config(db_path=db_path),
                     client=FakeClient(expirations=()), start_poller=False)
    with TestClient(app) as c:
        body = c.get("/api/options/NOPE").json()
    assert body["calls"] == []
    assert "no listed options" in body["reason"].lower()


def test_an_unknown_expiry_falls_back_to_the_nearest(client):
    body = client.get("/api/options/AAPL?expiry=1999-01-01").json()
    assert body["expiry"] == "2026-09-04"


def test_the_chain_is_cached_briefly(client):
    client.get("/api/options/AAPL")
    client.get("/api/options/AAPL")
    assert len(client.fake.chain_calls) == 1, "the second read refetched"


def test_a_stale_cache_is_refetched(client, db_path):
    """Quotes move; a chain from an hour ago is not an answer."""
    import json
    from datetime import datetime, timedelta, timezone

    client.get("/api/options/AAPL")
    key = "options:AAPL:2026-09-04"
    with database.get_conn(db_path) as conn:
        payload = json.loads(database.get_setting(conn, key))
        payload["as_of"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        database.set_setting(conn, key, json.dumps(payload))

    client.get("/api/options/AAPL")
    assert len(client.fake.chain_calls) == 2


def test_the_spot_comes_from_the_cached_quote(client, db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_price(conn, "AAPL", 100.0, 99.0, 1.0, 1000, "USD")
    body = client.get("/api/options/AAPL").json()
    assert body["summary"]["spot"] == 100.0
    assert body["summary"]["atm_iv"] is not None


def test_no_quote_means_no_derived_summary_rather_than_a_guess(client):
    """Deriving from a mid-strike guess would be worse than saying unknown."""
    body = client.get("/api/options/AAPL").json()
    assert body["summary"]["spot"] is None
    assert body["summary"]["atm_iv"] is None
    assert body["summary"]["implied_move_pct"] is None


def test_expirations_are_listed_for_the_picker(client):
    assert client.get("/api/options/AAPL/expirations").json()["expirations"] \
        == ["2026-09-04"]


def test_omon_is_in_the_function_directory(client):
    body = client.get("/api/functions").json()
    codes = {f["code"] for c in body["categories"] for f in c["functions"]}
    assert "OMON" in codes


# --- the screen --------------------------------------------------------------

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "app.js"


def test_every_selectable_chip_is_a_tab():
    """`aria-selected` is only meaningful on tab/option/row roles. On a bare
    button it styles fine and announces nothing, so the sighted user sees the
    selected expiry and the screen-reader user does not.

    Caught by a Chrome probe, not by reading the source: the CSS attribute
    selector works either way."""
    source = APP_JS.read_text(encoding="utf-8")
    for line_no, line in enumerate(source.splitlines(), 1):
        if "aria-selected" not in line or "setAttribute" in line:
            continue
        window = "\n".join(
            source.splitlines()[max(0, line_no - 6):line_no + 2]
        )
        assert 'role="tab"' in window or 'role="option"' in window, (
            f"app.js:{line_no} sets aria-selected without a role that "
            f"supports it"
        )
