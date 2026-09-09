"""The journal: what happened, measured against what was planned.

`factor_study.py` asks whether the engine works. This asks whether the trader
does, which is the more uncomfortable question and the one with the thinner
sample — so the tests care most about the honesty guards: excluding open
trades, flagging thin samples, and counting the losses that ran past the stop.
"""

import pytest

from backend.services import journal


def trade(entry=100, stop=95, exit_price=110, target1=110, shares=10,
          direction="long", fees=0, grade="B",
          entry_at="2026-01-01T00:00:00", exit_at="2026-01-05T00:00:00"):
    return {"ticker": "T", "direction": direction, "shares": shares,
            "entry_price": entry, "stop": stop, "target1": target1,
            "exit_price": exit_price, "entry_at": entry_at, "exit_at": exit_at,
            "fees": fees, "plan_grade": grade}


# --- R, the only unit that survives changing account size --------------------

def test_a_winner_is_measured_in_multiples_of_its_own_risk():
    """Risked 5 to make 10: two R."""
    assert journal.realised_r(trade(100, 95, 110)) == pytest.approx(2.0)


def test_a_full_stop_out_is_minus_one_r():
    assert journal.realised_r(trade(100, 95, 95)) == pytest.approx(-1.0)


def test_a_short_makes_money_when_price_falls():
    """Sign handling: without it every short reads as its own mirror image."""
    assert journal.realised_r(
        trade(100, 105, 90, direction="short")
    ) == pytest.approx(2.0)


def test_r_needs_a_stop():
    """Without one there is no risk to divide by, and a percentage return
    would quietly answer a different question."""
    assert journal.realised_r(trade(stop=None)) is None
    assert journal.realised_r(trade(stop=100, entry=100)) is None


def test_an_open_trade_has_no_realised_r():
    assert journal.realised_r(trade(exit_price=None)) is None


def test_pnl_is_after_fees():
    assert journal.pnl(trade(100, 95, 110, shares=10, fees=7)) == pytest.approx(93)


def test_open_pnl_marks_to_the_last_quote():
    assert journal.open_pnl(trade(100, 95, None, shares=10), 107) == pytest.approx(70)


def test_open_pnl_without_a_quote_is_unknown_not_zero():
    assert journal.open_pnl(trade(exit_price=None), None) is None


# --- the summary -------------------------------------------------------------

def test_open_trades_are_left_out_of_the_record():
    """An unrealised gain is not a result. Counting it would flatter every
    summary taken during a rally."""
    rows = [trade(exit_price=110), trade(exit_price=None, exit_at=None)]
    assert journal.summarise(rows).closed == 1


def test_expectancy_is_reported_because_win_rate_alone_misleads():
    """35% at +2R beats 60% at +0.4R, and only expectancy says so."""
    lots_of_small_wins = [trade(100, 95, 102) for _ in range(6)] + \
                         [trade(100, 95, 95) for _ in range(4)]
    few_big_wins = [trade(100, 95, 115) for _ in range(3)] + \
                   [trade(100, 95, 95) for _ in range(7)]
    a = journal.summarise(lots_of_small_wins)
    b = journal.summarise(few_big_wins)
    assert a.win_rate > b.win_rate
    assert b.expectancy_r > a.expectancy_r


def test_a_loss_worse_than_the_stop_is_counted_separately():
    """A gap, slippage, or a stop that was moved. It is the failure mode that
    quietly turns a positive expectancy negative."""
    rows = [trade(100, 95, 95), trade(100, 95, 88)]
    assert journal.summarise(rows).overruns == 1


def test_a_stop_honoured_exactly_is_not_an_overrun():
    assert journal.summarise([trade(100, 95, 95)]).overruns == 0


def test_the_gap_between_plan_and_result_is_measured():
    """Planned +3R, took +1R: two left on the table. This is what "I cut my
    winners" looks like as a number."""
    rows = [trade(100, 95, 105, target1=115)]
    assert journal.summarise(rows).left_on_table == pytest.approx(2.0)


def test_a_thin_sample_is_flagged():
    """A win rate from four trades is a rumour. Reported anyway — hiding it
    invites guessing — but never without the flag."""
    assert journal.summarise([trade() for _ in range(4)]).thin is True
    assert journal.summarise(
        [trade() for _ in range(journal.THIN_SAMPLE)]
    ).thin is False


def test_an_empty_journal_reports_unknown_not_zero():
    """A 0% win rate over no trades reads as "I lose every time"."""
    empty = journal.summarise([])
    assert empty.win_rate is None
    assert empty.expectancy_r is None
    assert empty.closed == 0


def test_grades_carry_their_own_counts():
    """The backtest showed the engine's A+ does not reliably beat its B.
    Whether that holds for the trades actually taken is a separate question on
    a far thinner sample, so every row states its n."""
    rows = [trade(grade="A"), trade(grade="B"), trade(grade="B", exit_price=95)]
    by_grade = journal.summarise(rows).by_grade
    assert by_grade["A"]["n"] == 1
    assert by_grade["B"]["n"] == 2
    assert all("n" in v for v in by_grade.values())


def test_a_trade_with_no_stop_does_not_poison_the_averages():
    """It has no R. It should be skipped, not counted as zero."""
    rows = [trade(100, 95, 110), trade(stop=None)]
    assert journal.summarise(rows).expectancy_r == pytest.approx(2.0)


def test_hold_time_is_reported_in_days():
    assert journal.summarise([trade()]).avg_hold_days == pytest.approx(4.0)


def test_a_malformed_timestamp_does_not_crash_the_summary():
    assert journal.summarise([trade(exit_at="not-a-date")]).closed == 1


def test_the_journal_touches_nothing():
    import inspect

    source = inspect.getsource(journal)
    for banned in ("import os", "open(", "httpx", "sqlite3", "requests"):
        assert banned not in source, banned
