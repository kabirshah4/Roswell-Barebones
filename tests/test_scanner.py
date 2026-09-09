"""Pre-market / intraday market scanner."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services import market_session
from backend.services.premarket_scanner import COLUMNS, PremarketScanner, rank

ET = ZoneInfo("America/New_York")


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET).astimezone(timezone.utc)


# --- session detection -------------------------------------------------------

def test_premarket_is_before_the_open():
    assert market_session.current(at(2026, 8, 26, 7, 30)).name == "premarket"


def test_the_regular_session():
    assert market_session.current(at(2026, 8, 26, 10, 0)).name == "open"


def test_after_hours():
    assert market_session.current(at(2026, 8, 26, 17, 0)).name == "afterhours"


def test_overnight_is_closed():
    assert market_session.current(at(2026, 8, 26, 2, 0)).name == "closed"


def test_the_weekend_is_not_tradeable():
    session = market_session.current(at(2026, 8, 29, 10, 0))  # Saturday
    assert session.name == "weekend"
    assert session.is_tradeable is False


def test_the_boundaries_land_on_the_right_side():
    assert market_session.current(at(2026, 8, 26, 9, 29)).name == "premarket"
    assert market_session.current(at(2026, 8, 26, 9, 30)).name == "open"
    assert market_session.current(at(2026, 8, 26, 15, 59)).name == "open"
    assert market_session.current(at(2026, 8, 26, 16, 0)).name == "afterhours"


def test_the_session_decides_what_to_rank_on():
    """Pre-market has no `change` yet and the regular session has no fresh
    `premarket_change`; ranking on the wrong one reads a stale field."""
    assert market_session.current(at(2026, 8, 26, 7, 0)).ranks_on == "premarket_change"
    assert market_session.current(at(2026, 8, 26, 11, 0)).ranks_on == "change"


# --- ranking -----------------------------------------------------------------

def row(**kw):
    base = {
        "close": 50.0, "average_volume_10d_calc": 20_000_000,
        "premarket_change": 0.0, "premarket_volume": 500_000,
        "change": 0.0, "relative_volume_10d_calc": 1.0, "RSI": 50.0,
    }
    base.update(kw)
    return base


def test_an_up_move_outranks_a_down_move_of_the_same_size():
    """The engine is long-only: every one of its seven factors wants an
    uptrend. Ranking on |move| filled the list with big decliners that then
    scored 0/7 and crowded out the names worth grading."""
    up, _ = rank(row(change=5.0), "open")
    down, _ = rank(row(change=-5.0), "open")
    assert up > down


def test_a_large_decline_does_not_top_the_list():
    modest_up, _ = rank(row(change=2.0), "open")
    huge_down, _ = rank(row(change=-39.0), "open")
    assert modest_up > huge_down


def test_a_decline_is_listed_not_excluded():
    """A controlled pullback in an uptrend is a legitimate long entry; the
    engine decides, the scanner just does not promote it."""
    _, why = rank(row(change=-3.0), "open")
    assert any("trend intact" in w for w in why)


def test_an_extreme_gap_is_capped():
    """Beyond ~15% a gap is usually a halt, an offering or a reverse split."""
    fifteen, _ = rank(row(change=15.0), "open")
    fifty, _ = rank(row(change=50.0), "open")
    assert fifty == fifteen


def test_a_gap_with_no_volume_is_penalised():
    """A 5% gap on 200 shares is a quote, not a market."""
    liquid, _ = rank(row(premarket_change=5.0, premarket_volume=800_000), "premarket")
    thin, why = rank(row(premarket_change=5.0, premarket_volume=200), "premarket")
    assert liquid > thin
    assert any("volume" in w for w in why)


def test_relative_volume_lifts_a_regular_session_candidate():
    quiet, _ = rank(row(change=2.0, relative_volume_10d_calc=0.4), "open")
    busy, _ = rank(row(change=2.0, relative_volume_10d_calc=3.0), "open")
    assert busy > quiet


def test_an_extended_name_is_marked_down():
    """Entering at RSI 88 puts the stop further away for the same target."""
    normal, _ = rank(row(change=2.0, RSI=55.0), "open")
    extended, why = rank(row(change=2.0, RSI=88.0), "open")
    assert normal > extended
    assert any("extended" in w for w in why)


def test_more_liquidity_scores_higher_but_with_diminishing_returns():
    ten, _ = rank(row(change=2.0, average_volume_10d_calc=10_000_000), "open")
    hundred, _ = rank(row(change=2.0, average_volume_10d_calc=100_000_000), "open")
    thousand, _ = rank(row(change=2.0, average_volume_10d_calc=1_000_000_000), "open")
    assert thousand > hundred > ten
    assert (thousand - hundred) <= (hundred - ten) + 0.01


def test_missing_fields_do_not_crash_the_ranking():
    score, _ = rank({}, "open")
    assert isinstance(score, float)


def test_a_nan_field_is_treated_as_missing():
    score, _ = rank(row(RSI=float("nan")), "open")
    assert isinstance(score, float)


# --- the scan ----------------------------------------------------------------

def poster(rows):
    def f(url, payload):
        f.payload = payload
        return {"data": rows}

    return f


def make_row(ticker, **values):
    merged = {c: None for c in COLUMNS}
    merged.update({"name": ticker, "description": f"{ticker} Inc"})
    merged.update(values)
    return {"s": f"NASDAQ:{ticker}", "d": [merged[c] for c in COLUMNS]}


def test_the_filters_reach_the_query():
    """A filter applied client-side would pull the whole market every scan."""
    p = poster([])
    PremarketScanner(poster=p).scan(min_price=25.0, min_avg_volume=5_000_000)
    filters = {f["left"]: f["right"] for f in p.payload["filter"]}
    assert filters["close"] == 25.0
    assert filters["average_volume_10d_calc"] == 5_000_000


def test_more_rows_are_pulled_than_returned():
    """The scanner sorts by raw move; the ranking reorders on volume and
    extension, so the best name is routinely not in the top few by gap."""
    p = poster([])
    PremarketScanner(poster=p).scan(limit=10)
    assert p.payload["range"][1] >= 100


def test_results_come_back_ranked_not_in_api_order():
    rows = [
        make_row("MEH", close=50, average_volume_10d_calc=11_000_000, change=0.2,
                 relative_volume_10d_calc=0.5, RSI=50),
        make_row("GOOD", close=50, average_volume_10d_calc=40_000_000, change=4.0,
                 relative_volume_10d_calc=3.0, RSI=55),
    ]
    out = PremarketScanner(poster=poster(rows)).scan(session_name="open")
    assert out[0].ticker == "GOOD"


def test_a_failed_scan_returns_empty_never_raises():
    def boom(url, payload):
        raise RuntimeError("403")

    assert PremarketScanner(poster=boom).scan() == []


def test_one_malformed_row_does_not_discard_the_rest():
    rows = [{"broken": True}, make_row("OK", close=20,
                                       average_volume_10d_calc=11_000_000, change=1.0)]
    assert [c.ticker for c in PremarketScanner(poster=poster(rows)).scan()] == ["OK"]


def test_share_class_dots_are_converted():
    rows = [make_row("BRK.B", close=500, average_volume_10d_calc=11_000_000)]
    assert PremarketScanner(poster=poster(rows)).scan()[0].ticker == "BRK-B"


# --- routes ------------------------------------------------------------------

class FakeScanner:
    def __init__(self, candidates=None):
        self.calls = []
        self._candidates = candidates or []

    def scan(self, session_name="premarket", min_price=15.0,
             min_avg_volume=10_000_000, limit=40):
        self.calls.append((session_name, min_price, min_avg_volume))
        return self._candidates


class ExplodingClient:
    def fetch_quotes(self, t): raise AssertionError("routes must never fetch")
    def fetch_bars(self, t, p, i): return []
    def fetch_news(self, t): raise AssertionError("routes must never fetch")
    def fetch_intraday(self, t): raise AssertionError("routes must never fetch")
    def fetch_fundamentals(self, t): raise AssertionError("routes must never fetch")
    def validate_ticker(self, t): return True


@pytest.fixture
def client(db_path):
    scanner = FakeScanner()
    app = create_app(cfg=Config(db_path=db_path), client=ExplodingClient(),
                     scanner=scanner, start_poller=False)
    with TestClient(app) as c:
        c.scanner = scanner
        yield c


def test_reading_the_scan_never_triggers_one(client):
    body = client.get("/api/scanner").json()
    assert body["candidates"] == []
    assert client.scanner.calls == [], "a read triggered a network scan"


def test_the_read_reports_the_session(client):
    assert "session" in client.get("/api/scanner").json()


def test_refresh_passes_the_filters_through(client):
    client.post("/api/scanner/refresh?min_price=30&min_avg_volume=5000000")
    assert client.scanner.calls[-1][1] == 30.0
    assert client.scanner.calls[-1][2] == 5_000_000.0


def test_a_refresh_is_cached_for_the_next_read(client, db_path):
    from backend.services.premarket_scanner import Candidate

    client.scanner._candidates = [Candidate(
        ticker="AAA", name="A", price=50.0, avg_volume_10d=2e7,
        premarket_change=1.0, premarket_volume=1e5, change=1.0, volume=1e6,
        relative_volume=1.2, market_cap=1e9, atr=1.0, rsi=55.0, sector="Tech",
        score=42.0, why=["moving +1.0%"])]
    client.post("/api/scanner/refresh")
    assert client.get("/api/scanner").json()["candidates"][0]["ticker"] == "AAA"


def test_the_scan_records_when_it_ran(client):
    client.post("/api/scanner/refresh")
    assert client.get("/api/scanner").json()["as_of"] is not None


def test_the_session_says_which_move_field_is_live(client):
    """`premarket_change` holds yesterday's gap once the market opens.
    Displaying it then is a stale number presented as today's move."""
    body = client.get("/api/scanner").json()
    assert body["session"]["ranks_on"] in ("premarket_change", "change")


