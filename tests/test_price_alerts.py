"""User-defined price alerts and the settings drawer."""

import asyncio

from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.routes.settings import mask
from backend.services.price_poller import PricePoller

SECRET = "https://discord.com/api/webhooks/123456/SUPERSECRETTOKEN"


class Quote:
    def __init__(self, price):
        self.price = price
        self.prev_close = price
        self.change_pct = 0.0
        self.volume = 1
        self.currency = "USD"


class YF:
    def __init__(self, price=100.0):
        self.price = price

    def fetch_quotes(self, tickers): return {t: Quote(self.price) for t in tickers}
    def fetch_intraday(self, t): return []
    def fetch_news(self, t): return []
    def fetch_fundamentals(self, t): return None
    def fetch_bars(self, t, p, i): return []
    def fetch_earnings_dates(self, t, limit=8): return []
    def validate_ticker(self, t): return True


class RecordingDiscord:
    def __init__(self, ok=True, enabled=True):
        self.enabled = enabled
        self.sent = []
        self._ok = ok

    def send_message(self, title, description, long_side=True):
        self.sent.append((title, description, long_side))
        return self._ok

    def send_setup(self, setup, stats=None):
        return True

    def set_webhook_url(self, url):
        self._url = url
        self.enabled = bool(url)


def poller(db_path, yf, discord=None):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    return PricePoller(yf, db_path, Config(db_path=db_path), ai_client=None,
                       discord_client=discord)


def client(db_path, discord=None):
    return TestClient(create_app(
        cfg=Config(db_path=db_path), client=YF(), ai_client=None,
        discord_client=discord or RecordingDiscord(enabled=False),
        start_poller=False))


# --- creating ----------------------------------------------------------------

def test_an_alert_can_be_created(db_path):
    with client(db_path) as c:
        r = c.post("/api/alerts", json={"ticker": "aapl", "direction": "above",
                                        "price": 250})
    assert r.status_code == 201
    assert r.json()["ticker"] == "AAPL"


def test_an_unknown_direction_is_rejected(db_path):
    with client(db_path) as c:
        assert c.post("/api/alerts", json={"ticker": "AAPL", "direction": "sideways",
                                           "price": 1}).status_code == 422


def test_a_non_positive_price_is_rejected(db_path):
    with client(db_path) as c:
        for price in (0, -5):
            assert c.post("/api/alerts", json={"ticker": "AAPL", "direction": "above",
                                               "price": price}).status_code == 422


def test_alerts_are_listed_and_deletable(db_path):
    with client(db_path) as c:
        alert_id = c.post("/api/alerts", json={"ticker": "AAPL", "direction": "below",
                                               "price": 10}).json()["id"]
        assert len(c.get("/api/alerts").json()["alerts"]) == 1
        assert c.delete(f"/api/alerts/{alert_id}").status_code == 204
        assert c.get("/api/alerts").json()["alerts"] == []


def test_deleting_a_missing_alert_is_a_404(db_path):
    with client(db_path) as c:
        assert c.delete("/api/alerts/999").status_code == 404


# --- firing ------------------------------------------------------------------

def test_an_above_alert_fires_when_the_price_reaches_it(db_path):
    p = poller(db_path, YF(price=150.0))
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "above", 120.0)
    asyncio.run(p.run_once())
    with database.get_conn(db_path) as conn:
        assert database.list_price_alerts(conn)[0]["triggered_at"] is not None
    assert p.price_alerts_fired == 1


def test_an_above_alert_stays_armed_below_the_threshold(db_path):
    p = poller(db_path, YF(price=100.0))
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "above", 120.0)
    asyncio.run(p.run_once())
    with database.get_conn(db_path) as conn:
        assert database.list_price_alerts(conn)[0]["triggered_at"] is None


def test_a_below_alert_fires_when_the_price_drops_to_it(db_path):
    p = poller(db_path, YF(price=90.0))
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "below", 95.0)
    asyncio.run(p.run_once())
    assert p.price_alerts_fired == 1


def test_a_below_alert_does_not_fire_above_its_threshold(db_path):
    p = poller(db_path, YF(price=200.0))
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "below", 95.0)
    asyncio.run(p.run_once())
    assert p.price_alerts_fired == 0


def test_the_threshold_itself_counts_as_a_hit(db_path):
    p = poller(db_path, YF(price=120.0))
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "above", 120.0)
    asyncio.run(p.run_once())
    assert p.price_alerts_fired == 1


def test_an_alert_fires_once_not_on_every_cycle(db_path):
    """A price oscillating around the threshold would otherwise spam."""
    p = poller(db_path, YF(price=150.0))
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "above", 120.0)
    for _ in range(4):
        asyncio.run(p.run_once())
    assert p.price_alerts_fired == 1


def test_an_alert_on_an_unwatched_ticker_does_not_fire(db_path):
    """Nothing quotes it, so there is no price to compare against."""
    p = poller(db_path, YF(price=150.0))
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "TSLA", "above", 1.0)
    asyncio.run(p.run_once())
    assert p.price_alerts_fired == 0


# --- delivery ----------------------------------------------------------------

