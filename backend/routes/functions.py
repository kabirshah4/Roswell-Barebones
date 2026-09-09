"""Function directory, command parsing, and the data behind the new screens."""

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from backend.db import database
from backend.routes import get_db_path
from backend.services import functions as fn

router = APIRouter(prefix="/api/functions", tags=["functions"])


@router.get("")
async def directory() -> dict:
    """MAIN — everything this terminal can open, plus what it deliberately cannot."""
    return {
        "categories": [
            {
                "name": category,
                "functions": [
                    {
                        "code": f.code, "name": f.name,
                        "needs_ticker": f.needs_ticker, "summary": f.summary,
                    }
                    for f in fn.FUNCTIONS if f.category == category
                ],
            }
            for category in fn.CATEGORIES
        ],
        "unavailable": [
            {"code": code, "reason": reason} for code, reason in fn.UNAVAILABLE
        ],
    }


@router.get("/parse")
async def parse_command(q: str = "") -> dict:
    """Resolve a typed command into a ticker and a function."""
    ticker, code = fn.parse(q)
    known = fn.BY_CODE.get(code or "")
    return {
        "ticker": ticker,
        "code": code,
        "valid": known is not None,
        "name": known.name if known else None,
        "needs_ticker": known.needs_ticker if known else False,
        "suggestions": [
            {"code": f.code, "name": f.name, "needs_ticker": f.needs_ticker}
            for f in fn.suggest(q.split()[-1] if q.split() else "")
        ],
    }


# --- company data (DES / FA / EE / ANR / HDS) --------------------------------

def _cache_key(ticker: str) -> str:
    return f"company:{ticker.upper()}"


@router.get("/company/{ticker}")
async def company(
    ticker: str, request: Request, db_path: Path = Depends(get_db_path)
) -> dict:
    """Statements, estimates, ratings and holders for one company.

    Cached, and fetched on a miss — unlike the read-only panels, a function
    screen has no poller filling it in ahead of time, and an empty screen with
    "try again in an hour" is not an answer. The fetch takes about a second.
    """
    symbol = ticker.strip().upper()
    with database.get_conn(db_path) as conn:
        raw = database.get_setting(conn, _cache_key(symbol))
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass

    import asyncio

    client = request.app.state.client
    try:
        data = await asyncio.to_thread(client.fetch_company, symbol)
    except Exception:
        data = {}
    payload = {"ticker": symbol, "as_of": datetime.now(timezone.utc).isoformat(),
               **data}
    if data:
        with database.get_conn(db_path) as conn:
            database.set_setting(conn, _cache_key(symbol), json.dumps(payload))
    return payload


@router.post("/company/{ticker}/refresh")
async def refresh_company(
    ticker: str, request: Request, db_path: Path = Depends(get_db_path)
) -> dict:
    symbol = ticker.strip().upper()
    with database.get_conn(db_path) as conn:
        database.delete_setting(conn, _cache_key(symbol))
    return await company(symbol, request, db_path)


# --- IMAP --------------------------------------------------------------------

@router.get("/sectors")
async def sectors(request: Request, db_path: Path = Depends(get_db_path)) -> dict:
    """Cap-weighted sector performance across the market.

    Weighted by market cap, not averaged: an equal-weight average lets a
    handful of micro caps swing a sector that trillions of dollars barely
    moved.
    """
    with database.get_conn(db_path) as conn:
        raw = database.get_setting(conn, "sector_map")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"sectors": [], "as_of": None}


@router.post("/sectors/refresh")
async def refresh_sectors(
    request: Request, db_path: Path = Depends(get_db_path)
) -> dict:
    import asyncio

    scanner = request.app.state.scanner
    payload = await asyncio.to_thread(_sector_snapshot, scanner)
    if payload["sectors"]:
        with database.get_conn(db_path) as conn:
            database.set_setting(conn, "sector_map", json.dumps(payload))
    return payload


