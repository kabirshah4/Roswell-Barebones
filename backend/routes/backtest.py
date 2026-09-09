"""Cached backtest results. Replays happen in the poller, never in a route."""

from pathlib import Path

from fastapi import APIRouter, Depends

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.get("")
async def all_stats(db_path: Path = Depends(get_db_path)) -> dict:
    """Every ticker's measured record, so the UI can annotate any grade."""
    with database.get_conn(db_path) as conn:
        return {"stats": database.all_backtest_stats(conn)}


@router.get("/{ticker}")
async def stats_for(ticker: str, db_path: Path = Depends(get_db_path)) -> dict:
    symbol = ticker.strip().upper()
    with database.get_conn(db_path) as conn:
        stats = database.get_backtest_stats(conn, symbol)
    return {"ticker": symbol, "stats": stats}
