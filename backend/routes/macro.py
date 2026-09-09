"""Read-only macro calendar view. No network I/O."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/macro", tags=["macro"])


def get_fred(request: Request) -> Any:
    return request.app.state.fred_client


def get_poller(request: Request) -> Any:
    return request.app.state.poller


@router.get("/calendar")
async def calendar(
    db_path: Path = Depends(get_db_path),
    fred: Any = Depends(get_fred),
    poller: Any = Depends(get_poller),
) -> dict:
    with database.get_conn(db_path) as conn:
        events = database.list_macro_events(conn)
    # Cached events are always served -- the panel never blanks on a failed
    # refresh -- but a failure is reported so stale dates are not presented
    # as current ones.
    return {
        "events": events,
        "fred_enabled": bool(getattr(fred, "enabled", False)),
        "is_stale": bool(getattr(poller, "macro_stale", False)),
        "last_refresh": getattr(poller, "macro_last_success", None),
    }


@router.get("/yields")
async def yields(db_path: Path = Depends(get_db_path)) -> dict:
    """WB — the Treasury curve as last cached.

    Serves SQLite like every other read route; the poller refreshes it. These
    are daily closes, so a cache measured in hours costs nothing.
    """
    import json

    with database.get_conn(db_path) as conn:
        raw = database.get_setting(conn, "yield_curve")
    if not raw:
        return {"points": [], "as_of": None,
                "reason": "No curve cached yet. It needs a FRED key, and "
                          "arrives on the next poll cycle."}
    try:
        payload = json.loads(raw)
    except Exception:
        return {"points": [], "as_of": None, "reason": "Cached curve unreadable."}

    points = payload.get("points", [])
    by_label = {p["label"]: p["percent"] for p in points}
    return {
        **payload,
        "reason": None,
        # The two spreads people actually quote. Inversion is the reason
        # anyone looks at this screen, so it is computed rather than left for
        # the reader to subtract in their head.
        "spread_10y_2y": round(by_label["10Y"] - by_label["2Y"], 2)
        if {"10Y", "2Y"} <= by_label.keys() else None,
        "spread_10y_3m": round(by_label["10Y"] - by_label["3M"], 2)
        if {"10Y", "3M"} <= by_label.keys() else None,
    }
