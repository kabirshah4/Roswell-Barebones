"""A plan for every ticker, not only the ones that fired.

`evaluate()` returns None for most tickers most of the time, and "no signals"
tells the user nothing about what they hold. `analyse()` always answers, with
an explicit verdict, so a weak chart is visible as weak rather than absent.
"""

import math

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services import signal_engine


def frame(n=260, drift=0.002, noise=0.004, vol=1_000_000.0):
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = [100.0 * math.exp(drift * i) * (1 + noise * ((-1) ** i)) for i in range(n)]
    return pd.DataFrame(
        {
            "Open": close,
            "High": [c * 1.004 for c in close],
            "Low": [c * 0.996 for c in close],
            "Close": close,
            "Volume": [vol * (1.5 if i % 4 == 0 else 1.0) for i in range(n)],
        },
        index=idx,
    )


def frames(**kw):
    return {tf: frame(**kw) for tf in ("1h", "4h", "1d")}


# --- analyse always answers --------------------------------------------------

def test_a_strong_chart_is_tradeable():
    plan = signal_engine.analyse("X", frames(drift=0.004))
    assert plan.verdict == "tradeable"
    assert plan.actionable is True
    assert plan.grade in ("A+", "A", "B")


def test_a_weak_chart_still_returns_levels():
    """This is the whole point: evaluate() would return None and the ticker
    would vanish from the interface."""
    plan = signal_engine.analyse("X", frames(drift=-0.004))
    assert plan.verdict == "weak"
    assert plan.entry is not None and plan.stop is not None
    assert plan.target1 is not None and plan.target2 is not None


def test_a_weak_plan_is_not_actionable():
    plan = signal_engine.analyse("X", frames(drift=-0.004))
    assert plan.actionable is False
    assert plan.grade is None


def test_a_weak_plan_names_what_is_missing():
    """"No setup" without a reason is not information."""
    plan = signal_engine.analyse("X", frames(drift=-0.004))
    assert "of 7 confluences" in plan.reason
    assert "missing" in plan.reason.lower()


def test_a_weak_plan_says_the_levels_are_not_a_recommendation():
    plan = signal_engine.analyse("X", frames(drift=-0.004))
    assert "not a reason to take one" in plan.reason


def test_insufficient_history_is_its_own_verdict():
    plan = signal_engine.analyse("X", {tf: frame(n=20) for tf in ("1h", "4h", "1d")})
    assert plan.verdict == "insufficient_data"
    assert plan.entry is None


def test_earnings_blackout_is_its_own_verdict():
    plan = signal_engine.analyse(
        "X", frames(drift=0.004),
        earnings_at="2026-08-27T00:00:00Z", now_iso="2026-08-26T00:00:00Z")
    assert plan.verdict == "blackout"
    assert "gap" in plan.reason.lower()


def test_a_flat_chart_with_no_range_is_not_tradeable():
    flat = {tf: frame(drift=0.0, noise=0.0) for tf in ("1h", "4h", "1d")}
    assert signal_engine.analyse("X", flat).actionable is False


# --- evaluate stays a faithful filter ----------------------------------------

def test_evaluate_agrees_with_analyse_on_the_levels():
    """Two code paths computing levels differently is how a chart and an alert
    end up disagreeing about where the stop is."""
    f = frames(drift=0.004)
    plan = signal_engine.analyse("X", f)
    setup = signal_engine.evaluate("X", f)
    assert setup is not None
    for field in ("entry", "stop", "target1", "target2", "risk_reward", "grade"):
        assert getattr(setup, field) == getattr(plan, field)


def test_evaluate_returns_none_where_analyse_says_weak():
    f = frames(drift=-0.004)
    assert signal_engine.analyse("X", f).actionable is False
    assert signal_engine.evaluate("X", f) is None


def test_evaluate_still_honours_the_blackout():
    f = frames(drift=0.004)
    assert signal_engine.evaluate(
        "X", f, earnings_at="2026-08-27T00:00:00Z",
        now_iso="2026-08-26T00:00:00Z") is None


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
    with TestClient(create_app(
        cfg=Config(db_path=db_path), client=ExplodingClient(), start_poller=False
    )) as c:
        yield c


