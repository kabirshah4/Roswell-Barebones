import pytest

from backend.db import database
from backend.services.screener import Filters, FilterError, run_screen, validate


def seed(conn, ticker, pe=None, mcap=None, div=None, sector=None, price=None):
    database.upsert_fundamentals(
        conn, ticker, pe_ratio=pe, market_cap=mcap, dividend_yield=div, sector=sector
    )
    if price is not None:
        database.upsert_price(conn, ticker, price, price, 0.0, 1, "USD")


def test_empty_filters_returns_everything(conn):
    seed(conn, "AAPL", pe=30.0)
    seed(conn, "MSFT", pe=25.0)
    assert len(run_screen(conn, Filters())) == 2


def test_pe_max_filters(conn):
    seed(conn, "CHEAP", pe=10.0)
    seed(conn, "RICH", pe=50.0)
    assert [r["ticker"] for r in run_screen(conn, Filters(pe_max=20.0))] == ["CHEAP"]


def test_pe_min_filters(conn):
    seed(conn, "CHEAP", pe=10.0)
    seed(conn, "RICH", pe=50.0)
    assert [r["ticker"] for r in run_screen(conn, Filters(pe_min=20.0))] == ["RICH"]


def test_pe_range_is_inclusive_at_boundaries(conn):
    seed(conn, "EDGE", pe=20.0)
    assert len(run_screen(conn, Filters(pe_min=20.0, pe_max=20.0))) == 1


def test_null_pe_is_excluded_when_pe_filter_present(conn):
    """A ticker with no P/E must not match a P/E filter — SQL NULL semantics
    would otherwise silently drop it, but we assert the behaviour explicitly."""
    seed(conn, "NOPE", pe=None, mcap=1e9)
    assert run_screen(conn, Filters(pe_max=100.0)) == []
    assert len(run_screen(conn, Filters())) == 1


def test_market_cap_filters(conn):
    seed(conn, "SMALL", mcap=1e9)
    seed(conn, "BIG", mcap=1e12)
    assert [r["ticker"] for r in run_screen(conn, Filters(market_cap_min=1e11))] == ["BIG"]


def test_sector_filter_is_exact(conn):
    seed(conn, "AAPL", sector="Technology")
    seed(conn, "XOM", sector="Energy")
    assert [r["ticker"] for r in run_screen(conn, Filters(sector="Energy"))] == ["XOM"]


def test_dividend_yield_min(conn):
    seed(conn, "NODIV", div=0.0)
    seed(conn, "DIV", div=3.5)
    assert [r["ticker"] for r in run_screen(conn, Filters(dividend_yield_min=1.0))] == ["DIV"]


def test_price_filter_uses_price_cache(conn):
    seed(conn, "LOW", pe=10.0, price=5.0)
    seed(conn, "HIGH", pe=10.0, price=500.0)
    assert [r["ticker"] for r in run_screen(conn, Filters(price_max=100.0))] == ["LOW"]


def test_combined_filters_are_anded(conn):
    seed(conn, "MATCH", pe=15.0, mcap=5e11, sector="Technology")
    seed(conn, "WRONG_SECTOR", pe=15.0, mcap=5e11, sector="Energy")
    seed(conn, "WRONG_PE", pe=80.0, mcap=5e11, sector="Technology")
    got = [r["ticker"] for r in run_screen(
        conn, Filters(pe_max=20.0, market_cap_min=1e11, sector="Technology")
    )]
    assert got == ["MATCH"]


def test_limit_is_respected(conn):
    for i in range(10):
        seed(conn, f"T{i}", pe=10.0)
    assert len(run_screen(conn, Filters(limit=3))) == 3


def test_empty_universe_returns_empty_not_error(conn):
    assert run_screen(conn, Filters(pe_max=20.0)) == []


def test_validate_rejects_inverted_pe_range():
    with pytest.raises(FilterError):
        validate(Filters(pe_min=50.0, pe_max=10.0))


def test_validate_rejects_inverted_market_cap_range():
    with pytest.raises(FilterError):
        validate(Filters(market_cap_min=1e12, market_cap_max=1e9))


def test_validate_accepts_sane_filters():
    validate(Filters(pe_min=10.0, pe_max=50.0))


def test_no_sql_injection_via_sector(conn):
    """Sector is user-supplied; it must be bound, never interpolated."""
    seed(conn, "AAPL", sector="Technology")
    evil = "Technology'; DROP TABLE fundamentals; --"
    assert run_screen(conn, Filters(sector=evil)) == []
    assert len(run_screen(conn, Filters())) == 1  # table survived
