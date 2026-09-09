"""User-defined price alerts. Stored here, evaluated by the poller."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

DIRECTIONS = ("above", "below")


class AlertIn(BaseModel):
    ticker: str = Field(min_length=1, max_length=12)
    direction: str
    price: float = Field(gt=0)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("ticker")
    @classmethod
    def normalise(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("direction")
    @classmethod
    def known_direction(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if cleaned not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        return cleaned


@router.get("")
async def list_alerts(db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        return {"alerts": database.list_price_alerts(conn)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_alert(body: AlertIn, db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        alert_id = database.add_price_alert(
            conn, body.ticker, body.direction, body.price, body.note
        )
    return {"id": alert_id, "ticker": body.ticker,
            "direction": body.direction, "price": body.price}


@router.delete("/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert(alert_id: int, db_path: Path = Depends(get_db_path)) -> Response:
    with database.get_conn(db_path) as conn:
        removed = database.delete_price_alert(conn, alert_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No alert {alert_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
