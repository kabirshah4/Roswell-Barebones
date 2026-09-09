"""OMON — the option chain for one ticker.

Fetched on demand and cached briefly. Unlike the price panels there is no
poller filling this in, and unlike the company screens a chain goes stale in
minutes rather than days, so the cache is short and a refresh is one click.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.db import database
from backend.routes import get_db_path
from backend.services import options as options_service

router = APIRouter(prefix="/api/options", tags=["options"])

# Quotes move; a chain older than this is refetched rather than served.
CACHE_SECONDS = 120


def _key(ticker: str, expiry: str) -> str:
    return f"options:{ticker}:{expiry}"


@router.get("/{ticker}/expirations")
async def expirations(
    ticker: str, request: Request, db_path: Path = Depends(get_db_path)
) -> dict:
    import asyncio

    symbol = ticker.strip().upper()
    dates = await asyncio.to_thread(
        request.app.state.client.fetch_expirations, symbol
    )
    return {"ticker": symbol, "expirations": dates}


@router.get("/{ticker}")
async def chain(
    ticker: str,
    request: Request,
    expiry: str = "",
    db_path: Path = Depends(get_db_path),
) -> dict:
    import asyncio

    symbol = ticker.strip().upper()
    client = request.app.state.client

    dates = await asyncio.to_thread(client.fetch_expirations, symbol)
    if not dates:
        # Plenty of names have no listed options; that is not an error.
        return {"ticker": symbol, "expirations": [], "expiry": None,
                "calls": [], "puts": [], "summary": None,
                "reason": f"{symbol} has no listed options."}

    chosen = expiry if expiry in dates else dates[0]

    cached = _read_cache(db_path, symbol, chosen)
    if cached is not None:
        cached["expirations"] = dates
        return cached

    data = await asyncio.to_thread(client.fetch_option_chain, symbol, chosen)

    with database.get_conn(db_path) as conn:
        prices = database.get_prices(conn, [symbol])
    # None when nothing has quoted the name. The chain still has a strike
    # ladder, but deriving a spot from the middle of it would be a guess
    # dressed as a measurement, so the summary reports unknown instead.
    spot = prices[0]["price"] if prices else None

    try:
        days = max((date.fromisoformat(chosen) - date.today()).days, 0)
    except ValueError:
        days = 0

    summary = options_service.summarise(data["calls"], data["puts"], spot, days)
    payload = {
        "ticker": symbol,
        "expirations": dates,
        "expiry": chosen,
        "days_to_expiry": days,
        "calls": data["calls"],
        "puts": data["puts"],
        "summary": vars(summary),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
    _write_cache(db_path, symbol, chosen, payload)
    return payload


def _read_cache(db_path: Path, ticker: str, expiry: str) -> dict | None:
    with database.get_conn(db_path) as conn:
        raw = database.get_setting(conn, _key(ticker, expiry))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(payload["as_of"])
        ).total_seconds()
        return payload if age < CACHE_SECONDS else None
    except Exception:
        return None


def _write_cache(db_path: Path, ticker: str, expiry: str, payload: dict) -> None:
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, _key(ticker, expiry), json.dumps(payload))