def seed_bars(db_path, ticker, n=900, drift=0.002):
    """900 hourly bars, not 400.

    The 4h frame is resampled from the 1h one, so 400 hourly bars yield only
    100 four-hour bars -- below the 200-bar minimum, and the plan comes back
    "insufficient_data" no matter how good the chart is.
    """
    from datetime import datetime, timedelta, timezone

    # Both series end at the same moment. Starting both at a fixed date and
    # stepping forward by their own interval left the hourly bars ending two
    # years before the daily ones, which no real feed does -- and which the
    # freshness guard correctly refuses as a session behind.
    t_end = datetime(2026, 6, 19, tzinfo=timezone.utc)
    for interval, step in (("1h", timedelta(hours=1)), ("1d", timedelta(days=1))):
        rows = []
        for i in range(n):
            price = 100.0 * math.exp(drift * i) * (1 + 0.004 * ((-1) ** i))
            rows.append({
                "ts": (t_end - step * (n - 1 - i)).isoformat(), "open": price,
                "high": price * 1.004, "low": price * 0.996, "close": price,
                "volume": 1_000_000.0 * (1.5 if i % 4 == 0 else 1.0),
            })
        with database.get_conn(db_path) as conn:
            database.upsert_bars(conn, ticker, interval, rows)


def test_every_watchlist_ticker_gets_a_plan(db_path, client):
    with database.get_conn(db_path) as conn:
        for t in ("AAA", "BBB", "CCC"):
            database.add_watchlist_ticker(conn, t)
    seed_bars(db_path, "AAA")
    plans = client.get("/api/plans").json()["plans"]
    assert {p["ticker"] for p in plans} == {"AAA", "BBB", "CCC"}


def test_a_ticker_without_bars_says_so_rather_than_vanishing(db_path, client):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "NEW")
    plan = client.get("/api/plans").json()["plans"][0]
    assert plan["verdict"] == "insufficient_data"
    assert "bars" in plan["reason"].lower()


def test_actionable_plans_sort_first(db_path, client):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "WEAK")
        database.add_watchlist_ticker(conn, "GOOD")
    seed_bars(db_path, "WEAK", drift=-0.004)
    # 0.002, not 0.004: a steeper trend runs so far from the EMA that
    # `not_extended` fails and the setup grades worse, not better.
    seed_bars(db_path, "GOOD", drift=0.002)
    plans = client.get("/api/plans").json()["plans"]
    assert plans[0]["ticker"] == "GOOD"
    assert plans[0]["verdict"] == "tradeable"


def test_the_plan_carries_every_level(db_path, client):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAA")
    seed_bars(db_path, "AAA", drift=0.004)
    plan = client.get("/api/plans").json()["plans"][0]
    for field in ("entry", "stop", "target1", "target2", "risk_reward"):
        assert plan[field] is not None, f"{field} missing"
    assert plan["stop"] < plan["entry"] < plan["target1"] < plan["target2"]


def test_passed_and_failed_confluences_are_separated(db_path, client):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAA")
    seed_bars(db_path, "AAA")
    plan = client.get("/api/plans").json()["plans"][0]
    assert set(plan["factors"]) & set(plan["factors_failed"]) == set()
    assert len(plan["factors"]) == plan["score"]


def test_the_grade_carries_its_measured_record(db_path, client):
    """Unconditionally: a test guarded by `if plan["grade"] == ...` passes
    happily when the record is dropped entirely."""
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAA")
    seed_bars(db_path, "AAA", drift=0.002)
    plan = client.get("/api/plans").json()["plans"][0]
    assert plan["grade"] is not None, "fixture no longer produces a graded plan"

    with database.get_conn(db_path) as conn:
        database.upsert_backtest_stats(conn, "AAA", {plan["grade"]: {
            "signals": 12, "wins": 5, "losses": 7, "open_trades": 0,
            "win_rate": 0.42, "avg_r": 0.25}})
    plan = client.get("/api/plans").json()["plans"][0]
    assert plan["measured"] is not None, "grade served without its record"
    assert plan["measured"]["signals"] == 12
    assert plan["measured"]["win_rate"] == 0.42


def test_the_risk_reward_is_two_by_construction():
    """target1 = entry + 2 x risk, so RR cannot vary. Pinned because the RR
    floor reads like a live filter and is not one."""
    plan = signal_engine.analyse("X", frames(drift=0.002))
    assert plan.risk_reward == 2.0
    assert signal_engine.RR_FLOOR == 2.0


