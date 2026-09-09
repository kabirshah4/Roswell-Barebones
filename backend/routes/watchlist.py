import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator

from backend.db import database
from backend.routes import get_client, get_db_path

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

# Real symbols use letters/digits plus '.' (e.g. BRK.B) and '-' (e.g. BF-B).
# Nothing else is a valid ticker, and this also keeps unsanitised characters
# (spaces, quotes, slashes, etc.) out of anything downstream that renders a
# ticker into HTML.
_TICKER_RE = re.compile(r"^[A-Z0-9.\-]+$")


class TickerIn(BaseModel):
    ticker: str

    @field_validator("ticker")
    @classmethod
    def normalise(cls, v: str) -> str:
        cleaned = v.strip().upper()
        if not cleaned:
            raise ValueError("ticker must not be blank")
        if not _TICKER_RE.fullmatch(cleaned):
            raise ValueError(
                f"ticker may only contain letters, digits, '.', and '-': {v!r}"
            )
        return cleaned


@router.get("")
async def list_tickers(db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        return {"tickers": database.list_watchlist(conn)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_ticker(
    body: TickerIn,
    request: Request,
    db_path: Path = Depends(get_db_path),
    client: Any = Depends(get_client),
) -> dict:
    """Validate against the upstream source before persisting.

    This is the one route permitted to touch the network: inserting a symbol
    that will never price would leave a permanently stale row in the UI.

    A rate-limited or otherwise unreachable upstream is not the same failure
    as a symbol that genuinely doesn't exist, so the two are reported with
    different status codes: 400 means "this ticker is bad", 503 means "we
    couldn't check — try again shortly".

    ``check_ticker`` does real (blocking) network I/O, so it is offloaded to
    a worker thread rather than called directly, matching the pattern
    ``price_poller`` already uses — otherwise this single request would stall
    the whole event loop (other routes, the background poller) for the
    ~1-1.5s a Yahoo round trip takes. See the comment on
    ``add_watchlist_ticker`` in ``backend/db/database.py`` for the one
    behavioural consequence of that: it opens a genuine await point before
    this route reaches the database.
    """
    result = await asyncio.to_thread(client.check_ticker, body.ticker)
    if result == "unavailable":
        raise HTTPException(
            status_code=503,
            detail="Upstream price data unavailable, try again shortly",
        )
    if result != "ok":
        raise HTTPException(
            status_code=400, detail=f"Unknown or unpriced ticker: {body.ticker}"
        )
    try:
        with database.get_conn(db_path) as conn:
            database.add_watchlist_ticker(conn, body.ticker)
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409, detail=f"{body.ticker} is already on the watchlist"
        )

    # Fetch everything for it now rather than leaving the panels empty until
    # the next cadence -- up to an hour for fundamentals. Scheduled as a
    # background task so this response stays as fast as the validation above.
    # Gated on the poller actually running: a caller that opted out of
    # background work (tests, one-off scripts) has also opted out of the
    # network, and a warm there would fetch behind their back.
    poller = getattr(request.app.state, "poller", None)
    running = getattr(request.app.state, "poller_task", None) is not None
    warming = False
    if running and poller is not None and hasattr(poller, "warm_ticker"):
        try:
            asyncio.create_task(poller.warm_ticker(body.ticker))
            warming = True
        except RuntimeError:
            # No running loop (a sync test client outside async context).
            # The ticker is still added; the cadence will fill it in.
            pass

    return {"ticker": body.ticker, "warming": warming}


@router.delete("/{ticker}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_ticker(
    ticker: str, db_path: Path = Depends(get_db_path)
) -> Response:
    with database.get_conn(db_path) as conn:
        removed = database.remove_watchlist_ticker(conn, ticker.strip().upper())
    if not removed:
        raise HTTPException(status_code=404, detail=f"{ticker} is not on the watchlist")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
