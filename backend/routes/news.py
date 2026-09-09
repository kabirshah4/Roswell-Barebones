"""Read-only news cache views. Nothing here may perform network or AI calls."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("/{ticker}")
async def get_news(ticker: str, db_path: Path = Depends(get_db_path)) -> dict:
    symbol = ticker.strip().upper()
    with database.get_conn(db_path) as conn:
        if symbol not in database.list_watchlist(conn):
            raise HTTPException(status_code=404, detail=f"{symbol} is not on the watchlist")
        rows = database.list_news(conn, symbol)

    articles = [
        {
            "id": r["id"],
            "title": r["title"],
            "publisher": r["publisher"],
            "url": r["url"],
            "published_at": r["published_at"],
            "summary": r["summary"],
            "ai_summary": r["ai_summary"],
            "sentiment": r["sentiment"],
            "link_status": r["link_status"],
            "link_code": r["link_code"],
        }
        for r in rows
    ]
    return {"ticker": symbol, "articles": articles}
