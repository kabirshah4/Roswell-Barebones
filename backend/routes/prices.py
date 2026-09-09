"""Read-only cache views.

Nothing here may perform network I/O. External call volume must stay a
function of the poller alone, no matter how many clients poll these routes.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/prices", tags=["prices"])


@router.get("")
async def list_prices(db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        tickers = database.list_watchlist(conn)
        cached = {row["ticker"]: row for row in database.get_prices(conn, tickers)}

    prices = []
    for ticker in tickers:
        row = cached.get(ticker)
        if row is None:
            # On the watchlist but not yet polled: surface it as stale rather
            # than hiding it, so the UI shows the row immediately after adding.
            prices.append(
                {
                    "ticker": ticker,
                    "price": None,
                    "change_pct": None,
                    "volume": None,
                    "prev_close": None,
                    "currency": None,
                    "fetched_at": None,
                    "is_stale": True,
                }
            )
        else:
            prices.append(
                {
                    "ticker": row["ticker"],
                    "price": row["price"],
                    "change_pct": row["change_pct"],
                    "volume": row["volume"],
                    "prev_close": row["prev_close"],
                    "currency": row["currency"],
                    "fetched_at": row["fetched_at"],
                    "is_stale": bool(row["is_stale"]),
                }
            )
    return {"prices": prices}


@router.get("/{ticker}/sparkline")
async def get_sparkline(ticker: str, db_path: Path = Depends(get_db_path)) -> dict:
    symbol = ticker.strip().upper()
    with database.get_conn(db_path) as conn:
        if symbol not in database.list_watchlist(conn):
            raise HTTPException(status_code=404, detail=f"{symbol} is not on the watchlist")
        points = database.get_sparkline(conn, symbol)
    return {"ticker": symbol, "points": points or []}
