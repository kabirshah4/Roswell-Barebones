"""The positions API: what is held, and what it added up to."""

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app


class ExplodingClient:
    def fetch_quotes(self, tickers): raise AssertionError("routes must never fetch")
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


def opened(client, **overrides):
    body = {"ticker": "AAPL", "shares": 10, "entry_price": 100.0,
            "stop": 95.0, "target1": 110.0, "plan_grade": "B"}
    body.update(overrides)
    return client.post("/api/positions", json=body)


# --- opening and closing -----------------------------------------------------

def test_a_position_can_be_opened_and_read_back(client):
    assert opened(client).status_code == 201
    body = client.get("/api/positions").json()
    assert len(body["open"]) == 1
    assert body["open"][0]["ticker"] == "AAPL"


def test_the_route_never_fetches(client):
    opened(client)
    assert client.get("/api/positions").status_code == 200


def test_a_longs_stop_must_sit_below_its_entry(client):
    """Above it, the "stop" is a target and every R in the journal is wrong."""
    r = opened(client, stop=105.0)
    assert r.status_code == 400
    assert "below" in r.json()["detail"]


def test_a_shorts_stop_must_sit_above_its_entry(client):
    assert opened(client, direction="short", stop=95.0).status_code == 400
    assert opened(client, direction="short", stop=105.0).status_code == 201


def test_a_position_without_a_stop_is_allowed(client):
    """Not every trade has one. It simply has no R, and the journal skips it
    rather than inventing one."""
    assert opened(client, stop=None).status_code == 201


def test_closing_records_the_result(client):
    opened(client)
    client.post("/api/positions/1/close",
                json={"exit_price": 110.0, "exit_reason": "target"})
    body = client.get("/api/positions").json()
    assert body["open"] == []
    assert body["closed"][0]["realised_r"] == pytest.approx(2.0)
    assert body["closed"][0]["pnl"] == pytest.approx(100.0)


def test_a_position_cannot_be_closed_twice(client):
    """A second exit price would silently overwrite the result the journal
    already measured."""
    opened(client)
    client.post("/api/positions/1/close", json={"exit_price": 110.0})
    assert client.post(
        "/api/positions/1/close", json={"exit_price": 50.0}
    ).status_code == 404
    assert client.get("/api/positions").json()["closed"][0]["exit_price"] == 110.0


def test_closing_something_that_does_not_exist_is_a_404(client):
    assert client.post(
        "/api/positions/999/close", json={"exit_price": 1.0}
    ).status_code == 404


def test_fees_accumulate_across_entry_and_exit(client):
    opened(client, fees=1.0)
    client.post("/api/positions/1/close", json={"exit_price": 110.0, "fees": 2.0})
    assert client.get("/api/positions").json()["closed"][0]["pnl"] == pytest.approx(97.0)


# --- adjusting ---------------------------------------------------------------

def test_a_stop_can_be_moved(client):
    opened(client)
    assert client.patch("/api/positions/1", json={"stop": 98.0}).status_code == 200
    assert client.get("/api/positions").json()["open"][0]["stop"] == 98.0


def test_the_entry_price_cannot_be_rewritten(client):
    """Changing it would rewrite history the journal already measured."""
    opened(client)
    client.patch("/api/positions/1", json={"stop": 98.0, "entry_price": 1.0})
    assert client.get("/api/positions").json()["open"][0]["entry_price"] == 100.0


def test_a_closed_position_cannot_be_adjusted(client):
    opened(client)
    client.post("/api/positions/1/close", json={"exit_price": 110.0})
    assert client.patch("/api/positions/1", json={"stop": 98.0}).status_code == 404


def test_a_position_can_be_deleted(client):
    opened(client)
    assert client.delete("/api/positions/1").status_code == 204
    assert client.get("/api/positions").json()["open"] == []


# --- the aggregate -----------------------------------------------------------

