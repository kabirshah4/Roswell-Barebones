"""Derived numbers from an option chain.

Pure functions over rows the client already fetched — no I/O — so the maths is
testable against a fixed chain rather than whatever the market is doing.

Everything here is arithmetic on quoted prices. Nothing models a fair value:
pricing an option properly needs a rate, a dividend assumption and a view on
volatility, and a Black-Scholes number computed from those guesses would look
far more authoritative than it is.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChainSummary:
    spot: float | None
    call_volume: float
    put_volume: float
    call_open_interest: float
    put_open_interest: float
    put_call_volume: float | None
    put_call_open_interest: float | None
    atm_iv: float | None
    implied_move_pct: float | None
    implied_move_abs: float | None
    max_pain: float | None


def _nearest(rows: list[dict], spot: float) -> dict | None:
    """The contract whose strike sits closest to spot."""
    priced = [r for r in rows if r.get("strike") is not None]
    return min(priced, key=lambda r: abs(r["strike"] - spot), default=None)


def atm_iv(calls: list[dict], puts: list[dict], spot: float | None) -> float | None:
    """Implied volatility at the money, averaged across the call and the put.

    Averaging the two matters: a single side's quote can be stale or wide, and
    put-call parity says a healthy market prices them close together.
    """
    if not spot:
        return None
    values = []
    for side in (calls, puts):
        row = _nearest(side, spot)
        if row and row.get("iv"):
            values.append(row["iv"])
    return round(sum(values) / len(values), 4) if values else None


def implied_move(calls: list[dict], puts: list[dict], spot: float | None,
                 days: int) -> tuple[float | None, float | None]:
    """What the option market expects this to move by expiry, as % and dollars.

    Uses the at-the-money straddle: the cost of buying both the call and the
    put is, near enough, what the market charges for the move in either
    direction. That is the standard approximation and it is close enough at the
    money; it is not a substitute for a proper distribution.
    """
    if not spot or days <= 0:
        return None, None
    call = _nearest(calls, spot)
    put = _nearest(puts, spot)
    if not call or not put:
        return None, None

    def mid(row):
        bid, ask = row.get("bid"), row.get("ask")
        if bid and ask:
            return (bid + ask) / 2
        # A contract with no two-sided quote still has a last trade, which is
        # better than discarding the strike entirely.
        return row.get("last")

    call_mid, put_mid = mid(call), mid(put)
    if not call_mid or not put_mid:
        return None, None
    straddle = call_mid + put_mid
    return round(straddle / spot * 100, 2), round(straddle, 2)


def max_pain(calls: list[dict], puts: list[dict]) -> float | None:
    """The strike where the most open interest expires worthless.

    A folk measure, included because it is on every options screen and people
    look for it — not because there is good evidence price gravitates to it.
    """
    strikes = sorted({r["strike"] for r in calls + puts if r.get("strike")})
    if not strikes:
        return None

    def pain_at(price: float) -> float:
        total = 0.0
        for row in calls:
            if row["strike"] < price:
                total += (price - row["strike"]) * (row.get("open_interest") or 0)
        for row in puts:
            if row["strike"] > price:
                total += (row["strike"] - price) * (row.get("open_interest") or 0)
        return total

    return min(strikes, key=pain_at)


def summarise(calls: list[dict], puts: list[dict], spot: float | None,
              days: int) -> ChainSummary:
    call_vol = sum(r.get("volume") or 0 for r in calls)
    put_vol = sum(r.get("volume") or 0 for r in puts)
    call_oi = sum(r.get("open_interest") or 0 for r in calls)
    put_oi = sum(r.get("open_interest") or 0 for r in puts)
    move_pct, move_abs = implied_move(calls, puts, spot, days)
    return ChainSummary(
        spot=spot,
        call_volume=call_vol,
        put_volume=put_vol,
        call_open_interest=call_oi,
        put_open_interest=put_oi,
        # Guarded: a name with no put volume would otherwise divide by zero,
        # and reporting 0.0 would read as "no put interest" rather than "no
        # data".
        put_call_volume=round(put_vol / call_vol, 3) if call_vol else None,
        put_call_open_interest=round(put_oi / call_oi, 3) if call_oi else None,
        atm_iv=atm_iv(calls, puts, spot),
        implied_move_pct=move_pct,
        implied_move_abs=move_abs,
        max_pain=max_pain(calls, puts),
    )