def test_an_unmeasured_grade_reports_none_rather_than_a_number(db_path, client):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAA")
    seed_bars(db_path, "AAA", drift=0.004)
    assert client.get("/api/plans").json()["plans"][0]["measured"] is None


def test_an_empty_watchlist_is_not_an_error(client):
    assert client.get("/api/plans").json()["plans"] == []


def test_the_four_hour_frame_needs_four_times_the_hourly_bars(db_path, client):
    """Resampling 1h into 4h divides the count by four, so a frame that looks
    long enough on its own is not. Caught by a fixture that seeded 400 hourly
    bars and got 'insufficient_data' for a textbook uptrend."""
    from backend.services import indicators
    from backend.services.price_poller import _frames_for

    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAA")
    seed_bars(db_path, "AAA", n=400)
    with database.get_conn(db_path) as conn:
        assert _frames_for(conn, "AAA") is not None
        assert len(_frames_for(conn, "AAA")["4h"]) < indicators.MIN_BARS

    seed_bars(db_path, "AAA", n=900)
    with database.get_conn(db_path) as conn:
        assert len(_frames_for(conn, "AAA")["4h"]) >= indicators.MIN_BARS



def test_a_parabolic_trend_is_not_the_best_setup(db_path, client):
    """A steeper move is not automatically a better entry: price runs away
    from the EMA and `not_extended` fails. Worth pinning, because the obvious
    reading of "stronger trend = higher grade" is wrong here."""
    from backend.services.price_poller import _frames_for

    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "STEEP")
    seed_bars(db_path, "STEEP", drift=0.004)
    with database.get_conn(db_path) as conn:
        plan = signal_engine.analyse("STEEP", _frames_for(conn, "STEEP"))
    assert plan.factors["trend"] is True
    assert plan.factors["not_extended"] is False


# --- horizons ----------------------------------------------------------------
#
# A scalper and a position trader want different answers about the same chart.
# The arithmetic does not change; the timeframes it runs on decide what the
# numbers mean.

def test_every_horizon_names_three_frames():
    from backend.services import horizons

    for h in horizons.HORIZONS.values():
        assert len(h.frames) == 3, f"{h.key} does not name three frames"


def test_every_frame_is_either_fetched_or_resampled():
    """A frame that is neither would silently make the horizon unusable."""
    from backend.services import horizons

    for h in horizons.HORIZONS.values():
        for interval in h.frames:
            assert interval in h.fetch or interval in h.resample, (
                f"{h.key} names {interval} but never obtains it"
            )


def test_every_resample_source_is_itself_fetched():
    from backend.services import horizons

    for h in horizons.HORIZONS.values():
        for target, source in h.resample.items():
            assert source in h.fetch, f"{h.key} resamples {target} from unfetched {source}"


def test_four_hour_is_the_only_resampled_interval():
    """yfinance serves every other interval natively; resampling them would
    throw away data for nothing."""
    from backend.services import horizons

    resampled = {t for h in horizons.HORIZONS.values() for t in h.resample}
    assert resampled == {"4h"}


def test_refresh_cadence_matches_the_holding_period():
    """A scalp horizon that refreshes hourly is useless; a position horizon
    that refreshes every 15s is waste."""
    from backend.services import horizons

    order = ["scalp", "intraday", "swing", "position"]
    cadences = [horizons.HORIZONS[k].refresh_seconds for k in order]
    assert cadences == sorted(cadences), "cadence must grow with the horizon"
    assert horizons.HORIZONS["scalp"].refresh_seconds <= 30
    assert horizons.HORIZONS["position"].refresh_seconds >= 600


def test_an_unknown_horizon_falls_back_rather_than_raising():
    from backend.services import horizons

    assert horizons.get("nonsense").key == horizons.DEFAULT_HORIZON
    assert horizons.get(None).key == horizons.DEFAULT_HORIZON
    assert horizons.get("").key == horizons.DEFAULT_HORIZON


def test_the_horizon_is_case_and_space_insensitive():
    from backend.services import horizons

    assert horizons.get("  SCALP ").key == "scalp"


def test_frames_are_built_for_the_requested_horizon(db_path):
    from backend.services import horizons
    from backend.services.price_poller import _frames_for

    seed_bars(db_path, "AAA")           # seeds 1h and 1d
    with database.get_conn(db_path) as conn:
        swing = _frames_for(conn, "AAA", horizons.get("swing"))
        scalp = _frames_for(conn, "AAA", horizons.get("scalp"))
    assert set(swing) == {"1h", "4h", "1d"}
    # No 1m/5m/15m bars were seeded, so scalp honestly reports nothing.
    assert scalp is None


