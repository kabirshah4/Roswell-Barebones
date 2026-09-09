"""The factor study. Cached, because a full replay is minutes of CPU."""

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/factors", tags=["factors"])

CACHE_KEY = "factor_study"


@router.get("")
async def read_study(db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        raw = database.get_setting(conn, CACHE_KEY)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"factors": [], "by_score": [], "resolved": 0, "as_of": None}


@router.post("/refresh")
async def refresh_study(db_path: Path = Depends(get_db_path)) -> dict:
    """Replay every cached ticker. Explicitly user-triggered: it is minutes
    of CPU, and nothing about it needs to be current to the second."""
    import asyncio

    payload = await asyncio.to_thread(_run, db_path)
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, CACHE_KEY, json.dumps(payload))
    return payload


def _run(db_path: Path) -> dict:
    import pandas as pd

    from backend.services import factor_study

    with database.get_conn(db_path) as conn:
        tickers = [
            r["ticker"] for r in conn.execute(
                "SELECT DISTINCT ticker FROM bars WHERE interval = '1d'"
            )
        ]
        results = []
        for ticker in tickers:
            rows = database.get_bars(conn, ticker, "1d", limit=3000)
            if len(rows) < 250:
                continue
            frame = pd.DataFrame(rows)
            frame.index = pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
            frame = frame.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume"})
            results.append(factor_study.study_frame(ticker, frame))

    payload = factor_study.combine(results)
    payload["tickers"] = len(results)
    payload["as_of"] = datetime.now(timezone.utc).isoformat()
    return payload
