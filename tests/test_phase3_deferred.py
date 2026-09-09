"""The three items deferred out of Phase 3.

Universe rate-limit backoff, macro staleness reporting, and the screener
preset UI.
"""

import asyncio
import re

from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services.price_poller import PricePoller, _is_rate_limited


class Quote:
    price = prev_close = 100.0
    change_pct = 0.0
    volume = 1
    currency = "USD"


class YF:
    def __init__(self, fundamentals_error=None):
        self._err = fundamentals_error
        self.fundamentals_calls = []

    def fetch_quotes(self, ts): return {t: Quote() for t in ts}
    def fetch_intraday(self, t): return []
    def fetch_news(self, t): return []
    def fetch_bars(self, t, p, i): return []
    def fetch_earnings_dates(self, t, limit=8): return []

    def fetch_fundamentals(self, ticker):
        self.fundamentals_calls.append(ticker)
        if self._err:
            raise self._err
        return None


def poller(db_path, client, **cfg):
    conf = Config(db_path=db_path, **cfg)
    with database.get_conn(db_path) as conn:
        database.seed_universe(conn, ["AAPL", "MSFT", "NVDA", "KO", "WMT"])
    return PricePoller(client, db_path, conf, ai_client=None)


# --- 1. universe rate-limit backoff -----------------------------------------

def test_a_429_is_recognised_by_message():
    assert _is_rate_limited(RuntimeError("429 Too Many Requests")) is True
    assert _is_rate_limited(RuntimeError("Rate limit exceeded")) is True


def test_a_429_is_recognised_by_type():
    """YFRateLimitError takes no argument -- it supplies its own message."""
    from backend.services.yfinance_client import RATE_LIMIT_ERRORS

    assert _is_rate_limited(RATE_LIMIT_ERRORS[0]()) is True


def test_the_typed_check_does_not_rely_on_the_message():
    """yfinance owns that string and can reword it in any release."""

    class Fake(Exception):
        def __str__(self):
            return "no hint here"

    from backend.services import yfinance_client, price_poller

    original = yfinance_client.RATE_LIMIT_ERRORS
    yfinance_client.RATE_LIMIT_ERRORS = (Fake,)
    try:
        assert price_poller._is_rate_limited(Fake()) is True
    finally:
        yfinance_client.RATE_LIMIT_ERRORS = original


def test_an_unrelated_error_is_not_a_rate_limit():
    assert _is_rate_limited(RuntimeError("delisted")) is False


def test_a_rate_limit_pauses_the_universe_warm(db_path):
    """Hammering Yahoo through a block makes the block last longer."""
    client = YF(fundamentals_error=RuntimeError("429 Too Many Requests"))
    p = poller(db_path, client, universe_cooldown_cycles=5)
    asyncio.run(p.run_once())
    assert p.universe_cooldown == 5
    calls = len(client.fundamentals_calls)
    asyncio.run(p.run_once())
    assert len(client.fundamentals_calls) == calls, "must not call while cooling down"


def test_the_cooldown_expires_and_warming_resumes(db_path):
    client = YF(fundamentals_error=RuntimeError("429 Too Many Requests"))
    p = poller(db_path, client, universe_cooldown_cycles=2)
    asyncio.run(p.run_once())          # trips the cooldown
    before = len(client.fundamentals_calls)
    asyncio.run(p.run_once())          # cooling
    asyncio.run(p.run_once())          # cooling
    assert len(client.fundamentals_calls) == before
    asyncio.run(p.run_once())          # resumes
    assert len(client.fundamentals_calls) > before


def test_a_rate_limit_does_not_spend_an_attempt(db_path):
    """Phase 1 shipped this bug in validate_ticker: three unrelated 429s
    permanently retired a perfectly valid ticker."""
    client = YF(fundamentals_error=RuntimeError("429 Too Many Requests"))
    p = poller(db_path, client, universe_cooldown_cycles=0)
    for _ in range(5):
        asyncio.run(p.run_once())
    with database.get_conn(db_path) as conn:
        failed = conn.execute(
            "SELECT COUNT(*) c FROM universe WHERE warm_state = 'failed'"
        ).fetchone()["c"]
    assert failed == 0


def test_an_ordinary_failure_still_spends_an_attempt(db_path):
    """A genuinely dead ticker must not stall the queue forever."""
    client = YF(fundamentals_error=RuntimeError("delisted"))
    p = poller(db_path, client, universe_max_attempts=2)
    for _ in range(6):
        asyncio.run(p.run_once())
    with database.get_conn(db_path) as conn:
        failed = conn.execute(
            "SELECT COUNT(*) c FROM universe WHERE warm_state = 'failed'"
        ).fetchone()["c"]
    assert failed >= 1


