"""Filter engine over cached fundamentals.

Pure SQL: no network, no imports from other service modules. Phase 5's
natural-language screener translates language into a `Filters` instance and
calls straight into `run_screen`.
"""

import sqlite3
from dataclasses import dataclass


class FilterError(ValueError):
    """Raised when a filter set is self-contradictory."""


@dataclass(frozen=True)
class Filters:
    pe_min: float | None = None
    pe_max: float | None = None
    market_cap_min: float | None = None
    market_cap_max: float | None = None
    dividend_yield_min: float | None = None
    sector: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    limit: int = 100


_RANGES = (
    ("pe_min", "pe_max", "P/E"),
    ("market_cap_min", "market_cap_max", "market cap"),
    ("price_min", "price_max", "price"),
)


def validate(filters: Filters) -> None:
    """Reject contradictory ranges loudly rather than returning nothing."""
    for lo_name, hi_name, label in _RANGES:
        lo = getattr(filters, lo_name)
        hi = getattr(filters, hi_name)
        if lo is not None and hi is not None and lo > hi:
            raise FilterError(f"{label} minimum ({lo}) exceeds maximum ({hi})")


def run_screen(conn: sqlite3.Connection, filters: Filters) -> list[dict]:
    """Return matching tickers. Every user value is bound, never interpolated."""
    clauses: list[str] = []
    params: list[object] = []

    def add(sql: str, value: object) -> None:
        clauses.append(sql)
        params.append(value)

    if filters.pe_min is not None:
        add("f.pe_ratio >= ?", filters.pe_min)
    if filters.pe_max is not None:
        add("f.pe_ratio <= ?", filters.pe_max)
    if filters.market_cap_min is not None:
        add("f.market_cap >= ?", filters.market_cap_min)
    if filters.market_cap_max is not None:
        add("f.market_cap <= ?", filters.market_cap_max)
    if filters.dividend_yield_min is not None:
        add("f.dividend_yield >= ?", filters.dividend_yield_min)
    if filters.sector is not None:
        add("f.sector = ?", filters.sector)
    if filters.price_min is not None:
        add("p.price >= ?", filters.price_min)
    if filters.price_max is not None:
        add("p.price <= ?", filters.price_max)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT f.ticker, f.pe_ratio, f.market_cap, f.dividend_yield,
               f.sector, p.price
        FROM fundamentals f
        LEFT JOIN price_cache p ON p.ticker = f.ticker
        {where}
        ORDER BY f.market_cap DESC NULLS LAST, f.ticker
        LIMIT ?
        """,
        (*params, filters.limit),
    ).fetchall()
    return [dict(r) for r in rows]
