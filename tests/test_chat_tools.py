import json

from backend.db import database
from backend.services import chat_tools as tools


def test_schemas_are_wellformed():
    assert len(tools.TOOL_SCHEMAS) >= 8
    for s in tools.TOOL_SCHEMAS:
        assert s["name"] and s["description"]
        assert s["input_schema"]["type"] == "object"
        assert "properties" in s["input_schema"]


def seed_daily_bars(db_path, ticker="AAPL", n=120):
    """Enough history for the tools that need a real series."""
    import math
    from datetime import datetime, timedelta, timezone

    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        price = 100.0 * math.exp(0.001 * i) + (1.5 if i % 2 else -1.5)
        rows.append({
            "ts": (start + timedelta(days=i)).isoformat(),
            "open": price, "high": price * 1.01, "low": price * 0.99,
            "close": price, "volume": 1_000_000.0,
        })
    with database.get_conn(db_path) as conn:
        database.upsert_bars(conn, ticker, "1d", rows)


def test_every_schema_has_a_matching_executor(db_path):
    seed_daily_bars(db_path)
    for s in tools.TOOL_SCHEMAS:
        out = tools.execute(s["name"], {"ticker": "AAPL"}, db_path)
        assert isinstance(out, dict), f"{s['name']} returned {type(out)}"
        assert "error" not in out, f"{s['name']} errored: {out.get('error')}"


def test_unknown_tool_returns_error_not_raise(db_path):
    assert "error" in tools.execute("nope", {}, db_path)