# --- 2. macro staleness ------------------------------------------------------

class Fred:
    def __init__(self, events=None, error=None):
        self.enabled = True
        self._events = events or []
        self._error = error

    def fetch_upcoming(self):
        if self._error:
            raise self._error
        return self._events


class Event:
    """A macro release a week out.

    Relative to today, not a fixed date. This was hardcoded to 2026-09-01, and
    `list_macro_events` serves upcoming events only -- so the test passed until
    that date and then failed every day afterwards, for a reason that has
    nothing to do with what it is checking.
    """

    def __init__(self, i, days_out=7):
        from datetime import datetime, timedelta, timezone

        self.id = f"e{i}"
        self.release_id = i
        self.release_name = f"Release {i}"
        self.event_date = (
            datetime.now(timezone.utc) + timedelta(days=days_out)
        ).strftime("%Y-%m-%dT00:00:00Z")
        self.impact = "high"


def macro_poller(db_path, fred):
    conf = Config(db_path=db_path, macro_every_n_cycles=1)
    return PricePoller(YF(), db_path, conf, ai_client=None, fred_client=fred)


def test_a_successful_refresh_is_not_stale(db_path):
    p = macro_poller(db_path, Fred(events=[Event(1)]))
    asyncio.run(p.run_once())
    assert p.macro_stale is False
    assert p.macro_last_success is not None


def test_a_failed_refresh_marks_the_calendar_stale(db_path):
    p = macro_poller(db_path, Fred(error=RuntimeError("FRED down")))
    asyncio.run(p.run_once())
    assert p.macro_stale is True


def test_cached_events_are_still_served_when_stale(db_path):
    """The panel must never blank on a failed refresh."""
    p = macro_poller(db_path, Fred(events=[Event(1)]))
    asyncio.run(p.run_once())
    p.fred_client = Fred(error=RuntimeError("FRED down"))
    asyncio.run(p.run_once())
    with database.get_conn(db_path) as conn:
        assert len(database.list_macro_events(conn)) == 1
    assert p.macro_stale is True


def test_recovery_clears_the_stale_flag(db_path):
    p = macro_poller(db_path, Fred(error=RuntimeError("down")))
    asyncio.run(p.run_once())
    assert p.macro_stale is True
    p.fred_client = Fred(events=[Event(1)])
    asyncio.run(p.run_once())
    assert p.macro_stale is False


def test_the_macro_route_reports_staleness(db_path):
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as c:
        body = c.get("/api/macro/calendar").json()
    assert "is_stale" in body and "last_refresh" in body


# --- 3. screener preset UI ---------------------------------------------------

def assets(db_path):
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as c:
        return c.get("/").text, c.get("/static/app.js").text


def test_the_preset_controls_exist(db_path):
    html, _ = assets(db_path)
    for control in ("preset-select", "preset-save", "preset-delete"):
        assert control in html


def test_the_ui_reaches_every_preset_endpoint(db_path):
    """Phase 3 shipped the routes with no way to call them."""
    _, js = assets(db_path)
    assert "/api/screener/presets" in js
    assert '"POST"' in js and '"DELETE"' in js


def test_market_cap_is_stored_in_api_units_not_the_ui_s_billions(db_path):
    """`market_cap_min` means raw dollars everywhere else; saving billions
    under that name would silently mean a value a billion times too small."""
    _, js = assets(db_path)
    assert "MCAP_SCALE" in js
    save = js[js.index("function currentFilters"):js.index("function applyFilters")]
    assert "* MCAP_SCALE" in save
    load = js[js.index("function applyFilters"):js.index("async function loadPresets")]
    assert "/ MCAP_SCALE" in load


def test_loading_a_preset_clears_fields_it_does_not_set(db_path):
    """A preset that omits a filter means 'no constraint', not 'keep the
    value that happened to be in the box'."""
    _, js = assets(db_path)
    load = js[js.index("function applyFilters"):js.index("async function loadPresets")]
    assert '= ""' in load


def test_preset_names_are_escaped_into_the_dropdown(db_path):
    """Preset names are user input and go straight into innerHTML."""
    _, js = assets(db_path)
    block = js[js.index("async function loadPresets"):]
    assert block.count("escapeHtml(p.name)") >= 2


def test_the_delete_button_is_disabled_with_nothing_selected(db_path):
    _, js = assets(db_path)
    assert 'preset-delete").disabled' in js