def test_open_risk_is_what_you_lose_if_every_stop_is_hit(client):
    """The number that decides whether to take one more."""
    opened(client, shares=10, entry_price=100.0, stop=95.0)   # 50 at risk
    opened(client, ticker="NVDA", shares=4, entry_price=200.0, stop=190.0)  # 40
    body = client.get("/api/positions").json()
    assert body["open_risk"] == pytest.approx(90.0)


def test_open_risk_is_also_a_percentage_of_the_account(client):
    client.put("/api/settings/account", json={"account_size": 10_000, "risk_pct": 1})
    opened(client, shares=10, entry_price=100.0, stop=95.0)
    assert client.get("/api/positions").json()["open_risk_pct"] == pytest.approx(0.5)


def test_exposure_needs_a_quote(client):
    """Marking to market without a price would mean marking to the entry,
    which reports every open position as flat."""
    opened(client)
    body = client.get("/api/positions").json()
    assert body["open"][0]["last_price"] is None
    assert body["open"][0]["open_pnl"] is None


def test_exposure_uses_the_cached_quote_when_there_is_one(client, db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_price(conn, "AAPL", 107.0, 100.0, 7.0, 1000, "USD")
    opened(client)
    body = client.get("/api/positions").json()
    assert body["open"][0]["open_pnl"] == pytest.approx(70.0)
    assert body["exposure"] == pytest.approx(1070.0)


def test_open_positions_sort_before_closed(client):
    """The ones needing a decision go on top."""
    opened(client)
    opened(client, ticker="NVDA")
    client.post("/api/positions/1/close", json={"exit_price": 110.0})
    body = client.get("/api/positions").json()
    assert [p["ticker"] for p in body["open"]] == ["NVDA"]
    assert [p["ticker"] for p in body["closed"]] == ["AAPL"]


# --- sizing over the wire ----------------------------------------------------

def test_sizing_uses_the_stored_account_by_default(client):
    client.put("/api/settings/account", json={"account_size": 25_000, "risk_pct": 1})
    body = client.get("/api/positions/size?entry=100&stop=97.5").json()
    assert body["shares"] == 100


def test_sizing_can_be_overridden_per_request(client):
    body = client.get(
        "/api/positions/size?entry=100&stop=97.5&account=50000&risk_pct=1"
    ).json()
    assert body["shares"] == 200


def test_an_unsizable_request_explains_itself(client):
    r = client.get("/api/positions/size?entry=100&stop=100")
    assert r.status_code == 400
    assert "differ" in r.json()["detail"]


def test_the_size_route_is_not_shadowed_by_the_id_routes(client):
    """`/size` and `/{position_id}` share a shape; declaration order decides
    which wins, and a regression here would 404 silently."""
    assert client.get("/api/positions/size?entry=100&stop=95").status_code == 200


# --- the storage layer's own guard -------------------------------------------
#
# The route model already drops unknown fields, so the allowlist in
# update_position is a second layer the API can never exercise. Tested here
# directly, because a mutation that widened it passed the whole route suite.

def test_the_storage_layer_refuses_to_rewrite_history(db_path):
    """Entry price and share count are what every R in the journal is measured
    against. A caller that reaches past the API must not be able to move
    them."""
    with database.get_conn(db_path) as conn:
        pid = database.add_position(
            conn, ticker="AAPL", shares=10, entry_price=100.0, stop=95.0
        )
        database.update_position(conn, pid, entry_price=1.0, shares=9999,
                                 stop=98.0)
        row = database.get_position(conn, pid)

    assert row["entry_price"] == 100.0
    assert row["shares"] == 10
    assert row["stop"] == 98.0, "the fields that should move, should move"


def test_an_update_of_nothing_changes_nothing(db_path):
    with database.get_conn(db_path) as conn:
        pid = database.add_position(
            conn, ticker="AAPL", shares=10, entry_price=100.0
        )
        assert database.update_position(conn, pid, entry_price=1.0) is False