def test_the_refresh_reports_it_too(client):
    assert "ranks_on" in client.post("/api/scanner/refresh").json()["session"]


def test_the_ui_displays_the_field_the_session_ranks_on(db_path):
    from fastapi.testclient import TestClient

    with TestClient(create_app(cfg=Config(db_path=db_path), start_poller=False)) as c:
        js = c.get("/static/app.js").text
    assert "session.ranks_on" in js
    assert "c[moveField]" in js


def test_a_graded_candidate_carries_its_measured_record(db_path):
    """A grade without its numbers is the bare letter this app refuses to show
    anywhere else — and a scanner pick is rarely on the watchlist, so nothing
    has backtested it in advance."""
    import math
    from datetime import datetime, timedelta, timezone

    from backend.services.premarket_scanner import Candidate

    def bars(n, step, drift):
        t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        out = []
        for i in range(n):
            price = 100.0 * math.exp(drift * i) * (1 + 0.004 * ((-1) ** i))
            out.append({
                "ts": (t0 + step * i).isoformat(), "open": price,
                "high": price * 1.006, "low": price * 0.994, "close": price,
                "volume": 1_000_000.0 * (1.5 if i % 4 == 0 else 1.0),
            })
        return out

    class Feeding(ExplodingClient):
        def fetch_bars(self, ticker, period, interval):
            if interval == "1d":
                return bars(500, timedelta(days=1), 0.002)
            return bars(900, timedelta(minutes=5), 0.002)

    scanner = FakeScanner([Candidate(
        ticker="AAA", name="A", price=50.0, avg_volume_10d=2e7,
        premarket_change=1.0, premarket_volume=1e5, change=2.0, volume=1e6,
        relative_volume=1.5, market_cap=1e9, atr=1.0, rsi=55.0, sector="Tech",
        score=40.0, why=["moving +2.0%"])])

    app = create_app(cfg=Config(db_path=db_path), client=Feeding(),
                     scanner=scanner, start_poller=False)
    with TestClient(app) as c:
        row = c.post("/api/scanner/refresh").json()["candidates"][0]

    assert row["plan"] is not None, "the candidate was never graded"
    assert "measured" in row
    if row["plan"]["grade"]:
        assert row["measured"] is not None, "a graded pick has no measured record"
        assert 0.0 <= row["measured"]["win_rate"] <= 1.0
        assert row["measured"]["signals"] >= 0


def test_an_ungraded_candidate_reports_no_record_rather_than_a_number(db_path):
    scanner = FakeScanner([])
    app = create_app(cfg=Config(db_path=db_path), client=ExplodingClient(),
                     scanner=scanner, start_poller=False)
    with TestClient(app) as c:
        body = c.post("/api/scanner/refresh").json()
    assert body["candidates"] == []
