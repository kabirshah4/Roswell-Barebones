import pytest

from backend.data.macro_releases import IMPACT, VALID_TIERS
from backend.services.fred_client import FredClient, MacroEvent


def payload(*entries):
    return {"release_dates": [
        {"release_id": rid, "release_name": name, "date": date}
        for rid, name, date in entries
    ]}


def getter_returning(data):
    calls = []

    def getter(url, params):
        calls.append((url, params))
        return data

    getter.calls = calls
    return getter


def getter_raising(exc):
    def getter(url, params):
        raise exc

    return getter


# --- the curated map itself ---

def test_every_impact_tier_is_valid():
    """A typo here would render an event with an undefined colour."""
    bad = {n: t for n, t in IMPACT.items() if t not in VALID_TIERS}
    assert bad == {}


def test_impact_map_is_not_empty():
    assert len(IMPACT) >= 15


def test_impact_map_covers_the_headline_releases():
    for name in ("Consumer Price Index", "Employment Situation", "Gross Domestic Product"):
        assert IMPACT[name] == "high"


def test_daily_data_feeds_are_excluded():
    """These fire ~daily in FRED and would swamp the calendar; verified live."""
    for name in ("FOMC Press Release", "H.15 Selected Interest Rates", "Commercial Paper"):
        assert name not in IMPACT


# --- the client ---

def test_disabled_client_makes_no_calls():
    getter = getter_returning(payload())
    client = FredClient(http_getter=getter, api_key=None)
    assert client.enabled is False
    assert client.fetch_upcoming() == []
    assert getter.calls == []


def test_enabled_when_key_present():
    assert FredClient(http_getter=getter_returning(payload()), api_key="k").enabled is True


def test_fetch_maps_curated_releases():
    getter = getter_returning(payload((10, "Consumer Price Index", "2099-01-15")))
    events = FredClient(http_getter=getter, api_key="k").fetch_upcoming()
    assert len(events) == 1
    e = events[0]
    assert e.release_name == "Consumer Price Index"
    assert e.impact == "high"
    assert e.event_date == "2099-01-15"
    assert e.release_id == 10


def test_uncurated_releases_are_filtered_out():
    """957 releases a month is not a calendar; only curated ones survive."""
    getter = getter_returning(payload(
        (1, "Consumer Price Index", "2099-01-15"),
        (2, "Coinbase Cryptocurrencies", "2099-01-15"),
    ))
    events = FredClient(http_getter=getter, api_key="k").fetch_upcoming()
    assert [e.release_name for e in events] == ["Consumer Price Index"]


def test_event_id_is_stable_and_unique_per_release_date():
    getter = getter_returning(payload(
        (1, "Consumer Price Index", "2099-01-15"),
        (1, "Consumer Price Index", "2099-02-15"),
    ))
    events = FredClient(http_getter=getter, api_key="k").fetch_upcoming()
    assert len({e.id for e in events}) == 2


def test_api_key_is_sent_but_request_includes_future_window():
    getter = getter_returning(payload())
    FredClient(http_getter=getter, api_key="secret").fetch_upcoming(days=30)
    _, params = getter.calls[0]
    assert params["api_key"] == "secret"
    assert params["include_release_dates_with_no_data"] == "true"
    assert params["realtime_start"] <= params["realtime_end"]


def test_fetch_returns_empty_on_exception():
    client = FredClient(http_getter=getter_raising(RuntimeError("down")), api_key="k")
    assert client.fetch_upcoming() == []


def test_fetch_returns_empty_on_fred_error_payload():
    getter = getter_returning({"error_message": "Bad Request. Invalid api_key"})
    assert FredClient(http_getter=getter, api_key="k").fetch_upcoming() == []


def test_malformed_entry_does_not_abort_the_list():
    getter = getter_returning({"release_dates": [
        "not-a-dict",
        {"release_id": 1, "release_name": "Consumer Price Index", "date": "2099-01-15"},
    ]})
    events = FredClient(http_getter=getter, api_key="k").fetch_upcoming()
    assert [e.release_name for e in events] == ["Consumer Price Index"]


# --- credential-leak regressions -------------------------------------------
# FRED takes the API key as a query parameter, so any log line carrying the
# request URL (ours, or httpx's own INFO-level request logger) exposes it.

_SENTINEL = "SENTINEL_KEY_MUST_NOT_APPEAR_IN_LOGS"


def _http_error_getter(url, params):
    """Raise the way httpx really does.

    `raise_for_status()` builds a message that embeds the full request URL, so
    the key rides in `str(exc)`. Constructing the error with a plain message
    would make this test pass even against a logger that dumps the traceback.
    """
    import httpx

    request = httpx.Request("GET", f"{url}?api_key={_SENTINEL}")
    response = httpx.Response(400, request=request)
    response.raise_for_status()


def test_http_error_does_not_leak_key_into_logs(caplog):
    caplog.set_level("DEBUG")
    client = FredClient(http_getter=_http_error_getter, api_key=_SENTINEL)
    assert client.fetch_upcoming() == []
    assert _SENTINEL not in caplog.text


def test_generic_exception_does_not_leak_key_into_logs(caplog):
    caplog.set_level("DEBUG")

    def boom(url, params):
        raise RuntimeError(f"connection to {url}?api_key={_SENTINEL} failed")

    client = FredClient(http_getter=boom, api_key=_SENTINEL)
    assert client.fetch_upcoming() == []
    assert _SENTINEL not in caplog.text


def test_malformed_entry_does_not_leak_key_into_logs(caplog):
    caplog.set_level("DEBUG")
    client = FredClient(
        http_getter=getter_returning({"release_dates": ["not-a-dict"]}),
        api_key=_SENTINEL,
    )
    assert client.fetch_upcoming() == []
    assert _SENTINEL not in caplog.text


def test_httpx_request_logger_is_silenced():
    """httpx logs the full request URL at INFO; FRED's key rides in that URL."""
    import logging

    import backend.services.fred_client  # noqa: F401  (import applies the setting)

    assert logging.getLogger("httpx").level >= logging.WARNING
