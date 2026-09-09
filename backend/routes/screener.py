"""Screener query and preset routes. SQL only — no network I/O."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from backend.db import database
from backend.routes import get_db_path
from backend.services import screener as engine

router = APIRouter(prefix="/api/screener", tags=["screener"])


class ScreenRequest(BaseModel):
    pe_min: float | None = None
    pe_max: float | None = None
    market_cap_min: float | None = None
    market_cap_max: float | None = None
    dividend_yield_min: float | None = None
    sector: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    limit: int = Field(default=100, ge=1, le=500)


class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    filters: dict


@router.post("")
async def run(body: ScreenRequest, db_path: Path = Depends(get_db_path)) -> dict:
    filters = engine.Filters(**body.model_dump())
    try:
        engine.validate(filters)
    except engine.FilterError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with database.get_conn(db_path) as conn:
        matches = engine.run_screen(conn, filters)
        coverage = database.universe_coverage(conn)
    return {"matches": matches, "coverage": coverage}


@router.get("/presets")
async def get_presets(db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        return {"presets": database.list_presets(conn)}


@router.post("/presets", status_code=status.HTTP_201_CREATED)
async def add_preset(body: PresetIn, db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        database.save_preset(conn, body.name.strip(), json.dumps(body.filters))
    return {"name": body.name.strip()}


@router.delete("/presets/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_preset(name: str, db_path: Path = Depends(get_db_path)) -> Response:
    with database.get_conn(db_path) as conn:
        removed = database.delete_preset(conn, name.strip())
    if not removed:
        raise HTTPException(status_code=404, detail=f"No preset named {name}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
