"""Covers the app-level universe-seeding path that no other test exercises:
`_load_universe()` itself and the `start_poller=True` lifespan wiring that
calls `database.seed_universe` with its output. Without these, a broken data
file path, dropped lines, or a reordered lifespan block would silently leave
the screener running against an empty universe -- indistinguishable from
"still warming" -- and nothing would notice.
"""

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.config import Config
from backend.db import database
from backend.main import _load_universe, create_app


class InertClient:
    """Duck-typed client whose methods are safe no-ops. Never raises, but
    also never simulates a real fetch -- the poller only reaches these when
    the watchlist is non-empty, which it never is in these tests."""

    def fetch_quotes(self, tickers):
        return {}

    def fetch_intraday(self, ticker):
        return []

    def fetch_news(self, ticker):
        return []

    def fetch_fundamentals(self, ticker):
        return None

    def check_ticker(self, ticker):
        return "ok"

    def validate_ticker(self, ticker):
        return True


class InertFred:
    enabled = False

    def fetch_upcoming(self, days=30):
        return []


def test_load_universe_returns_normalized_tickers():
    tickers = _load_universe()
    assert tickers
    assert all(t and t == t.upper() for t in tickers)
    # yfinance returns marketCap=None for "BRK.B" but real data for "BRK-B" --
    # the on-disk ticker list must already use the dash form.
    assert all("." not in t for t in tickers)
    assert "BRK-B" in tickers


def test_startup_seeds_universe_when_poller_starts(db_path):
    app = create_app(
        cfg=Config(db_path=db_path), client=InertClient(), ai_client=None,
        fred_client=InertFred(), start_poller=True,
    )
    with TestClient(app):
        pass
    with database.get_conn(db_path) as conn:
        coverage = database.universe_coverage(conn)
    # Non-self-referential floor: catches "seeding silently does nothing"
    # even in the degenerate case where _load_universe() itself regressed to
    # returning [] (which would make a purely self-referential comparison
    # pass trivially).
    assert coverage["universe"] > 100
    assert coverage["universe"] == len(_load_universe())


def test_seeding_is_idempotent_across_restarts(db_path):
    """Restarting must not re-seed duplicates or reset warming progress --
    otherwise every restart would re-warm 503 tickers against Yahoo."""

    def start_once():
        app = create_app(
            cfg=Config(db_path=db_path), client=InertClient(), ai_client=None,
            fred_client=InertFred(), start_poller=True,
        )
        with TestClient(app):
            pass

    start_once()
    with database.get_conn(db_path) as conn:
        first_count = database.universe_coverage(conn)["universe"]
        database.mark_universe_done(conn, "AAPL")

    start_once()

    with database.get_conn(db_path) as conn:
        second_count = database.universe_coverage(conn)["universe"]
        still_pending = database.pending_universe(conn, 1000)

    assert second_count == first_count
    assert "AAPL" not in still_pending


def test_missing_data_file_degrades_instead_of_crashing(monkeypatch, db_path, tmp_path):
    # _load_universe() derives its path from this module's own __file__;
    # pointing that at an empty directory reproduces "file was deleted"
    # without touching the real repo file.
    monkeypatch.setattr(main_module, "__file__", str(tmp_path / "unused" / "main.py"))

    assert _load_universe() == []

    app = create_app(
        cfg=Config(db_path=db_path), client=InertClient(), ai_client=None,
        fred_client=InertFred(), start_poller=True,
    )
    with TestClient(app) as c:
        health = c.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["universe_coverage"]["universe"] == 0
