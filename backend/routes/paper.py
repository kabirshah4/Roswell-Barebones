"""The forward test's results.

Reads SQLite only. The runner lives on the poller — this just reports.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends

from backend.db import database
from backend.routes import get_db_path
from backend.services import journal as journal_service
from backend.services import paper as paper_service

router = APIRouter(prefix="/api/paper", tags=["paper"])


@router.get("")
async def results(db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        rows = database.paper_trades(conn)
        tickers = sorted({r["ticker"] for r in rows if r["exit_at"] is None})
        quotes = {q["ticker"]: q["price"]
                  for q in database.get_prices(conn, tickers)}

    armed, open_, closed, dropped = [], [], [], []
    for row in rows:
        item = dict(row)
        item["factors"] = _factors(row)
        if row["exit_at"] and row["exit_price"] is None:
            # Expired or unviable: no fill, so no result. Kept because the
            # fill rate is part of the honest picture — a strategy whose
            # entries rarely trade is not the same strategy on paper.
            dropped.append(item)
        elif row["exit_at"]:
            item["realised_r"] = _r(row)
            closed.append(item)
        elif row["entry_at"]:
            price = quotes.get(row["ticker"])
            item["last_price"] = price
            item["open_r"] = _r({**row, "exit_price": price}) if price else None
            open_.append(item)
        else:
            armed.append(item)

    # journal.summarise speaks the positions vocabulary; paper rows are the
    # same shape under different column names.
    stats = journal_service.summarise([
        {"entry_price": r["entry_price"], "stop": r["stop"],
         "target1": r["target1"], "exit_price": r["exit_price"],
         "exit_at": r["exit_at"], "entry_at": r["entry_at"],
         "direction": r["direction"], "shares": r["shares"],
         "fees": r["fees"], "plan_grade": r["plan_grade"]}
        for r in rows if r["exit_price"] is not None
    ])

    filled = len(closed) + len(open_)
    return {
        "armed": armed,
        "open": open_,
        "closed": closed[:60],
        "dropped": dropped[:20],
        "counts": {
            "armed": len(armed), "open": len(open_),
            "closed": len(closed), "dropped": len(dropped),
        },
        # A plan that never trades is not a loss, but a strategy whose entries
        # rarely fill is not the strategy the backtest measured either.
        "fill_rate": round(filled / (filled + len(dropped)) * 100, 1)
        if (filled + len(dropped)) else None,
        "gapped_exits": sum(1 for r in rows if r["exit_gapped"]),
        "journal": vars(stats),
        "caveats": list(paper_service.CAVEATS),
    }


def _factors(row: dict) -> list:
    try:
        return json.loads(row["factors"]) if row["factors"] else []
    except Exception:
        return []


def _r(row: dict) -> float | None:
    entry, stop, exit_price = row["entry_price"], row["stop"], row["exit_price"]
    if entry is None or stop is None or exit_price is None:
        return None
    risk = abs(entry - stop)
    if risk == 0:
        return None
    move = exit_price - entry
    if row["direction"] == "short":
        move = -move
    return round(move / risk, 3)
