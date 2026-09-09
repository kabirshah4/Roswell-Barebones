"""Signals are served from SQLite; the route never evaluates or delivers."""

import asyncio
import json

from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services.price_poller import PricePoller


class ExplodingClient:
    def fetch_quotes(self, tickers): raise AssertionError("routes must never fetch")
    def fetch_intraday(self, t): raise AssertionError("routes must never fetch")
    def fetch_news(self, t): raise AssertionError("routes must never fetch")
    def fetch_fundamentals(self, t): raise AssertionError("routes must never fetch")
    def fetch_bars(self, t, p, i): raise AssertionError("routes must never fetch")
    def fetch_earnings_dates(self, t, limit=8): raise AssertionError("routes must never fetch")
    def validate_ticker(self, t): return True


class ExplodingDiscord:
    enabled = True

    def send_setup(self, setup, stats=None):
        raise AssertionError("routes must never deliver alerts")


class ExplodingAI:
    enabled = True

    def enrich(self, title, summary):
        raise AssertionError("routes must never call the AI provider")


def make_client(db_path):
    app = create_app(
        cfg=Config(db_path=db_path), client=ExplodingClient(), ai_client=ExplodingAI(),
        fred_client=None, discord_client=ExplodingDiscord(), start_poller=False,
    )
    return TestClient(app)


class Setup:
    def __init__(self, ticker="AAPL", grade="A+", entry=100.0):
        self.ticker = ticker
        self.direction = "long"
        self.grade = grade
        self.score = 6
        # The engine emits dict[str, bool] -- every factor with its verdict,
        # not just the ones that passed. A tuple fixture here is what let a
        # "failed factors shown as confluence" bug reach the browser.
        self.factors = {
            "trend": True, "momentum": True, "macd_cross": False,
            "volume": False, "structure": True, "not_extended": True,
            "multi_timeframe": True,
        }
        self.entry = entry
        self.stop = 95.0
        self.target1 = 110.0
        self.target2 = 120.0
        self.risk_reward = 2.0
        self.timeframes = "1h,4h,1d"
        self.earnings_at = None


def seed(db_path, *setups):
    with database.get_conn(db_path) as conn:
        for i, s in enumerate(setups):
            database.upsert_signal(conn, s, f"fp-{i}")


def test_empty_when_nothing_has_fired(db_path):
    with make_client(db_path) as c:
        assert c.get("/api/signals").json() == {"signals": []}


def test_a_stored_setup_is_served_whole(db_path):
    seed(db_path, Setup())
    with make_client(db_path) as c:
        sig = c.get("/api/signals").json()["signals"][0]
    assert sig["ticker"] == "AAPL"
    assert sig["entry"] == 100.0 and sig["stop"] == 95.0
    assert sig["target1"] == 110.0 and sig["target2"] == 120.0
    assert sig["grade"] == "A+"


def test_only_passing_factors_are_served_as_confluence(db_path):
    """The engine stores every factor with a verdict; failures are not
    confluence, and presenting them as such inflates a 5-of-7 setup."""
    seed(db_path, Setup())
    with make_client(db_path) as c:
        sig = c.get("/api/signals").json()["signals"][0]
    assert sig["factors"] == [
        "momentum", "multi_timeframe", "not_extended", "structure", "trend"]
    assert sig["factors_failed"] == ["macd_cross", "volume"]


def test_factors_come_back_as_a_list_not_a_json_string(db_path):
    seed(db_path, Setup())
    with make_client(db_path) as c:
        assert isinstance(c.get("/api/signals").json()["signals"][0]["factors"], list)


def test_malformed_factors_do_not_break_the_route(db_path):
    seed(db_path, Setup())
    with database.get_conn(db_path) as conn:
        conn.execute("UPDATE signals SET factors_json = 'not json'")
    with make_client(db_path) as c:
        r = c.get("/api/signals")
    assert r.status_code == 200
    assert r.json()["signals"][0]["factors"] == []


def test_a_legacy_list_shape_still_works(db_path):
    """Rows written before factors became a dict must not break the panel."""
    seed(db_path, Setup())
    with database.get_conn(db_path) as conn:
        conn.execute("""UPDATE signals SET factors_json = '["trend"]'""")
    with make_client(db_path) as c:
        sig = c.get("/api/signals").json()["signals"][0]
    assert sig["factors"] == ["trend"]
    assert sig["factors_failed"] == []


def test_the_limit_is_capped(db_path):
    """An uncapped limit lets one request pull the whole table."""
    seed(db_path, *[Setup(entry=100.0 + i) for i in range(20)])
    with make_client(db_path) as c:
        assert len(c.get("/api/signals?limit=100000").json()["signals"]) <= 100


def test_the_limit_is_honoured(db_path):
    seed(db_path, *[Setup(entry=100.0 + i) for i in range(10)])
    with make_client(db_path) as c:
        assert len(c.get("/api/signals?limit=3").json()["signals"]) == 3


def test_a_zero_or_negative_limit_still_returns_something(db_path):
    seed(db_path, Setup())
    with make_client(db_path) as c:
        assert len(c.get("/api/signals?limit=0").json()["signals"]) == 1


def test_health_reports_whether_alerts_are_configured(db_path):
    with make_client(db_path) as c:
        body = c.get("/api/health").json()
    assert body["alerts_enabled"] is True
    assert "signals_found" in body and "alerts_sent" in body