def test_the_route_reports_the_horizon_and_a_timestamp(db_path, client):
    body = client.get("/api/plans").json()
    assert body["horizon"]["key"] == "swing"
    assert body["horizon"]["frames"] == ["1h", "4h", "1d"]
    assert "as_of" in body


def test_the_timestamp_is_when_the_levels_were_computed(db_path, client):
    """Not when the page loaded: two requests must not share a stamp."""
    import time

    first = client.get("/api/plans").json()["as_of"]
    time.sleep(0.01)
    assert client.get("/api/plans").json()["as_of"] != first


def test_choosing_a_horizon_persists_it(db_path, client):
    client.get("/api/plans?horizon=intraday")
    assert client.get("/api/plans/horizons").json()["selected"] == "intraday"
    # And a later request with no explicit horizon keeps it.
    assert client.get("/api/plans").json()["horizon"]["key"] == "intraday"


def test_an_unknown_horizon_query_does_not_error(db_path, client):
    body = client.get("/api/plans?horizon=banana").json()
    assert body["horizon"]["key"] == "swing"


def test_a_horizon_without_cached_bars_says_which_ones_are_missing(db_path, client):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAA")
    seed_bars(db_path, "AAA")
    plan = client.get("/api/plans?horizon=scalp").json()["plans"][0]
    assert plan["verdict"] == "insufficient_data"
    assert "1m/5m/15m" in plan["reason"]


def test_the_poller_always_caches_the_swing_intervals(db_path):
    """The signal engine and backtest run on swing regardless of what the
    interface displays; switching to SCALP must not stop the alerts."""
    from backend.config import Config
    from backend.services.price_poller import PricePoller

    with database.get_conn(db_path) as conn:
        database.set_setting(conn, "horizon", "scalp")

    class YF:
        def fetch_quotes(self, t): return {}

    poller = PricePoller(YF(), db_path, Config(db_path=db_path))
    intervals = poller._bar_intervals()
    assert {"1h", "1d"} <= set(intervals), "swing intervals were dropped"
    assert {"1m", "5m", "15m"} <= set(intervals), "scalp intervals not added"


def test_the_poller_does_not_fetch_every_horizon_at_once(db_path):
    """Four horizons is nine intervals per ticker -- a lot of Yahoo calls for
    data nobody is looking at."""
    from backend.config import Config
    from backend.services.price_poller import PricePoller

    class YF:
        def fetch_quotes(self, t): return {}

    poller = PricePoller(YF(), db_path, Config(db_path=db_path))
    assert set(poller._bar_intervals()) == {"1h", "1d"}


def test_analyse_uses_the_frames_it_is_given_not_a_hardcoded_set(db_path):
    """`analyse` hardcoded the swing timeframes, so every other horizon
    reported insufficient_data no matter how much data it had. The frame
    builder was correct; the engine ignored it."""
    scalp = {tf: frame(drift=0.002) for tf in ("1m", "5m", "15m")}
    plan = signal_engine.analyse("X", scalp)
    assert plan.verdict != "insufficient_data", plan.reason
    assert plan.timeframes == "1m,5m,15m"


def test_the_finest_frame_drives_the_levels(db_path):
    """Entry, ATR and structure come from frames[0]; a wider frame would size
    a swing stop onto a scalp trade."""
    tight = {tf: frame(drift=0.002, noise=0.001) for tf in ("1m", "5m", "15m")}
    wide = {tf: frame(drift=0.002, noise=0.02) for tf in ("1m", "5m", "15m")}
    tight_plan = signal_engine.analyse("X", tight)
    wide_plan = signal_engine.analyse("X", wide)
    tight_risk = tight_plan.entry - tight_plan.stop
    wide_risk = wide_plan.entry - wide_plan.stop
    assert wide_risk > tight_risk, "ATR of the finest frame must set the stop"


def test_a_two_frame_dict_is_rejected(db_path):
    """The engine's multi-timeframe factor is meaningless with fewer than
    three, so it must not quietly score them."""
    plan = signal_engine.analyse("X", {tf: frame() for tf in ("1m", "5m")})
    assert plan.verdict == "insufficient_data"