def _sector_snapshot(scanner) -> dict:
    from collections import defaultdict

    from backend.services.screener_client import scanner_url

    payload = {
        "filter": [
            {"left": "type", "operation": "equal", "right": "stock"},
            {"left": "market_cap_basic", "operation": "greater",
             "right": 2_000_000_000},
        ],
        "columns": ["name", "sector", "change", "market_cap_basic"],
        "range": [0, 3000],
    }
    url = scanner_url("america")
    if url is None:
        return {"sectors": [], "as_of": None}

    try:
        body = scanner._poster(url, payload)
    except Exception:
        return {"sectors": [], "as_of": None}

    weighted: dict = defaultdict(lambda: {"w": 0.0, "cap": 0.0, "n": 0})
    for row in (body or {}).get("data") or []:
        try:
            _, sector, change, cap = row["d"]
            if not sector or change is None or not cap:
                continue
            bucket = weighted[sector]
            bucket["w"] += float(change) * float(cap)
            bucket["cap"] += float(cap)
            bucket["n"] += 1
        except Exception:
            continue

    sectors = [
        {
            "sector": name,
            "change": round(v["w"] / v["cap"], 3) if v["cap"] else 0.0,
            "market_cap": v["cap"],
            "constituents": v["n"],
        }
        for name, v in weighted.items()
    ]
    sectors.sort(key=lambda s: s["change"], reverse=True)
    return {
        "sectors": sectors,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "constituents": sum(s["constituents"] for s in sectors),
    }


# --- WB : Treasury yields ----------------------------------------------------

# FRED series for the benchmark curve. Verified against FRED's own catalogue.
TREASURY_SERIES = (
    ("DGS1MO", "1 Month"), ("DGS3MO", "3 Month"), ("DGS6MO", "6 Month"),
    ("DGS1", "1 Year"), ("DGS2", "2 Year"), ("DGS5", "5 Year"),
    ("DGS7", "7 Year"), ("DGS10", "10 Year"), ("DGS20", "20 Year"),
    ("DGS30", "30 Year"),
)


@router.get("/yields")
async def yields(request: Request, db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        raw = database.get_setting(conn, "treasury_yields")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    fred = getattr(request.app.state, "fred_client", None)
    return {
        "curve": [], "as_of": None,
        "fred_enabled": bool(getattr(fred, "enabled", False)),
    }


# --- NSE : news search -------------------------------------------------------

@router.get("/news-search")
async def news_search(
    q: str = "", limit: int = 40, db_path: Path = Depends(get_db_path)
) -> dict:
    """Search cached headlines across every ticker. SQLite only."""
    query = (q or "").strip()
    with database.get_conn(db_path) as conn:
        if not query:
            rows = conn.execute(
                "SELECT * FROM news ORDER BY published_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        else:
            like = f"%{query}%"
            rows = conn.execute(
                """
                SELECT * FROM news
                WHERE title LIKE ? OR summary LIKE ? OR ai_summary LIKE ?
                   OR ticker LIKE ?
                ORDER BY published_at DESC LIMIT ?
                """,
                (like, like, like, like, max(1, min(limit, 200))),
            ).fetchall()
    return {
        "query": query,
        "articles": [
            {
                "id": r["id"], "ticker": r["ticker"], "title": r["title"],
                "publisher": r["publisher"], "url": r["url"],
                "published_at": r["published_at"], "ai_summary": r["ai_summary"],
                "sentiment": r["sentiment"], "link_status": r["link_status"],
            }
            for r in rows
        ],
    }


# --- PORT --------------------------------------------------------------------

@router.get("/portfolio")
async def portfolio(db_path: Path = Depends(get_db_path)) -> dict:
    """Watchlist exposure by sector and weight.

    Equal-weighted by construction: the terminal tracks a watchlist, not
    positions, so it has no share counts. Stated rather than implied, because a
    weight column that looks like real allocation would be a fiction.
    """
    with database.get_conn(db_path) as conn:
        tickers = database.list_watchlist(conn)
        prices = {p["ticker"]: p for p in database.get_prices(conn, tickers)}
        holdings = []
        for ticker in tickers:
            fundamentals = database.get_fundamentals(conn, ticker) or {}
            quote = prices.get(ticker, {})
            holdings.append({
                "ticker": ticker,
                "price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
                "sector": fundamentals.get("sector"),
                "beta": fundamentals.get("beta"),
                "market_cap": fundamentals.get("market_cap"),
            })

    weight = round(100.0 / len(holdings), 2) if holdings else 0.0
    betas = [h["beta"] for h in holdings if h["beta"] is not None]
    moves = [h["change_pct"] for h in holdings if h["change_pct"] is not None]
    exposure: dict = {}
    for holding in holdings:
        exposure[holding["sector"] or "Unknown"] = round(
            exposure.get(holding["sector"] or "Unknown", 0.0) + weight, 2
        )

    return {
        "holdings": holdings,
        "equal_weight_pct": weight,
        "weighting": "equal",
        "portfolio_beta": round(sum(betas) / len(betas), 3) if betas else None,
        "day_change_pct": round(sum(moves) / len(moves), 3) if moves else None,
        "sector_exposure": sorted(
            ({"sector": k, "weight_pct": v} for k, v in exposure.items()),
            key=lambda s: -s["weight_pct"],
        ),
    }
