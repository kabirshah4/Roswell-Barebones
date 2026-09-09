"""How many shares.

The playbook computes an entry and a stop. This turns the distance between them
into a share count, given how much of the account you are willing to lose on the
trade. It is the one number standing between a plan and an order, and unlike
almost everything else in this app it involves no view, no model and no guess —
just arithmetic.

Pure. No I/O.
"""

from dataclasses import dataclass
from math import floor

# Above this, the tool says so. Not a limit — it is your money — but risking a
# fifth of an account on one idea is a decision that deserves to be noticed
# rather than arrived at by arithmetic.
LOUD_RISK_PCT = 5.0
LOUD_CONCENTRATION_PCT = 25.0


@dataclass(frozen=True)
class Sizing:
    shares: int
    risk_per_share: float
    risk_amount: float          # what you actually lose if stopped, after rounding
    intended_risk: float        # what the risk % asked for
    position_value: float
    account_pct: float
    capped_by: str | None       # None | "cash" | "risk"
    warnings: tuple[str, ...]


def size(
    account: float,
    risk_pct: float,
    entry: float,
    stop: float,
    *,
    allow_margin: bool = False,
) -> Sizing | None:
    """Shares to buy, or None when the question has no answer.

    Returns None rather than a zero when the inputs are unusable — a zero is a
    real answer ("your risk budget does not cover one share") and should not be
    confused with "you gave me a stop equal to the entry".
    """
    if not all(isinstance(v, (int, float)) for v in (account, risk_pct, entry, stop)):
        return None
    if account <= 0 or risk_pct <= 0 or entry <= 0 or stop <= 0:
        return None

    risk_per_share = abs(entry - stop)
    if risk_per_share == 0:
        # An entry and stop at the same price implies infinite size.
        return None

    intended_risk = account * risk_pct / 100
    raw = intended_risk / risk_per_share

    # Floor, never round. Rounding 4.7 up to 5 risks 6% more than the number you
    # just typed, which is exactly the kind of quiet overshoot this is meant to
    # prevent.
    shares = floor(raw)
    capped_by = None

    if not allow_margin and shares * entry > account:
        affordable = floor(account / entry)
        if affordable < shares:
            shares = affordable
            capped_by = "cash"

    warnings: list[str] = []
    if shares == 0:
        capped_by = capped_by or "risk"
        warnings.append(
            f"One share risks {risk_per_share:,.2f}, more than the "
            f"{intended_risk:,.2f} this trade is allowed to lose. Either the "
            f"stop is too far away or the risk is too small."
        )
    if risk_pct >= LOUD_RISK_PCT:
        warnings.append(
            f"{risk_pct:g}% of the account on one trade. Six in a row at this "
            f"size is a quarter of the account."
        )

    position_value = shares * entry
    account_pct = position_value / account * 100 if account else 0.0
    if shares and account_pct >= LOUD_CONCENTRATION_PCT:
        warnings.append(
            f"This position is {account_pct:.0f}% of the account. The risk is "
            f"still capped by the stop, but only if the stop is reachable — a "
            f"gap through it costs the position, not the risk."
        )
    if capped_by == "cash":
        warnings.append(
            "Cash-capped: the risk budget allowed more shares than the account "
            "can pay for. The trade risks less than requested."
        )

    return Sizing(
        shares=shares,
        risk_per_share=round(risk_per_share, 4),
        risk_amount=round(shares * risk_per_share, 2),
        intended_risk=round(intended_risk, 2),
        position_value=round(position_value, 2),
        account_pct=round(account_pct, 2),
        capped_by=capped_by,
        warnings=tuple(warnings),
    )
