"""What actually happened, measured against what was planned.

`factor_study.py` asks whether the engine works. This asks whether the trader
does — the same question pointed the other way, and the more uncomfortable one.

Everything is measured in R: multiples of the risk you took, meaning the
distance from entry to stop. R is the only unit that makes a 40-share trade and
a 400-share trade comparable, and the only one that survives changing account
size.

Pure. No I/O.
"""

from dataclasses import dataclass, field
from datetime import datetime

# Below this, a win rate is a rumour. Reported anyway — hiding it invites
# guessing — but never without the count beside it.
THIN_SAMPLE = 20


def realised_r(row: dict) -> float | None:
    """What the trade actually returned, in units of its own risk.

    Needs a stop: without one there is no risk to divide by, and a percentage
    return would silently answer a different question.
    """
    entry, stop, exit_price = row.get("entry_price"), row.get("stop"), row.get("exit_price")
    if entry is None or stop is None or exit_price is None:
        return None
    risk = abs(entry - stop)
    if risk == 0:
        return None
    move = exit_price - entry
    if row.get("direction") == "short":
        move = -move
    return move / risk


def planned_r(row: dict, target_key: str = "target1") -> float | None:
    """What the plan was worth if it reached its first target."""
    entry, stop, target = row.get("entry_price"), row.get("stop"), row.get(target_key)
    if entry is None or stop is None or target is None:
        return None
    risk = abs(entry - stop)
    if risk == 0:
        return None
    move = target - entry
    if row.get("direction") == "short":
        move = -move
    return move / risk


def pnl(row: dict) -> float | None:
    """Currency, after fees. R is the honest unit; this is the one you spend."""
    entry, exit_price, shares = (
        row.get("entry_price"), row.get("exit_price"), row.get("shares")
    )
    if entry is None or exit_price is None or shares is None:
        return None
    move = exit_price - entry
    if row.get("direction") == "short":
        move = -move
    return move * shares - (row.get("fees") or 0)


def open_pnl(row: dict, price: float | None) -> float | None:
    if price is None or row.get("shares") is None or row.get("entry_price") is None:
        return None
    move = price - row["entry_price"]
    if row.get("direction") == "short":
        move = -move
    return move * row["shares"] - (row.get("fees") or 0)


def hold_days(row: dict) -> float | None:
    try:
        opened = datetime.fromisoformat(row["entry_at"])
        closed = datetime.fromisoformat(row["exit_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (closed - opened).total_seconds() / 86400


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class Journal:
    closed: int
    wins: int
    losses: int
    scratches: int
    win_rate: float | None
    avg_win_r: float | None
    avg_loss_r: float | None
    expectancy_r: float | None
    total_r: float | None
    realised_pnl: float
    avg_hold_days: float | None
    overruns: int                 # losses worse than the stop said they would be
    left_on_table: float | None   # planned R minus realised R, on winners
    thin: bool
    by_grade: dict = field(default_factory=dict)


def summarise(rows: list[dict]) -> Journal:
    """Aggregate closed trades. Open ones are excluded — an unrealised gain is
    not a result, and counting it would flatter every summary taken during a
    rally."""
    closed = [r for r in rows if r.get("exit_at")]
    rs = [(r, realised_r(r)) for r in closed]
    scored = [(r, v) for r, v in rs if v is not None]

    wins = [v for _, v in scored if v > 0]
    losses = [v for _, v in scored if v < 0]
    scratches = [v for _, v in scored if v == 0]

    # A loss worse than -1R means the stop did not hold: a gap, slippage, or it
    # was moved. Worth counting separately, because it is the failure mode that
    # quietly turns a positive expectancy negative.
    overruns = sum(1 for v in losses if v < -1.01)

    gaps = []
    for r, v in scored:
        target = planned_r(r)
        if target is not None and v > 0:
            gaps.append(target - v)

    return Journal(
        closed=len(closed),
        wins=len(wins),
        losses=len(losses),
        scratches=len(scratches),
        win_rate=round(len(wins) / len(scored) * 100, 1) if scored else None,
        avg_win_r=round(_mean(wins), 2) if wins else None,
        avg_loss_r=round(_mean(losses), 2) if losses else None,
        # The number that actually decides whether this is worth doing. A 35%
        # win rate at +2R beats a 60% win rate at +0.4R, and only expectancy
        # says so.
        expectancy_r=round(_mean([v for _, v in scored]), 3) if scored else None,
        total_r=round(sum(v for _, v in scored), 2) if scored else None,
        realised_pnl=round(sum(pnl(r) or 0 for r in closed), 2),
        avg_hold_days=(
            round(_mean([d for d in (hold_days(r) for r in closed) if d is not None]), 1)
            if any(hold_days(r) is not None for r in closed) else None
        ),
        overruns=overruns,
        left_on_table=round(_mean(gaps), 2) if gaps else None,
        thin=len(scored) < THIN_SAMPLE,
        by_grade=_by_grade(scored),
    )


def _by_grade(scored: list[tuple[dict, float]]) -> dict:
    """Per-grade expectancy, so "do I actually do better on A setups?" has an
    answer that is not a feeling.

    The backtest already showed the engine's A+ does not reliably beat its B.
    Whether that holds for the trades you took is a different question, and the
    sample will be far thinner — so every row carries its own count.
    """
    buckets: dict[str, list[float]] = {}
    for row, value in scored:
        grade = row.get("plan_grade") or "—"
        buckets.setdefault(grade, []).append(value)
    return {
        grade: {
            "n": len(values),
            "expectancy_r": round(sum(values) / len(values), 3),
            "win_rate": round(
                sum(1 for v in values if v > 0) / len(values) * 100, 1
            ),
        }
        for grade, values in sorted(buckets.items())
    }