def test_the_blank_plan_reports_the_horizon_frames(db_path):
    """"Needs 200 bars on 1h, 4h and 1d" while showing a scalp horizon is
    actively misleading."""
    plan = signal_engine.analyse("X", {tf: frame(n=20) for tf in ("1m", "5m", "15m")})
    assert "1m" in plan.reason and "1h" not in plan.reason


def test_switching_horizon_warms_its_bars(db_path):
    """Otherwise INTRADAY shows insufficient_data for half an hour and reads
    as broken."""
    import asyncio

    from backend.config import Config
    from backend.services.price_poller import PricePoller

    fetched = []

    class YF:
        def fetch_bars(self, ticker, period, interval):
            fetched.append((ticker, interval))
            return []

    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAA")

    poller = PricePoller(YF(), db_path, Config(db_path=db_path))
    asyncio.run(poller.warm_horizon("intraday"))
    assert {i for _, i in fetched} == {"5m", "30m", "1h"}


def test_a_horizon_warm_with_an_empty_watchlist_is_a_noop(db_path):
    import asyncio

    from backend.config import Config
    from backend.services.price_poller import PricePoller

    class Boom:
        def fetch_bars(self, t, p, i):
            raise AssertionError("nothing to warm")

    asyncio.run(PricePoller(Boom(), db_path, Config(db_path=db_path))
                .warm_horizon("scalp"))


def test_a_failing_interval_does_not_abort_the_horizon_warm(db_path):
    import asyncio

    from backend.config import Config
    from backend.services.price_poller import PricePoller

    seen = []

    class Flaky:
        def fetch_bars(self, ticker, period, interval):
            seen.append(interval)
            if interval == "5m":
                raise RuntimeError("down")
            return []

    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAA")
    asyncio.run(PricePoller(Flaky(), db_path, Config(db_path=db_path))
                .warm_horizon("intraday"))
    assert {"30m", "1h"} <= set(seen)


# --- what the levels actually mean -------------------------------------------

def test_the_levels_row_shows_the_move_each_target_represents(db_path, client):
    """"T1 504.62" does not say what you would make; "+1.70%" does."""
    import re

    js = client.get("/static/app.js").text
    block = js[js.index("function levelsRow"):js.index("function measuredLine")]
    assert "TP1" in block and "TP2" in block
    # Each target specifically: checking for one "%" passes while a target
    # silently loses its annotation, because the stop line has one too.
    for target in ("p.target1", "p.target2"):
        assert f"move({target})" in block, f"{target} has no percentage move"
        assert f"rMultiple({target})" in block, f"{target} has no R multiple"
    assert "move(p.stop)" in block, "the stop has no percentage move"


def test_the_r_multiple_is_measured_against_the_stop(db_path, client):
    """R is the ratio of reward to what is actually at risk, so it has to be
    computed from the stop rather than assumed."""
    js = client.get("/static/app.js").text
    block = js[js.index("function levelsRow"):js.index("function measuredLine")]
    assert "p.entry - p.stop" in block


def test_a_zero_risk_setup_does_not_divide_by_zero(db_path, client):
    js = client.get("/static/app.js").text
    block = js[js.index("function levelsRow"):js.index("function measuredLine")]
    assert "risk > 0" in block


def test_the_win_rate_never_appears_without_its_sample_size(db_path, client):
    """67% of three trades is noise, and a bare percentage invites reading it
    as fact."""
    js = client.get("/static/app.js").text
    block = js[js.index("function measuredLine"):js.index("async function renderPlans")]
    assert "m.signals" in block
    assert "win rate" in block


def test_every_list_uses_the_same_levels_renderer(db_path, client):
    """The fired setups, the playbook and the scanner previously each had
    their own copy."""
    js = client.get("/static/app.js").text
    assert js.count("function levelsRow") == 1
    assert js.count("levelsRow(") >= 4   # definition plus three call sites


def test_an_ungraded_setup_says_so_rather_than_showing_nothing(db_path, client):
    """A blank where a win rate belongs reads as a missing number, not as
    "there is no claim to check"."""
    js = client.get("/static/app.js").text
    block = js[js.index("function measuredLine"):js.index("async function renderPlans")]
    assert "no grade" in block
    assert "nothing measured" in block


def test_the_scanner_shows_the_measured_state_for_every_graded_row(db_path, client):
    js = client.get("/static/app.js").text
    block = js[js.index("async function loadScan") - 3000:js.index("async function loadScan")]
    assert "measuredLine(c.measured" in block