def test_get_watchlist(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    assert tools.execute("get_watchlist", {}, db_path)["tickers"] == ["AAPL"]


def test_get_prices_returns_cached_only(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_price(conn, "AAPL", 100.0, 99.0, 1.01, 5, "USD")
    out = tools.execute("get_prices", {}, db_path)
    assert out["prices"][0]["ticker"] == "AAPL"
    assert out["prices"][0]["price"] == 100.0


def test_get_news_for_ticker(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_news_article(
            conn, "n1", "AAPL", "Headline", "Reuters", "http://x",
            "2026-08-01T00:00:00Z", "blurb")
        database.set_enrichment(conn, "n1", "One-liner.", "bullish")
    out = tools.execute("get_news", {"ticker": "AAPL"}, db_path)
    assert out["articles"][0]["title"] == "Headline"
    assert out["articles"][0]["sentiment"] == "bullish"


def test_get_signals_includes_computed_levels(db_path):
    from backend.services.signal_engine import Setup

    setup = Setup(
        ticker="KO", direction="long", grade="A+", score=6,
        factors={"trend": True}, entry=70.0, stop=68.0, target1=74.0,
        target2=76.0, risk_reward=2.0, timeframes="1h,4h,1d", earnings_at=None)
    with database.get_conn(db_path) as conn:
        database.upsert_signal(conn, setup, "KO|long|A+|70")
    s = tools.execute("get_signals", {}, db_path)["signals"][0]
    assert s["entry"] == 70.0 and s["stop"] == 68.0
    assert s["grade"] == "A+"


def test_get_backtest_stats_uses_cached_bars_only(db_path):
    """No network: with no cached bars it must say so, not fetch."""
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    out = tools.execute("get_backtest_stats", {"ticker": "AAPL"}, db_path)
    assert "stats" in out or "note" in out


def test_get_earnings(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_earnings(conn, "AAPL", "2099-10-29T20:00:00+00:00", 1.98)
    out = tools.execute("get_earnings", {"ticker": "AAPL"}, db_path)
    assert out["next_earnings"].startswith("2099-10-29")


def test_run_screener_respects_filters(db_path):
    with database.get_conn(db_path) as conn:
        database.upsert_fundamentals(conn, "CHEAP", pe_ratio=8.0)
        database.upsert_fundamentals(conn, "RICH", pe_ratio=60.0)
    out = tools.execute("run_screener", {"pe_max": 20}, db_path)
    assert [m["ticker"] for m in out["matches"]] == ["CHEAP"]


def test_screener_reports_coverage_so_partial_data_is_visible(db_path):
    with database.get_conn(db_path) as conn:
        database.seed_universe(conn, ["A", "B", "C"])
        database.upsert_fundamentals(conn, "A", pe_ratio=10.0)
    out = tools.execute("run_screener", {}, db_path)
    assert out["coverage"]["universe"] == 3
    assert out["coverage"]["screened"] == 1


def test_get_macro_calendar(db_path):
    with database.get_conn(db_path) as conn:
        database.upsert_macro_event(
            conn, "1|2099-01-01", 1, "Consumer Price Index", "2099-01-01", "high")
    out = tools.execute("get_macro_calendar", {}, db_path)
    assert out["events"][0]["release_name"] == "Consumer Price Index"


def test_results_are_json_serialisable(db_path):
    """Results go back to the model as JSON; a non-serialisable value breaks the loop."""
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_price(conn, "AAPL", 100.0, 99.0, 1.01, 5, "USD")
    for s in tools.TOOL_SCHEMAS:
        json.dumps(tools.execute(s["name"], {"ticker": "AAPL"}, db_path))


def test_no_tool_performs_network_io():
    """The tool module must not import any network client."""
    import inspect

    src = inspect.getsource(tools)
    for banned in ("import httpx", "import requests", "yfinance", "anthropic"):
        assert banned not in src, f"{banned} must not appear in chat_tools"


def test_backtest_description_tells_the_model_to_pair_grades_with_stats():
    """The A+ grade did not beat B in measurement; a bare letter overclaims."""
    sig = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "get_signals")
    assert "get_backtest_stats" in sig["description"]


# --- get_link_health --------------------------------------------------------

def _seed_link(conn, aid, status=None, code=None):
    database.upsert_news_article(
        conn, aid, "AAPL", f"Headline {aid}", "Reuters", f"https://x.test/{aid}",
        "2026-08-01T00:00:00Z", "blurb")
    if status:
        database.set_link_status(conn, aid, status, code)


def test_link_health_reports_counts_and_broken_rows(db_path):
    with database.get_conn(db_path) as conn:
        _seed_link(conn, "ok1", "ok", 200)
        _seed_link(conn, "dead1", "dead", 404)
        _seed_link(conn, "new1")

    out = tools.execute("get_link_health", {}, db_path)
    assert out["summary"] == {"ok": 1, "dead": 1, "unchecked": 1}
    assert [r["id"] for r in out["broken"]] == ["dead1"]


def test_link_health_broken_rows_carry_the_url(db_path):
    with database.get_conn(db_path) as conn:
        _seed_link(conn, "dead1", "dead", 410)
    out = tools.execute("get_link_health", {}, db_path)
    assert out["broken"][0]["url"] == "https://x.test/dead1"
    assert out["broken"][0]["link_code"] == 410


def test_link_health_on_an_empty_table(db_path):
    out = tools.execute("get_link_health", {}, db_path)
    assert out == {"summary": {}, "broken": []}


def test_link_health_tool_is_advertised():
    names = [s["name"] for s in tools.TOOL_SCHEMAS]
    assert "get_link_health" in names


def test_link_health_description_warns_about_unchecked():
    """Unchecked is not the same as working; the model must not conflate them."""
    schema = next(
        s for s in tools.TOOL_SCHEMAS if s["name"] == "get_link_health"
    )
    assert "unchecked" in schema["description"].lower()


# --- get_price_outlook ------------------------------------------------------

def test_outlook_without_bars_refuses_rather_than_estimating(db_path):
    """An empty projection is worse than none: the model would fill the gap."""
    out = tools.execute("get_price_outlook", {"ticker": "AAPL"}, db_path)
    assert "error" in out
    assert "do not estimate" in out["error"].lower()


def test_outlook_reports_bands_over_several_horizons(db_path):
    seed_daily_bars(db_path)
    out = tools.execute("get_price_outlook", {"ticker": "AAPL"}, db_path)
    assert len(out["bands"]) >= 3
    for band in out["bands"]:
        lo68, hi68 = band["range_68pct"]
        lo95, hi95 = band["range_95pct"]
        assert lo95 <= lo68 <= band["central_estimate"] <= hi68 <= hi95


def test_outlook_always_states_its_basis(db_path):
    """A number with no stated model is indistinguishable from a guess."""
    seed_daily_bars(db_path)
    out = tools.execute("get_price_outlook", {"ticker": "AAPL"}, db_path)
    assert "volatility" in out["basis"].lower()
    assert "earnings" in out["basis"].lower()


def test_outlook_answers_a_target_probability(db_path):
    seed_daily_bars(db_path)
    out = tools.execute(
        "get_price_outlook",
        {"ticker": "AAPL", "target_price": 200, "horizon_days": 21},
        db_path,
    )
    assert 0.0 <= out["target"]["probability_above"] <= 1.0
    assert out["target"]["horizon_trading_days"] == 21


def test_a_target_far_above_is_reported_as_unlikely(db_path):
    seed_daily_bars(db_path)
    out = tools.execute(
        "get_price_outlook", {"ticker": "AAPL", "target_price": 10_000}, db_path)
    assert out["target"]["probability_above"] < 0.05


def test_no_target_means_no_target_block(db_path):
    seed_daily_bars(db_path)
    out = tools.execute("get_price_outlook", {"ticker": "AAPL"}, db_path)
    assert "target" not in out


def test_outlook_never_raises_on_junk_input(db_path):
    for args in ({}, {"ticker": ""}, {"ticker": "AAPL", "target_price": "abc"}):
        assert isinstance(
            tools.execute("get_price_outlook", args, db_path), dict)


def test_the_outlook_tool_is_advertised():
    names = [s["name"] for s in tools.TOOL_SCHEMAS]
    assert "get_price_outlook" in names


def test_the_outlook_description_demands_the_range(db_path):
    """A central estimate quoted alone reads as a prediction."""
    schema = next(
        s for s in tools.TOOL_SCHEMAS if s["name"] == "get_price_outlook")
    assert "range" in schema["description"].lower()
