"""Read-only fundamentals cache view. No network I/O."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/fundamentals", tags=["fundamentals"])

# Derived from the storage layer rather than repeated here. A second hand-kept
# list is exactly the thing that goes stale: adding twenty-one extended fields
# to the schema left this whitelist quietly serving the original eleven.
_FIELDS = database._FUNDAMENTAL_FIELDS + ("updated_at", "is_stale")


@router.get("/{ticker}")
async def get_fundamentals(ticker: str, db_path: Path = Depends(get_db_path)) -> dict:
    symbol = ticker.strip().upper()
    with database.get_conn(db_path) as conn:
        if symbol not in database.list_watchlist(conn):
            raise HTTPException(status_code=404, detail=f"{symbol} is not on the watchlist")
        row = database.get_fundamentals(conn, symbol)

    payload = {name: row[name] for name in _FIELDS} if row else None
    return {"ticker": symbol, "fundamentals": payload}
