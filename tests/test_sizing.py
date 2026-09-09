"""Position sizing: the number between a plan and an order.

Unlike almost everything else in this app there is no view here, no model and
no guess — just arithmetic. Which means the tests can be exact, and the only
interesting cases are the ones where the arithmetic has no answer.
"""

import pytest

from backend.services import sizing


def test_the_share_count_is_the_risk_divided_by_the_stop_distance():
    """1% of 25,000 is 250. A 2.50 stop distance buys 100 shares."""
    result = sizing.size(25_000, 1.0, 100.0, 97.5)
    assert result.shares == 100
    assert result.risk_amount == 250.0


def test_shares_are_floored_never_rounded():
    """4.7 rounded up to 5 risks 6% more than the number just typed. That
    quiet overshoot is exactly what this function exists to prevent."""
    # 100 risk / 21 per share = 4.76 shares
    result = sizing.size(10_000, 1.0, 100.0, 79.0)
    assert result.shares == 4
    assert result.risk_amount < result.intended_risk


def test_the_reported_risk_is_what_you_actually_lose():
    """Not what the percentage asked for — the rounding down means they
    differ, and the smaller one is the true number."""
    result = sizing.size(10_000, 1.0, 100.0, 79.0)
    assert result.risk_amount == pytest.approx(4 * 21)
    assert result.intended_risk == 100.0


def test_a_stop_equal_to_the_entry_has_no_answer():
    """Zero risk per share implies infinite size. None, not a large number."""
    assert sizing.size(10_000, 1.0, 100.0, 100.0) is None


def test_nonsense_inputs_return_nothing():
    for args in ((0, 1, 100, 95), (10_000, 0, 100, 95), (10_000, 1, 0, 95),
                 (10_000, 1, 100, 0), (-1, 1, 100, 95)):
        assert sizing.size(*args) is None, args


def test_zero_shares_is_an_answer_not_an_error():
    """"Your risk budget does not cover one share" is a real result, and must
    not be confused with "your inputs were unusable"."""
    result = sizing.size(100, 1.0, 314.57, 311.67)
    assert result is not None
    assert result.shares == 0
    assert result.capped_by == "risk"
    assert any("more than" in w for w in result.warnings)


def test_you_cannot_buy_more_than_the_account_holds():
    """A tight stop asks for more shares than there is cash. Without this the
    tool would hand you an order you cannot place."""
    result = sizing.size(10_000, 1.0, 100.0, 99.9)
    assert result.position_value <= 10_000
    assert result.capped_by == "cash"


def test_a_cash_capped_trade_says_it_risks_less_than_asked():
    result = sizing.size(10_000, 1.0, 100.0, 99.9)
    assert result.risk_amount < result.intended_risk
    assert any("less than requested" in w for w in result.warnings)


def test_margin_is_off_unless_asked_for():
    unlevered = sizing.size(10_000, 1.0, 100.0, 99.9)
    levered = sizing.size(10_000, 1.0, 100.0, 99.9, allow_margin=True)
    assert levered.shares > unlevered.shares


def test_a_large_risk_is_called_out():
    """Not blocked — it is his money — but arrived at deliberately rather than
    by arithmetic."""
    result = sizing.size(10_000, 8.0, 100.0, 90.0)
    assert any("one trade" in w for w in result.warnings)


def test_a_concentrated_position_is_called_out():
    """The stop caps the loss only if the stop is reachable. A gap through it
    costs the position, not the risk."""
    result = sizing.size(10_000, 1.0, 100.0, 99.0)
    assert result.account_pct > 25
    assert any("gap through it" in w for w in result.warnings)


def test_an_ordinary_trade_is_quiet():
    """Warnings that fire on everything get ignored."""
    assert sizing.size(100_000, 1.0, 100.0, 95.0).warnings == ()


def test_the_position_value_and_percentage_agree():
    result = sizing.size(50_000, 1.0, 250.0, 245.0)
    assert result.position_value == pytest.approx(result.shares * 250.0)
    assert result.account_pct == pytest.approx(
        result.position_value / 50_000 * 100, abs=0.01
    )


def test_a_stop_above_the_entry_still_sizes():
    """The distance is what matters. A short's stop sits above its entry, and
    refusing to size it here would push that check into every caller."""
    long_side = sizing.size(25_000, 1.0, 100.0, 97.5)
    short_side = sizing.size(25_000, 1.0, 100.0, 102.5)
    assert long_side.shares == short_side.shares


def test_sizing_touches_nothing():
    """Pure: the module may not read a file, a socket or a clock."""
    import inspect

    source = inspect.getsource(sizing)
    for banned in ("import os", "open(", "requests", "httpx", "sqlite3",
                   "datetime.now", "time.time"):
        assert banned not in source, banned
