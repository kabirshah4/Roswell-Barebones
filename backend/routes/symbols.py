"""Search the cached symbol catalogue. Never fetches."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


@router.get("/search")
async def search(
    q: str = "", limit: int = 20, kind: str = "",
    db_path: Path = Depends(get_db_path),
) -> dict:
    with database.get_conn(db_path) as conn:
        results = database.search_symbols(conn, q, limit, kind.strip().lower() or None)
    return {"query": q, "results": results}


@router.get("/browse")
async def browse(
    offset: int = 0,
    limit: int = 50,
    kind: str = "",
    db_path: Path = Depends(get_db_path),
) -> dict:
    """A page of the full catalogue. Backs the click-to-browse dropdown."""
    kind_filter = kind.strip().lower() or None
    with database.get_conn(db_path) as conn:
        results = database.browse_symbols(conn, offset, limit, kind_filter)
        total = database.symbol_count(conn, kind_filter)
    return {
        "results": results,
        "offset": offset,
        "total": total,
        # Saves the client guessing from a short page whether more remain.
        "has_more": offset + len(results) < total,
    }


@router.get("/status")
async def status(db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        return {"count": database.symbol_count(conn)}


@router.post("/refresh")
async def refresh(request: Request, db_path: Path = Depends(get_db_path)) -> dict:
    """Re-pull the catalogue on demand.

    The one write route that reaches the network, alongside POST /api/watchlist
    and POST /api/chat. It is explicitly user-triggered, so the cost is visible
    rather than hidden in a page load.
    """
    import asyncio

    client = request.app.state.screener_client
    if not client.enabled:
        with database.get_conn(db_path) as conn:
            return {"count": database.symbol_count(conn), "refreshed": False,
                    "reason": "no_screener_source"}

    symbols = await asyncio.to_thread(client.fetch_symbols)
    if not symbols:
        with database.get_conn(db_path) as conn:
            return {"count": database.symbol_count(conn), "refreshed": False}
    with database.get_conn(db_path) as conn:
        stored = database.replace_symbols(conn, symbols)
    return {"count": stored, "refreshed": True}
