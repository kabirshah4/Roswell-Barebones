"""WB — the Treasury curve.

The shape is the only reason to open this screen, so the tests care about the
curve arriving intact, the spreads being computed rather than left for the
reader to subtract, and a missing maturity not taking the rest with it.
"""

import json

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services.fred_client import FredClient


def observations(*values):
    return {"observations": [{"date": d, "value": v} for d, v in values]}


class FakeFred:
    """Answers the observations endpoint from a table, and records what it was
    asked for."""

    def __init__(self, table=None, fail=()):
        self.table = table or {}
        self.fail = set(fail)
        self.asked = []

    def __call__(self, url, params):
        series = params["series_id"]
        self.asked.append(series)
        if series in self.fail:
            raise RuntimeError("FRED is down")
        return self.table.get(series, observations())


FULL = {
    sid: observations(("2026-08-27", str(rate)), ("2026-07-28", str(rate - 0.05)))
    for sid, rate in [
        ("DGS1MO", 3.81), ("DGS3MO", 3.84), ("DGS6MO", 3.94), ("DGS1", 4.04),
        ("DGS2", 4.20), ("DGS5", 4.38), ("DGS7", 4.52), ("DGS10", 4.67),
        ("DGS20", 5.18), ("DGS30", 5.19),
    ]
}


# --- the client --------------------------------------------------------------

def test_the_whole_curve_comes_back_short_end_first():
    """Ordered by maturity, not alphabetically — the shape is the point, and
    "DGS1, DGS10, DGS1MO" is a sort rather than a curve."""
    points = FredClient(FakeFred(FULL), api_key="k").fetch_yield_curve()
    assert [p.label for p in points] == [
        "1M", "3M", "6M", "1Y", "2Y", "5Y", "7Y", "10Y", "20Y", "30Y"
    ]


def test_a_maturity_that_fails_does_not_take_the_curve_with_it():
    """A curve missing its 7-year is still worth reading; a blank screen is
    not."""
    points = FredClient(
        FakeFred(FULL, fail={"DGS7"}), api_key="k"
    ).fetch_yield_curve()
    assert len(points) == 9
    assert "7Y" not in [p.label for p in points]


def test_a_maturity_with_no_prints_is_skipped():
    table = {**FULL, "DGS20": observations()}
    points = FredClient(FakeFred(table), api_key="k").fetch_yield_curve()
    assert "20Y" not in [p.label for p in points]


def test_closed_market_placeholders_are_not_read_as_prices():
    """FRED writes "." for a day the market was shut. Parsed as a number it
    would raise; treated as zero it would draw a cliff in the curve."""
    table = {**FULL, "DGS10": observations(
        ("2026-08-28", "."), ("2026-08-27", "4.67"), ("2026-07-28", "4.68"))}
    points = FredClient(FakeFred(table), api_key="k").fetch_yield_curve()
    ten = next(p for p in points if p.label == "10Y")
    assert ten.percent == 4.67
    assert ten.observed == "2026-08-27"


def test_a_month_ago_is_carried_for_the_change_column():
    points = FredClient(FakeFred(FULL), api_key="k").fetch_yield_curve()
    assert all(p.month_ago is not None for p in points)


def test_a_series_with_one_print_has_no_comparison(monkeypatch):
    """Better than comparing it with itself and reporting a flat zero."""
    table = {**FULL, "DGS30": observations(("2026-08-27", "5.19"))}
    points = FredClient(FakeFred(table), api_key="k").fetch_yield_curve()
    assert next(p for p in points if p.label == "30Y").month_ago is None


def test_without_a_key_it_asks_for_nothing():
    fake = FakeFred(FULL)
    assert FredClient(fake, api_key="").fetch_yield_curve() == []
    assert fake.asked == [], "a disabled client must not call out"


def test_the_key_is_never_logged(caplog):
    """FRED takes the key as a query parameter, so anything that logs a URL
    leaks it."""
    import logging

    with caplog.at_level(logging.DEBUG):
        FredClient(FakeFred(FULL, fail={"DGS1"}), api_key="hunter2-secret") \
            .fetch_yield_curve()
    assert "hunter2-secret" not in caplog.text


# --- the route ---------------------------------------------------------------

class ExplodingClient:
    def fetch_quotes(self, t): raise AssertionError("routes must never fetch")
    def fetch_intraday(self, t): raise AssertionError("routes must never fetch")
    def fetch_news(self, t): raise AssertionError("routes must never fetch")
    def fetch_fundamentals(self, t): raise AssertionError("routes must never fetch")
    def fetch_bars(self, t, p, i): raise AssertionError("routes must never fetch")
    def fetch_earnings_dates(self, t, limit=8): raise AssertionError("routes must never fetch")
    def validate_ticker(self, t): return True


@pytest.fixture
def client(db_path):
    app = create_app(cfg=Config(db_path=db_path), client=ExplodingClient(),
                     start_poller=False)
    with TestClient(app) as c:
        yield c


def cache(db_path, points):
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, "yield_curve", json.dumps(
            {"points": points, "as_of": "2026-08-28T00:00:00+00:00"}))


def curve(**overrides):
    base = {"1M": 3.81, "3M": 3.84, "2Y": 4.20, "10Y": 4.67, "30Y": 5.19}
    base.update(overrides)
    return [{"series_id": f"X{k}", "label": k, "percent": v,
             "observed": "2026-08-27", "month_ago": v - 0.05}
            for k, v in base.items()]


def test_an_empty_cache_explains_itself(client):
    body = client.get("/api/macro/yields").json()
    assert body["points"] == []
    assert "FRED" in body["reason"]


def test_the_cached_curve_is_served(client, db_path):
    cache(db_path, curve())
    body = client.get("/api/macro/yields").json()
    assert len(body["points"]) == 5
    assert body["reason"] is None


def test_the_route_never_fetches(client, db_path):
    cache(db_path, curve())
    assert client.get("/api/macro/yields").status_code == 200


def test_the_quoted_spreads_are_computed(client, db_path):
    """Inversion is why anyone opens this screen. Leaving the reader to
    subtract two rows in their head is how it gets missed."""
    cache(db_path, curve())
    body = client.get("/api/macro/yields").json()
    assert body["spread_10y_2y"] == pytest.approx(0.47)
    assert body["spread_10y_3m"] == pytest.approx(0.83)


def test_an_inverted_curve_reports_a_negative_spread(client, db_path):
    cache(db_path, curve(**{"2Y": 5.00}))
    assert client.get("/api/macro/yields").json()["spread_10y_2y"] < 0


def test_a_spread_missing_a_leg_is_unknown_not_zero(client, db_path):
    """Zero would read as "flat", which is a real and different signal."""
    cache(db_path, [p for p in curve() if p["label"] != "2Y"])
    body = client.get("/api/macro/yields").json()
    assert body["spread_10y_2y"] is None
    assert body["spread_10y_3m"] is not None


def test_an_unreadable_cache_does_not_500(client, db_path):
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, "yield_curve", "{not json")
    body = client.get("/api/macro/yields").json()
    assert body["points"] == [] and body["reason"]


def test_wb_is_in_the_function_directory(client):
    body = client.get("/api/functions").json()
    codes = {f["code"] for c in body["categories"] for f in c["functions"]}
    assert "WB" in codes
