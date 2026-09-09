"""Measuring whether each confluence factor earns its place.

The engine scores all seven as if they weigh the same. This checks the
measurement itself: that it never sees a future bar, that it resolves outcomes
the pessimistic way, and that it reports a factor's absence as well as its
presence.
"""

import math

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services import factor_study


def frame(n=400, drift=0.002, noise=0.004):
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = [100.0 * math.exp(drift * i) * (1 + noise * ((-1) ** i)) for i in range(n)]
    return pd.DataFrame(
        {
            "Open": close,
            "High": [c * 1.01 for c in close],
            "Low": [c * 0.99 for c in close],
            "Close": close,
            "Volume": [1_000_000.0 * (1.5 if i % 4 == 0 else 1.0) for i in range(n)],
        },
        index=idx,
    )


# --- the outcome rule --------------------------------------------------------

class Plan:
    stop = 95.0
    target1 = 110.0


def bars(rows):
    return pd.DataFrame(rows, columns=["Low", "High"])


def test_hitting_the_target_first_is_a_win():
    future = bars([[99, 101], [99, 111]])
    assert factor_study._outcome(Plan(), future) == 2.0


def test_hitting_the_stop_first_is_a_loss():
    future = bars([[99, 101], [94, 101]])
    assert factor_study._outcome(Plan(), future) == -1.0


def test_neither_hit_is_left_open():
    assert factor_study._outcome(Plan(), bars([[99, 101], [98, 102]])) is None


def test_a_bar_touching_both_counts_as_a_loss():
    """With only OHLC there is no way to know which came first intrabar, and
    assuming the target is how a backtest flatters itself."""
    assert factor_study._outcome(Plan(), bars([[94, 111]])) == -1.0


# --- the replay --------------------------------------------------------------

def test_the_replay_never_sees_a_future_bar():
    """The window must be sliced strictly up to the current bar."""
    import inspect

    source = inspect.getsource(factor_study.study_frame)
    assert "daily.iloc[: i + 1]" in source


def test_too_little_history_produces_nothing():
    by_factor, by_score = factor_study.study_frame("X", frame(n=60))
    assert dict(by_factor) == {}
    assert dict(by_score) == {}


def test_a_replay_records_both_sides_of_each_factor():
    by_factor, by_score = factor_study.study_frame("X", frame())
    assert by_factor, "no factors recorded"
    for name, sides in by_factor.items():
        assert isinstance(sides[True], list) and isinstance(sides[False], list)
        assert sides[True] or sides[False], f"{name} recorded nothing"


def test_scores_are_recorded_per_setup():
    _, by_score = factor_study.study_frame("X", frame())
    assert by_score
    assert all(0 <= s <= 7 for s in by_score)


# --- combining ---------------------------------------------------------------

def test_a_factor_that_never_varied_is_omitted():
    """Reporting an edge against an empty comparison group would be a number
    with nothing behind it."""
    single = {"always": {True: [2.0, -1.0], False: []}}
    report = factor_study.combine([(single, {4: [2.0, -1.0]})])
    assert report["factors"] == []


def test_the_edge_is_the_difference_between_the_two_sides():
    data = {
        "good": {True: [2.0, 2.0, 2.0, -1.0], False: [-1.0, -1.0, -1.0, 2.0]},
    }
    report = factor_study.combine([(data, {4: [2.0]})])
    row = report["factors"][0]
    assert row["with"]["win_rate"] == 0.75
    assert row["without"]["win_rate"] == 0.25
    assert row["win_edge_pp"] == pytest.approx(50.0)
    assert row["r_edge"] > 0


def test_a_harmful_factor_reports_a_negative_edge():
    """The point of measuring is that a factor may be worth less than nothing,
    and the report must be able to say so."""
    data = {"bad": {True: [-1.0, -1.0, 2.0], False: [2.0, 2.0, -1.0]}}
    report = factor_study.combine([(data, {4: [2.0]})])
    assert report["factors"][0]["r_edge"] < 0
    assert report["factors"][0]["win_edge_pp"] < 0


def test_factors_are_sorted_best_first():
    """Named so alphabetical order is the OPPOSITE of edge order: "strong"
    sorts before "weak" either way, so that pair proved nothing."""
    data = {
        "alpha_weak": {True: [2.0, -1.0], False: [2.0, -1.0]},
        "zulu_strong": {True: [2.0, 2.0], False: [-1.0, -1.0]},
    }
    report = factor_study.combine([(data, {4: [2.0]})])
    assert [f["factor"] for f in report["factors"]] == ["zulu_strong", "alpha_weak"]


def test_results_from_several_tickers_are_pooled():
    a = ({"f": {True: [2.0], False: [-1.0]}}, {4: [2.0]})
    b = ({"f": {True: [2.0], False: [-1.0]}}, {4: [-1.0]})
    report = factor_study.combine([a, b])
    assert report["factors"][0]["with"]["n"] == 2
    assert report["resolved"] == 2


def test_the_report_states_the_harness_limitation(db_path):
    """The backtest passes one daily frame for all three timeframes, so
    multi_timeframe compares a frame against itself. Reporting that number
    without the caveat would be misleading."""
    report = factor_study.combine([({"f": {True: [2.0], False: [-1.0]}}, {4: [2.0]})])
    assert "multi_timeframe" in report["caveat"]
    assert "same daily frame" in report["caveat"]


# --- the route ---------------------------------------------------------------

class ExplodingClient:
    def fetch_quotes(self, t): raise AssertionError("routes must never fetch")
    def fetch_bars(self, t, p, i): raise AssertionError("routes must never fetch")
    def fetch_news(self, t): raise AssertionError("routes must never fetch")
    def fetch_intraday(self, t): raise AssertionError("routes must never fetch")
    def fetch_fundamentals(self, t): raise AssertionError("routes must never fetch")
    def validate_ticker(self, t): return True


@pytest.fixture
def client(db_path):
    app = create_app(cfg=Config(db_path=db_path), client=ExplodingClient(),
                     start_poller=False)
    with TestClient(app) as c:
        yield c


def test_reading_the_study_never_runs_one(client):
    body = client.get("/api/factors").json()
    assert body["factors"] == []
    assert body["as_of"] is None


def test_a_refresh_with_no_bars_is_not_an_error(client):
    body = client.post("/api/factors/refresh").json()
    assert body["factors"] == []
    assert body["tickers"] == 0


def test_a_refresh_is_cached_for_the_next_read(client, db_path):
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(400):
        price = 100.0 * math.exp(0.002 * i) * (1 + 0.004 * ((-1) ** i))
        rows.append({"ts": (t0 + timedelta(days=i)).isoformat(), "open": price,
                     "high": price * 1.01, "low": price * 0.99, "close": price,
                     "volume": 1_000_000.0})
    with database.get_conn(db_path) as conn:
        database.upsert_bars(conn, "AAA", "1d", rows)

    fresh = client.post("/api/factors/refresh").json()
    assert fresh["tickers"] == 1
    assert client.get("/api/factors").json()["resolved"] == fresh["resolved"]