def test_a_triggered_alert_is_pushed(db_path):
    discord = RecordingDiscord()
    p = poller(db_path, YF(price=150.0), discord)
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "above", 120.0, "breakout")
    asyncio.run(p.run_once())
    assert len(discord.sent) == 1
    title, body, above = discord.sent[0]
    assert "AAPL" in title and "above" in title
    assert "breakout" in body
    assert above is True


def test_a_delivered_alert_is_not_pushed_twice(db_path):
    discord = RecordingDiscord()
    p = poller(db_path, YF(price=150.0), discord)
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "above", 120.0)
    asyncio.run(p.run_once())
    asyncio.run(p.run_once())
    assert len(discord.sent) == 1


def test_a_failed_push_is_retried_then_abandoned(db_path):
    discord = RecordingDiscord(ok=False)
    p = poller(db_path, YF(price=150.0), discord)
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "above", 120.0)
    for _ in range(6):
        asyncio.run(p.run_once())
    assert len(discord.sent) == 3


def test_alerts_still_fire_without_a_webhook(db_path):
    """The panel is the primary surface; Discord is the optional extra."""
    p = poller(db_path, YF(price=150.0), RecordingDiscord(enabled=False))
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "above", 120.0)
    asyncio.run(p.run_once())
    assert p.price_alerts_fired == 1


def test_a_raising_discord_does_not_fail_the_cycle(db_path):
    class Boom:
        enabled = True

        def send_message(self, *a, **k):
            raise RuntimeError("down")

        def send_setup(self, *a, **k):
            return True

    p = poller(db_path, YF(price=150.0), Boom())
    with database.get_conn(db_path) as conn:
        database.add_price_alert(conn, "AAPL", "above", 120.0)
    assert asyncio.run(p.run_once()) is True


# --- settings ----------------------------------------------------------------

def test_the_webhook_is_never_returned_in_full(db_path):
    """It is a credential: anyone holding the URL can post to the channel."""
    discord = RecordingDiscord()
    with client(db_path, discord) as c:
        c.put("/api/settings/discord-webhook", json={"url": SECRET})
        body = c.get("/api/settings").json()
    assert "SUPERSECRETTOKEN" not in str(body)
    assert body["discord_webhook_configured"] is True


def test_the_mask_shows_enough_to_recognise_not_to_use():
    hint = mask(SECRET)
    assert "SUPERSECRETTOKEN" not in hint
    assert hint.startswith("…/")


def test_masking_nothing_is_none():
    assert mask(None) is None
    assert mask("") is None


def test_a_non_discord_url_is_rejected(db_path):
    """Otherwise the user's setups get posted to whatever host they pasted."""
    with client(db_path) as c:
        for bad in ("https://evil.example/hook", "not-a-url", "http://discord.com/x"):
            assert c.put("/api/settings/discord-webhook",
                         json={"url": bad}).status_code == 422


def test_saving_applies_immediately_without_a_restart(db_path):
    discord = RecordingDiscord(enabled=False)
    with client(db_path, discord) as c:
        c.put("/api/settings/discord-webhook", json={"url": SECRET})
    assert discord.enabled is True


def test_clearing_falls_back_to_the_environment(db_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", SECRET)
    discord = RecordingDiscord()
    with client(db_path, discord) as c:
        c.put("/api/settings/discord-webhook", json={"url": SECRET})
        assert c.delete("/api/settings/discord-webhook").status_code == 204
    assert discord.enabled is True


def test_the_source_is_reported_so_a_save_cannot_look_like_a_no_op(db_path):
    """An env-var webhook cannot be edited from the browser; say so."""
    with client(db_path, RecordingDiscord()) as c:
        assert c.get("/api/settings").json()["discord_webhook_source"] == "environment"


def test_unset_is_reported_as_unset(db_path):
    with client(db_path, RecordingDiscord(enabled=False)) as c:
        assert c.get("/api/settings").json()["discord_webhook_source"] == "unset"


def test_a_saved_webhook_survives_a_restart(db_path):
    discord = RecordingDiscord(enabled=False)
    with client(db_path, discord) as c:
        c.put("/api/settings/discord-webhook", json={"url": SECRET})
    fresh = RecordingDiscord(enabled=False)
    with client(db_path, fresh):
        pass
    assert fresh.enabled is True, "stored webhook was not applied on startup"


def test_re_marking_a_triggered_alert_does_not_move_its_timestamp(conn):
    """The poller pre-filters to armed alerts, so this guard is belt and
    braces — but "when did it fire" must survive any caller that does not."""
    import time

    alert_id = database.add_price_alert(conn, "AAPL", "above", 100.0)
    database.mark_alert_triggered(conn, alert_id)
    first = database.list_price_alerts(conn)[0]["triggered_at"]

    time.sleep(0.01)
    database.mark_alert_triggered(conn, alert_id)
    assert database.list_price_alerts(conn)[0]["triggered_at"] == first


def test_only_armed_alerts_are_offered_for_checking(conn):
    armed = database.add_price_alert(conn, "AAPL", "above", 100.0)
    fired = database.add_price_alert(conn, "MSFT", "below", 50.0)
    database.mark_alert_triggered(conn, fired)
    assert [a["id"] for a in database.armed_price_alerts(conn)] == [armed]
