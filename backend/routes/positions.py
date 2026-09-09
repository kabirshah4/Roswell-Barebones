"""Positions taken, and what they add up to.

The watchlist is a list of things to look at. This is a list of things you own,
and it is what lets the terminal answer "how am I doing" with a measurement
instead of a feeling.

Reads serve SQLite. The only outside number is the last cached quote, used to
mark open positions to market — which is itself a read.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.db import database
from backend.routes import get_db_path
from backend.services import journal as journal_service
from backend.services import sizing as sizing_service

router = APIRouter(prefix="/api/positions", tags=["positions"])

# Defaults until the user sets their own in settings. A 1% risk is the
# convention, not a recommendation — it is simply the number most position
# sizing is taught with.
DEFAULT_ACCOUNT = 10_000.0
DEFAULT_RISK_PCT = 1.0


class OpenPosition(BaseModel):
    ticker: str
    shares: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    direction: str = "long"
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    plan_grade: str | None = None
    plan_score: int | None = None
    plan_horizon: str | None = None
    fees: float = 0
    note: str | None = None
    entry_at: str | None = None


class ClosePosition(BaseModel):
    exit_price: float = Field(gt=0)
    exit_reason: str | None = None
    fees: float = 0
    exit_at: str | None = None


class AdjustPosition(BaseModel):
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    note: str | None = None


def _account(conn) -> tuple[float, float]:
    account = database.get_setting(conn, "account_size")
    risk = database.get_setting(conn, "risk_pct")
    try:
        account = float(account) if account else DEFAULT_ACCOUNT
    except ValueError:
        account = DEFAULT_ACCOUNT
    try:
        risk = float(risk) if risk else DEFAULT_RISK_PCT
    except ValueError:
        risk = DEFAULT_RISK_PCT
    return account, risk


@router.get("")
async def positions(db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        rows = database.list_positions(conn)
        account, risk_pct = _account(conn)
        tickers = sorted({r["ticker"] for r in rows if r["exit_at"] is None})
        quotes = {q["ticker"]: q["price"] for q in database.get_prices(conn, tickers)}

    open_rows, closed_rows = [], []
    exposure = 0.0
    unrealised = 0.0
    open_risk = 0.0
    for row in rows:
        item = dict(row)
        if row["exit_at"] is None:
            price = quotes.get(row["ticker"])
            item["last_price"] = price
            item["open_pnl"] = journal_service.open_pnl(row, price)
            item["market_value"] = price * row["shares"] if price else None
            # What is still on the table if every open stop is hit from here.
            if row["stop"] is not None:
                item["risk_remaining"] = round(
                    abs(row["entry_price"] - row["stop"]) * row["shares"], 2
                )
                open_risk += item["risk_remaining"]
            else:
                item["risk_remaining"] = None
            item["planned_r"] = journal_service.planned_r(row)
            exposure += item["market_value"] or 0
            unrealised += item["open_pnl"] or 0
            open_rows.append(item)
        else:
            item["realised_r"] = journal_service.realised_r(row)
            item["pnl"] = journal_service.pnl(row)
            item["hold_days"] = journal_service.hold_days(row)
            closed_rows.append(item)

    stats = journal_service.summarise(rows)
    return {
        "open": open_rows,
        "closed": closed_rows,
        "account_size": account,
        "risk_pct": risk_pct,
        "exposure": round(exposure, 2),
        "exposure_pct": round(exposure / account * 100, 1) if account else None,
        "unrealised_pnl": round(unrealised, 2),
        # The number that matters when deciding whether to take one more: what
        # you lose if every open stop is hit today.
        "open_risk": round(open_risk, 2),
        "open_risk_pct": round(open_risk / account * 100, 2) if account else None,
        "journal": vars(stats),
    }


@router.post("", status_code=201)
async def open_position(
    body: OpenPosition, db_path: Path = Depends(get_db_path)
) -> dict:
    if body.stop is not None:
        if body.direction == "long" and body.stop >= body.entry_price:
            raise HTTPException(
                400, "A long's stop has to sit below the entry."
            )
        if body.direction == "short" and body.stop <= body.entry_price:
            raise HTTPException(
                400, "A short's stop has to sit above the entry."
            )
    with database.get_conn(db_path) as conn:
        position_id = database.add_position(conn, **body.model_dump())
        return database.get_position(conn, position_id)


@router.post("/{position_id}/close")
async def close(
    position_id: int, body: ClosePosition, db_path: Path = Depends(get_db_path)
) -> dict:
    with database.get_conn(db_path) as conn:
        if not database.close_position(
            conn, position_id, body.exit_price, body.exit_reason,
            body.exit_at, body.fees,
        ):
            raise HTTPException(404, "No open position with that id.")
        return database.get_position(conn, position_id)


@router.patch("/{position_id}")
async def adjust(
    position_id: int, body: AdjustPosition, db_path: Path = Depends(get_db_path)
) -> dict:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    with database.get_conn(db_path) as conn:
        if not database.update_position(conn, position_id, **changes):
            raise HTTPException(404, "No open position with that id.")
        return database.get_position(conn, position_id)


@router.delete("/{position_id}", status_code=204)
async def remove(position_id: int, db_path: Path = Depends(get_db_path)) -> None:
    with database.get_conn(db_path) as conn:
        if not database.delete_position(conn, position_id):
            raise HTTPException(404, "No position with that id.")


@router.get("/size")
async def size(
    entry: float, stop: float, risk_pct: float | None = None,
    account: float | None = None, db_path: Path = Depends(get_db_path),
) -> dict:
    """How many shares, for a plan's entry and stop."""
    with database.get_conn(db_path) as conn:
        stored_account, stored_risk = _account(conn)
    account = account if account is not None else stored_account
    risk_pct = risk_pct if risk_pct is not None else stored_risk

    result = sizing_service.size(account, risk_pct, entry, stop)
    if result is None:
        raise HTTPException(
            400,
            "Cannot size that: the entry and stop must differ and both be "
            "positive, with a positive account and risk.",
        )
    return {"account_size": account, "risk_pct": risk_pct, **vars(result)}
