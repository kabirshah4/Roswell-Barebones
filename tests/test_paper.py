"""The forward test.

Everything this app claims about grades comes from a backtest, and the factor
study already found the tell — 4-of-7 confluence beating 7-of-7 is what
overfitting looks like from the inside. No further backtesting settles that.
So the engine's plans get frozen and judged by bars that arrived afterwards.

Which makes these tests mostly about honesty: no look-ahead, the stop winning
ties, and gaps costing what gaps cost.
"""

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services import paper, paper_trader


def bar(ts, o, h, l, c, v=1000):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


# --- filling -----------------------------------------------------------------

def test_a_long_fills_when_price_trades_down_to_the_entry():
    fill = paper.fill_entry(100, "long", [bar("d1", 102, 103, 99, 101)])
    assert fill.price == 100 and not fill.gapped


def test_a_gap_below_the_entry_fills_at_the_open():
    """Better than planned, and the one place this model is allowed to be
    generous — it is genuinely what a resting limit order does."""
    fill = paper.fill_entry(100, "long", [bar("d1", 98, 99, 97, 98)])
    assert fill.price == 98 and fill.gapped


def test_a_price_that_never_comes_back_does_not_fill():
    assert paper.fill_entry(100, "long", [bar("d1", 105, 106, 104, 105)]) is None


def test_a_short_fills_on_the_way_up():
    fill = paper.fill_entry(100, "short", [bar("d1", 98, 101, 97, 100)])
    assert fill.price == 100


def test_the_arm_window_is_finite():
    """A setup that has not traded in five bars has gone. Leaving it armed
    forever would fill it on some unrelated move weeks later."""
    never = [bar(f"d{i}", 105, 106, 104, 105) for i in range(10)]
    late = never[:6] + [bar("d7", 101, 102, 99, 100)]
    assert paper.fill_entry(100, "long", late, arm_bars=5) is None


# --- exiting -----------------------------------------------------------------

def test_the_target_pays_when_it_is_reached():
    exit_ = paper.find_exit(100, 95, 110, "long", [bar("d1", 101, 112, 100, 111)])
    assert exit_.reason == "target1" and exit_.price == 110


def test_the_stop_wins_when_a_single_bar_covers_both():
    """With only OHLC there is no way to know which came first intrabar, and
    assuming the target is exactly how a backtest flatters itself."""
    exit_ = paper.find_exit(100, 95, 110, "long", [bar("d1", 101, 112, 94, 100)])
    assert exit_.reason == "stop"


def test_a_gap_through_the_stop_costs_what_it_costs():
    """The single most common way a paper record overstates itself is assuming
    the stop price held through a gap. Here it exits at the open — a loss of
    2.4R on a stop that promised 1R."""
    exit_ = paper.find_exit(100, 95, 110, "long", [bar("d1", 88, 90, 87, 89)])
    assert exit_.price == 88 and exit_.gapped
    assert (exit_.price - 100) / abs(100 - 95) < -2


def test_a_trade_that_resolves_nothing_is_closed_on_time():
    """Left open forever, every mediocre result would quietly leave the
    record."""
    flat = [bar(f"d{i}", 100, 101, 99, 100) for i in range(10)]
    exit_ = paper.find_exit(100, 95, 110, "long", flat, max_hold=5)
    assert exit_.reason == "time"


def test_an_unresolved_trade_inside_the_window_stays_open():
    flat = [bar(f"d{i}", 100, 101, 99, 100) for i in range(3)]
    assert paper.find_exit(100, 95, 110, "long", flat, max_hold=40) is None


def test_a_short_stops_out_upward():
    exit_ = paper.find_exit(100, 105, 90, "short", [bar("d1", 101, 106, 100, 105)])
    assert exit_.reason == "stop"


# --- no look-ahead -----------------------------------------------------------

def test_only_bars_after_the_plan_may_fill_it():
    """The bar the plan was computed from must never be the bar that fills it.
    This one function is the whole guarantee."""
    bars = [bar("2026-01-01", 1, 1, 1, 1), bar("2026-01-02", 2, 2, 2, 2)]
    assert [b["ts"] for b in paper.bars_after(bars, "2026-01-01")] == ["2026-01-02"]


def test_a_plan_armed_after_the_last_bar_has_nothing_to_fill_it():
    bars = [bar("2026-01-01", 1, 1, 1, 1)]
    assert paper.bars_after(bars, "2026-06-01") == []


# --- viability ---------------------------------------------------------------

def test_a_stop_inside_the_spread_is_not_modellable():
    """Found by replaying: one NVDA plan with a stop five cents from a $43.92
    entry — 0.11% — produced −28R on a gap and dragged a 5,000-trade sample's
    mean to −202R. The trade was not unusually bad; the denominator was
    unusually small."""
    assert paper.viable(43.92, 43.87) is False
    assert paper.viable(100, 95) is True


def test_viability_needs_both_numbers():
    assert paper.viable(None, 95) is False
    assert paper.viable(100, None) is False
    assert paper.viable(0, 0) is False


def test_the_model_states_where_it_is_generous():
    """A forward test that hides its assumptions is worth less than none."""
    assert len(paper.CAVEATS) >= 3
    assert any("slippage" in c or "spread" in c for c in paper.CAVEATS)


# --- the runner --------------------------------------------------------------

class ExplodingClient:
    def fetch_quotes(self, t): raise AssertionError("must never fetch")
    def fetch_intraday(self, t): raise AssertionError("must never fetch")
    def fetch_news(self, t): raise AssertionError("must never fetch")
    def fetch_fundamentals(self, t): raise AssertionError("must never fetch")
    def fetch_bars(self, t, p, i): raise AssertionError("must never fetch")
    def fetch_earnings_dates(self, t, limit=8): raise AssertionError("must never fetch")
    def validate_ticker(self, t): return True


