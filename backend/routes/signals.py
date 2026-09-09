"""Read-only views of computed setups. Nothing here fetches or evaluates."""

from pathlib import Path

from fastapi import APIRouter, Depends

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/signals", tags=["signals"])


@router.get("")
async def list_signals(
    limit: int = 25, db_path: Path = Depends(get_db_path)
) -> dict:
    capped = max(1, min(int(limit), 100))
    with database.get_conn(db_path) as conn:
        rows = database.list_signals(conn, limit=capped)

    import json

    signals = []
    for r in rows:
        try:
            factors = json.loads(r["factors_json"] or "{}")
        except Exception:
            factors = {}
        # The engine records every factor with a pass/fail flag, not just the
        # ones that passed. Both are served: naming only the passing ones
        # would let a client present a partial setup as a full-confluence one.
        if isinstance(factors, dict):
            passed = sorted(k for k, v in factors.items() if v)
            failed = sorted(k for k, v in factors.items() if not v)
        else:
            passed, failed = sorted(factors or []), []
        signals.append({
            "id": r["id"], "ticker": r["ticker"], "direction": r["direction"],
            "grade": r["grade"], "score": r["score"],
            "factors": passed, "factors_failed": failed,
            "entry": r["entry"], "stop": r["stop"],
            "target1": r["target1"], "target2": r["target2"],
            "risk_reward": r["risk_reward"], "timeframes": r["timeframes"],
            "earnings_at": r["earnings_at"], "created_at": r["created_at"],
            "delivered_at": r["delivered_at"],
        })
    return {"signals": signals}
