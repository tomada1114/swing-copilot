"""Unit tests for the shared exit primitives (`backtest/exits.py`).

These rules are shared by the backtest engine and any other consumer that must
apply the *same* trailing-stop / max-hold semantics, so the boundaries
(`<=` on the stop, stop-before-max-hold precedence, `days_held + 1 >=
max_hold_days`) are pinned here rather than only implied by engine-level
regression tests.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from swing_copilot.backtest.exits import (
    ExitDecision,
    atr_as_of,
    atr_by_date,
    evaluate_exit,
    next_trailing_stop,
)
from swing_copilot.screening.indicators import symbol_window

_START = date(2027, 1, 1)
#: `settings.trade_plan.exit_atr_period`'s shipped value; these tests pin the
#: shape of the function, not the configured number (Issue #194).
_PERIOD = 14


def _days(count: int) -> list[date]:
    return [_START + timedelta(days=index) for index in range(count)]


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _flat_rows(
    days: list[date], symbol: str = "AAA", close: float = 100.0
) -> list[dict[str, object]]:
    """Bars whose true range is exactly 2.0 every day (high/low = close +/- 1)."""
    return [
        {
            "symbol": symbol,
            "date": day,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        }
        for day in days
    ]


# --- evaluate_exit -----------------------------------------------------------


def test_evaluate_exit_open_below_stop_exits_at_open() -> None:
    decision = evaluate_exit(
        open_price=90.0,
        low=88.0,
        close=92.0,
        stop_price=95.0,
        days_held=0,
        max_hold_days=60,
    )

    assert decision == ExitDecision(exit_price=90.0, reason="stop")


def test_evaluate_exit_open_equal_to_stop_exits_at_open() -> None:
    decision = evaluate_exit(
        open_price=95.0,
        low=94.0,
        close=96.0,
        stop_price=95.0,
        days_held=0,
        max_hold_days=60,
    )

    assert decision == ExitDecision(exit_price=95.0, reason="stop")


def test_evaluate_exit_low_equal_to_stop_exits_at_stop_price() -> None:
    decision = evaluate_exit(
        open_price=99.0,
        low=95.0,
        close=98.0,
        stop_price=95.0,
        days_held=0,
        max_hold_days=60,
    )

    assert decision == ExitDecision(exit_price=95.0, reason="stop")


def test_evaluate_exit_low_below_stop_exits_at_stop_price() -> None:
    decision = evaluate_exit(
        open_price=99.0,
        low=93.5,
        close=98.0,
        stop_price=95.0,
        days_held=0,
        max_hold_days=60,
    )

    assert decision == ExitDecision(exit_price=95.0, reason="stop")


def test_evaluate_exit_low_just_above_stop_returns_none() -> None:
    decision = evaluate_exit(
        open_price=99.0,
        low=95.01,
        close=98.0,
        stop_price=95.0,
        days_held=0,
        max_hold_days=60,
    )

    assert decision is None


def test_evaluate_exit_stop_and_max_hold_on_same_day_prefers_stop() -> None:
    decision = evaluate_exit(
        open_price=99.0,
        low=95.0,
        close=98.0,
        stop_price=95.0,
        days_held=59,
        max_hold_days=60,
    )

    assert decision == ExitDecision(exit_price=95.0, reason="stop")


def test_evaluate_exit_gap_down_and_max_hold_on_same_day_prefers_stop_at_open() -> None:
    decision = evaluate_exit(
        open_price=90.0,
        low=89.0,
        close=91.0,
        stop_price=95.0,
        days_held=59,
        max_hold_days=60,
    )

    assert decision == ExitDecision(exit_price=90.0, reason="stop")


def test_evaluate_exit_days_held_one_below_max_hold_exits_at_close() -> None:
    decision = evaluate_exit(
        open_price=99.0,
        low=98.0,
        close=101.0,
        stop_price=95.0,
        days_held=59,
        max_hold_days=60,
    )

    assert decision == ExitDecision(exit_price=101.0, reason="max_hold")


def test_evaluate_exit_days_held_two_below_max_hold_returns_none() -> None:
    decision = evaluate_exit(
        open_price=99.0,
        low=98.0,
        close=101.0,
        stop_price=95.0,
        days_held=58,
        max_hold_days=60,
    )

    assert decision is None


def test_evaluate_exit_days_held_past_max_hold_exits_at_close() -> None:
    decision = evaluate_exit(
        open_price=99.0,
        low=98.0,
        close=101.0,
        stop_price=95.0,
        days_held=61,
        max_hold_days=60,
    )

    assert decision == ExitDecision(exit_price=101.0, reason="max_hold")


def test_evaluate_exit_missing_stop_skips_stop_checks() -> None:
    decision = evaluate_exit(
        open_price=10.0,
        low=1.0,
        close=5.0,
        stop_price=None,
        days_held=0,
        max_hold_days=60,
    )

    assert decision is None


def test_evaluate_exit_missing_stop_still_applies_max_hold() -> None:
    decision = evaluate_exit(
        open_price=10.0,
        low=1.0,
        close=5.0,
        stop_price=None,
        days_held=59,
        max_hold_days=60,
    )

    assert decision == ExitDecision(exit_price=5.0, reason="max_hold")


# --- next_trailing_stop ------------------------------------------------------


def test_next_trailing_stop_without_current_stop_returns_candidate() -> None:
    assert (
        next_trailing_stop(
            current_stop=None, close=100.0, atr=2.0, exit_atr_multiple=2.5
        )
        == 95.0
    )


def test_next_trailing_stop_higher_candidate_ratchets_up() -> None:
    assert (
        next_trailing_stop(
            current_stop=90.0, close=100.0, atr=2.0, exit_atr_multiple=2.5
        )
        == 95.0
    )


def test_next_trailing_stop_lower_candidate_keeps_current_stop() -> None:
    assert (
        next_trailing_stop(
            current_stop=96.0, close=100.0, atr=2.0, exit_atr_multiple=2.5
        )
        == 96.0
    )


def test_next_trailing_stop_equal_candidate_keeps_same_value() -> None:
    assert (
        next_trailing_stop(
            current_stop=95.0, close=100.0, atr=2.0, exit_atr_multiple=2.5
        )
        == 95.0
    )


# --- atr_as_of -------------------------------------------------------------


def test_atr_as_of_smoothing_period_follows_the_argument() -> None:
    # Issue #194: the period is configuration, not a constant. A shorter
    # period reacts faster to one spike day, so the two values must differ by
    # exactly the Wilder step each period implies.
    days = _days(21)
    rows = _flat_rows(days[:20])
    rows.append(
        {
            "symbol": "AAA",
            "date": days[20],
            "open": 100.0,
            "high": 110.0,
            "low": 100.0,
            "close": 105.0,
        }
    )
    frame = _frame(rows)

    with_14 = atr_as_of(frame, "AAA", days[20], 14)
    with_7 = atr_as_of(frame, "AAA", days[20], 7)

    assert with_14 == pytest.approx(2.0 + 8.0 / 14.0)
    assert with_7 == pytest.approx(2.0 + 8.0 / 7.0)


def test_atr_as_of_minimum_history_boundary_follows_the_period() -> None:
    days = _days(10)
    frame = _frame(_flat_rows(days))

    # 10 bars is short of a 14-period ATR but enough for a 10-period one.
    assert atr_as_of(frame, "AAA", days[-1], 14) is None
    assert atr_as_of(frame, "AAA", days[-1], 10) == pytest.approx(2.0)


def test_atr_as_of_insufficient_history_returns_none() -> None:
    days = _days(_PERIOD - 1)

    assert atr_as_of(_frame(_flat_rows(days)), "AAA", days[-1], _PERIOD) is None


def test_atr_as_of_exactly_period_bars_returns_value() -> None:
    days = _days(_PERIOD)

    assert atr_as_of(
        _frame(_flat_rows(days)), "AAA", days[-1], _PERIOD
    ) == pytest.approx(2.0)


def test_atr_as_of_unknown_symbol_returns_none() -> None:
    days = _days(20)

    assert atr_as_of(_frame(_flat_rows(days)), "ZZZ", days[-1], _PERIOD) is None


def test_atr_as_of_all_nan_bars_returns_none() -> None:
    days = _days(20)
    rows: list[dict[str, object]] = [
        {
            "symbol": "AAA",
            "date": day,
            "open": float("nan"),
            "high": float("nan"),
            "low": float("nan"),
            "close": float("nan"),
        }
        for day in days
    ]

    assert atr_as_of(_frame(rows), "AAA", days[-1], _PERIOD) is None


def test_atr_as_of_ignores_bars_after_cutoff() -> None:
    days = _days(21)
    rows = _flat_rows(days[:20])
    spike_day = days[20]
    # True range on the spike day is max(110-100, |110-100|, |100-100|) = 10,
    # so Wilder smoothing gives 2 + (10 - 2) / 14 from the flat 2.0 baseline.
    rows.append(
        {
            "symbol": "AAA",
            "date": spike_day,
            "open": 100.0,
            "high": 110.0,
            "low": 100.0,
            "close": 105.0,
        }
    )
    frame = _frame(rows)

    before = atr_as_of(frame, "AAA", days[19], _PERIOD)
    at_cutoff = atr_as_of(frame, "AAA", spike_day, _PERIOD)

    assert before == pytest.approx(2.0)
    assert at_cutoff == pytest.approx(2.0 + 8.0 / 14.0)


# --- atr_by_date -----------------------------------------------------------


def test_atr_by_date_matches_atr_as_of_for_every_session() -> None:
    # The one-pass map is the contract: a caller replaying a position day by
    # day must get exactly what a per-day `atr_as_of` call would return, or
    # the ledger's trailing stop drifts away from the engine's.
    days = _days(21)
    rows = _flat_rows(days[:20])
    rows.append(
        {
            "symbol": "AAA",
            "date": days[20],
            "open": 100.0,
            "high": 110.0,
            "low": 100.0,
            "close": 105.0,
        }
    )
    frame = _frame(rows)

    by_date = atr_by_date(frame, "AAA", days[-1], _PERIOD)

    assert by_date == {
        day: pytest.approx(atr_as_of(frame, "AAA", day, _PERIOD))
        for day in days
        if atr_as_of(frame, "AAA", day, _PERIOD) is not None
    }


def test_atr_by_date_smoothing_period_follows_the_argument() -> None:
    # The one-pass map must honor the same configured period as `atr_as_of`,
    # or a ledger replay would trail a different stop than the simulator.
    days = _days(21)
    rows = _flat_rows(days[:20])
    rows.append(
        {
            "symbol": "AAA",
            "date": days[20],
            "open": 100.0,
            "high": 110.0,
            "low": 100.0,
            "close": 105.0,
        }
    )
    frame = _frame(rows)

    assert atr_by_date(frame, "AAA", days[20], 7)[days[20]] == pytest.approx(
        atr_as_of(frame, "AAA", days[20], 7)
    )
    assert atr_by_date(frame, "AAA", days[20], 7)[days[20]] != pytest.approx(
        atr_by_date(frame, "AAA", days[20], 14)[days[20]]
    )


def test_atr_by_date_omits_sessions_without_enough_history() -> None:
    days = _days(_PERIOD + 1)

    by_date = atr_by_date(_frame(_flat_rows(days)), "AAA", days[-1], _PERIOD)

    # `min_periods=_PERIOD`: the first value lands on the 14th session.
    assert sorted(by_date) == days[_PERIOD - 1 :]


def test_atr_by_date_ignores_bars_after_cutoff() -> None:
    days = _days(21)

    by_date = atr_by_date(_frame(_flat_rows(days)), "AAA", days[15], _PERIOD)

    assert max(by_date) == days[15]


def test_atr_by_date_insufficient_history_returns_empty() -> None:
    days = _days(_PERIOD - 1)

    assert atr_by_date(_frame(_flat_rows(days)), "AAA", days[-1], _PERIOD) == {}


def test_atr_by_date_unknown_symbol_returns_empty() -> None:
    days = _days(20)

    assert atr_by_date(_frame(_flat_rows(days)), "ZZZ", days[-1], _PERIOD) == {}


# --- SymbolWindow.atr, the engine's one-pass replacement for atr_as_of -------
#
# Issue #224: `BacktestEngine._update_trailing_stops` reads the trailing stop's
# ATR from `symbol_window(...).atr(period)` instead of calling `atr_as_of` once
# per position per simulated day. Nothing about the simulated numbers may move,
# so the substitution's equivalence is pinned here beside `atr_as_of` itself,
# the same way `atr_by_date`'s is.


def _spike_frame(days: list[date]) -> pd.DataFrame:
    """Flat TR-2.0 bars with one TR-10.0 spike on the second-to-last session."""
    rows = _flat_rows(days[:-2])
    rows.append(
        {
            "symbol": "AAA",
            "date": days[-2],
            "open": 100.0,
            "high": 110.0,
            "low": 100.0,
            "close": 105.0,
        }
    )
    rows += _flat_rows(days[-1:], close=105.0)
    return _frame(rows)


def test_symbol_window_atr_matches_atr_as_of_for_every_session() -> None:
    days = _days(22)
    frame = _spike_frame(days)

    for day in days:
        window = symbol_window(frame, "AAA", day)
        windowed = None if window is None else window.atr(_PERIOD)
        expected = atr_as_of(frame, "AAA", day, _PERIOD)
        if expected is None:
            assert windowed is None or math.isnan(windowed)
        else:
            # Bit-identical, not merely close: the engine's equity curve is
            # asserted to the cent against hand-calculated ATRs.
            assert windowed == expected


def test_symbol_window_atr_at_the_as_of_boundary_ignores_later_bars() -> None:
    # Immediately before / exactly at / immediately after the spike session,
    # against a frame that always carries the later bars (the engine hands the
    # whole run's frame to every day's lookup).
    days = _days(22)
    frame = _spike_frame(days)
    before, at_cutoff, after = days[-3], days[-2], days[-1]

    values = [
        symbol_window(frame, "AAA", day).atr(_PERIOD)  # type: ignore[union-attr]  # every day has bars
        for day in (before, at_cutoff, after)
    ]

    assert values[0] == pytest.approx(2.0)
    assert values[1] == pytest.approx(2.0 + 8.0 / 14.0)
    assert values[2] == pytest.approx(values[1] + (2.0 - values[1]) / 14.0)
    assert values == [
        atr_as_of(frame, "AAA", day, _PERIOD) for day in (before, at_cutoff, after)
    ]


def test_symbol_window_atr_is_nan_where_atr_as_of_reports_no_value() -> None:
    # `atr_as_of` returns `None` below `period` bars; the window reports the
    # same absence as `NaN`, which is what the engine's `math.isnan` guard
    # tests. Pinned at the exact boundary session on both sides.
    days = _days(_PERIOD)
    frame = _frame(_flat_rows(days))

    short = symbol_window(frame, "AAA", days[_PERIOD - 2])
    exact = symbol_window(frame, "AAA", days[_PERIOD - 1])

    assert short is not None
    assert exact is not None
    assert math.isnan(short.atr(_PERIOD))
    assert atr_as_of(frame, "AAA", days[_PERIOD - 2], _PERIOD) is None
    assert exact.atr(_PERIOD) == atr_as_of(frame, "AAA", days[_PERIOD - 1], _PERIOD)


def test_symbol_window_is_none_for_a_symbol_the_frame_does_not_carry() -> None:
    # The engine maps this to `NaN` and skips the trailing-stop update, exactly
    # as `atr_as_of`'s `None` did.
    days = _days(20)
    frame = _frame(_flat_rows(days))

    assert symbol_window(frame, "ZZZ", days[-1]) is None
    assert atr_as_of(frame, "ZZZ", days[-1], _PERIOD) is None