class FakePlan:
    def __init__(self, verdict="tradeable", entry=100.0, stop=95.0,
                 target1=110.0, grade="B", score=4):
        self.verdict, self.entry, self.stop = verdict, entry, stop
        self.target1, self.target2 = target1, 115.0
        self.grade, self.score = grade, score
        self.direction = "long"
        self.factors = {"momentum": True, "volume": False}


@pytest.fixture
def client(db_path):
    app = create_app(cfg=Config(db_path=db_path), client=ExplodingClient(),
                     start_poller=False)
    with TestClient(app) as c:
        yield c


def test_only_tradeable_plans_are_armed(db_path):
    """The record answers "does acting on this work", not "what would happen
    if I took everything"."""
    with database.get_conn(db_path) as conn:
        assert paper_trader.arm(conn, "AAPL", FakePlan("watch"), "swing", "1d") is None
        assert paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d") is not None


def test_an_unmodellable_plan_is_not_armed(db_path):
    with database.get_conn(db_path) as conn:
        tight = FakePlan(entry=100.0, stop=99.95)
        assert paper_trader.arm(conn, "AAPL", tight, "swing", "1d") is None


def test_one_trade_per_ticker_at_a_time(db_path):
    """A setup that stays valid for a week would otherwise arm a fresh trade
    every cycle and fill the record with fifty copies of one idea."""
    with database.get_conn(db_path) as conn:
        assert paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
        assert paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d") is None
        # A different horizon is a different idea.
        assert paper_trader.arm(conn, "AAPL", FakePlan(), "scalp", "1d")


def test_the_plan_is_frozen_as_computed(db_path):
    """Judging the engine against a plan it has since revised measures
    nothing."""
    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
        row = database.paper_trades(conn)[0]
    assert row["planned_entry"] == 100.0 and row["stop"] == 95.0
    assert row["plan_grade"] == "B" and row["plan_score"] == 4


def test_only_the_passing_factors_are_recorded(db_path):
    """`factors` is a dict of pass/fail. Storing its keys would record a
    4-of-7 setup as 7-of-7."""
    import json

    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
        row = database.paper_trades(conn)[0]
    assert json.loads(row["factors"]) == ["momentum"]


def test_a_trade_fills_then_resolves(db_path):
    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
        trade = database.live_paper_trades(conn)[0]
        bars = [bar("2027-01-01", 102, 103, 99, 101),
                bar("2027-01-02", 101, 112, 100, 111)]
        assert paper_trader.resolve(conn, trade, bars) == "target1"
        row = database.paper_trades(conn)[0]
    assert row["entry_price"] == 100 and row["exit_price"] == 110


def test_a_plan_that_never_trades_expires(db_path):
    """Not a loss — but the fill rate is part of the honest picture."""
    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
        trade = database.live_paper_trades(conn)[0]
        away = [bar(f"2027-01-0{i}", 105, 106, 104, 105) for i in range(1, 7)]
        assert paper_trader.resolve(conn, trade, away) == "expired"
        row = database.paper_trades(conn)[0]
    assert row["exit_reason"] == "expired" and row["exit_price"] is None


def test_a_gap_onto_the_stop_is_dropped_not_scored(db_path):
    """Filling a hair above the stop leaves no distance to divide by, and
    manufactures enormous R from an ordinary move."""
    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(entry=100.0, stop=95.0),
                         "swing", "1d")
        trade = database.live_paper_trades(conn)[0]
        assert paper_trader.resolve(
            conn, trade, [bar("2027-01-01", 95.01, 96, 95.0, 95.5)]
        ) == "unviable"


def test_bars_before_the_plan_cannot_fill_it(db_path):
    """The look-ahead guard, end to end."""
    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
        trade = database.live_paper_trades(conn)[0]
        stale = [bar("1999-01-01", 90, 91, 89, 90)]
        assert paper_trader.resolve(conn, trade, stale) is None
        assert database.paper_trades(conn)[0]["entry_at"] is None


# --- the route ---------------------------------------------------------------

def test_results_are_grouped_by_state(client, db_path):
    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
    body = client.get("/api/paper").json()
    assert body["counts"]["armed"] == 1
    assert body["counts"]["closed"] == 0


def test_the_fill_rate_is_reported(client, db_path):
    """A strategy whose entries rarely trade is not the strategy the backtest
    measured."""
    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
        t = database.live_paper_trades(conn)[0]
        database.fill_paper_trade(conn, t["id"], 100.0, "2027-01-01", False)
        database.close_paper_trade(conn, t["id"], 110.0, "2027-01-02", "target1")
        paper_trader.arm(conn, "NVDA", FakePlan(), "swing", "1d")
        t2 = database.live_paper_trades(conn)[0]
        database.close_paper_trade(conn, t2["id"], None, "2027-01-02", "expired")
    assert client.get("/api/paper").json()["fill_rate"] == 50.0


def test_unfilled_trades_never_enter_the_record(client, db_path):
    """An expired plan has no result. Scoring it as a scratch would dilute
    every number."""
    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
        t = database.live_paper_trades(conn)[0]
        database.close_paper_trade(conn, t["id"], None, "2027-01-02", "expired")
    body = client.get("/api/paper").json()
    assert body["journal"]["closed"] == 0
    assert body["counts"]["dropped"] == 1


def test_the_caveats_travel_with_the_numbers(client):
    assert len(client.get("/api/paper").json()["caveats"]) >= 3


def test_the_route_never_fetches(client, db_path):
    with database.get_conn(db_path) as conn:
        paper_trader.arm(conn, "AAPL", FakePlan(), "swing", "1d")
    assert client.get("/api/paper").status_code == 200