# --- delivery ---------------------------------------------------------------

class RecordingDiscord:
    def __init__(self, ok=True):
        self.enabled = True
        self.sent = []
        self._ok = ok

    def send_setup(self, setup, stats=None):
        self.sent.append((setup, stats))
        return self._ok


def poller_with(db_path, discord, **cfg):
    conf = Config(db_path=db_path, **cfg)
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")

    class Quote:
        price = prev_close = 100.0
        change_pct = 0.0
        volume = 1
        currency = "USD"

    class YF:
        def fetch_quotes(self, ts): return {t: Quote() for t in ts}
        def fetch_intraday(self, t): return []
        def fetch_news(self, t): return []
        def fetch_fundamentals(self, t): return None
        def fetch_bars(self, t, p, i): return []
        def fetch_earnings_dates(self, t, limit=8): return []

    return PricePoller(YF(), db_path, conf, ai_client=None, discord_client=discord)


def test_a_new_setup_is_delivered(db_path):
    seed(db_path, Setup())
    discord = RecordingDiscord()
    asyncio.run(poller_with(db_path, discord).run_once())
    assert len(discord.sent) == 1
    assert discord.sent[0][0].ticker == "AAPL"


def test_a_delivered_setup_is_not_sent_twice(db_path):
    seed(db_path, Setup())
    discord = RecordingDiscord()
    poller = poller_with(db_path, discord)
    asyncio.run(poller.run_once())
    asyncio.run(poller.run_once())
    assert len(discord.sent) == 1
    assert poller.alerts_sent == 1


def test_a_failed_delivery_is_retried(db_path):
    seed(db_path, Setup())
    discord = RecordingDiscord(ok=False)
    poller = poller_with(db_path, discord)
    asyncio.run(poller.run_once())
    asyncio.run(poller.run_once())
    assert len(discord.sent) == 2
    assert poller.alerts_sent == 0


def test_retries_stop_at_the_ceiling(db_path):
    """A permanently bad webhook must not be hammered forever."""
    seed(db_path, Setup())
    discord = RecordingDiscord(ok=False)
    poller = poller_with(db_path, discord)
    for _ in range(6):
        asyncio.run(poller.run_once())
    assert len(discord.sent) == 3


def test_no_webhook_configured_means_no_delivery_attempts(db_path):
    seed(db_path, Setup())

    class Off:
        enabled = False

        def send_setup(self, setup, stats=None):
            raise AssertionError("must not send when disabled")

    asyncio.run(poller_with(db_path, Off()).run_once())


def test_a_raising_client_does_not_fail_the_cycle(db_path):
    seed(db_path, Setup())

    class Boom:
        enabled = True

        def send_setup(self, setup, stats=None):
            raise RuntimeError("discord down")

    assert asyncio.run(poller_with(db_path, Boom()).run_once()) is True


def test_delivery_runs_every_cycle_not_only_on_scan_cycles(db_path):
    """A webhook that was down should not wait for the next 30-minute scan."""
    seed(db_path, Setup())
    discord = RecordingDiscord()
    asyncio.run(poller_with(db_path, discord, signal_every_n_cycles=999).run_once())
    assert len(discord.sent) == 1


def test_the_per_cycle_cap_is_respected(db_path):
    seed(db_path, *[Setup(entry=100.0 + i) for i in range(20)])
    discord = RecordingDiscord()
    asyncio.run(poller_with(db_path, discord, max_alerts_per_cycle=4).run_once())
    assert len(discord.sent) == 4


def test_the_setup_is_rebuilt_with_its_levels_intact(db_path):
    seed(db_path, Setup())
    discord = RecordingDiscord()
    asyncio.run(poller_with(db_path, discord).run_once())
    setup = discord.sent[0][0]
    assert (setup.entry, setup.stop, setup.target1, setup.target2) == (
        100.0, 95.0, 110.0, 120.0)
    assert setup.factors["trend"] is True
    assert setup.factors["volume"] is False


def test_stats_are_none_without_enough_history(db_path):
    """The alert should say the grade is unvalidated rather than invent a rate."""
    seed(db_path, Setup())
    discord = RecordingDiscord()
    asyncio.run(poller_with(db_path, discord).run_once())
    assert discord.sent[0][1] is None


def test_the_rebuilt_setup_keeps_each_factor_verdict(db_path):
    """tuple(dict) keeps only the keys, turning every failed factor into an
    apparent confluence by the time it reaches Discord."""
    seed(db_path, Setup())
    discord = RecordingDiscord()
    asyncio.run(poller_with(db_path, discord).run_once())
    factors = discord.sent[0][0].factors
    assert isinstance(factors, dict)
    assert factors["volume"] is False
    assert sum(1 for v in factors.values() if v) == 5


def test_the_delivered_embed_does_not_overstate_confluence(db_path):
    """End to end: stored row -> rebuilt setup -> rendered Discord embed."""
    from backend.services.discord_client import build_payload

    seed(db_path, Setup())
    discord = RecordingDiscord()
    asyncio.run(poller_with(db_path, discord).run_once())
    fields = build_payload(discord.sent[0][0])["embeds"][0]["fields"]
    conf = next(f for f in fields if f["name"].startswith("Confluence"))
    assert "(5/7)" in conf["name"]
    assert "volume" not in conf["value"].split("\n")[0]
