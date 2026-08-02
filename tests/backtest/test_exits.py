"""Unit tests for the shared exit primitives (`backtest/exits.py`).

These rules are shared by the backtest engine and any other consumer that must
apply the *same* trailing-stop / max-hold semantics, so the boundaries
(`<=` on the stop, stop-before-max-hold precedence, `days_held + 1 >=
max_hold_days`) are pinned here rather than only implied by engine-level
regression tests.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from swing_copilot.backtest.exits import (
    ATR_PERIOD,
    ExitDecision,
    atr14_as_of,
    evaluate_exit,
    next_trailing_stop,
)

_START = date(2027, 1, 1)


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


# --- atr14_as_of -------------------------------------------------------------


def test_atr14_as_of_uses_fourteen_period_window() -> None:
    assert ATR_PERIOD == 14


def test_atr14_as_of_insufficient_history_returns_none() -> None:
    days = _days(ATR_PERIOD - 1)

    assert atr14_as_of(_frame(_flat_rows(days)), "AAA", days[-1]) is None


def test_atr14_as_of_exactly_period_bars_returns_value() -> None:
    days = _days(ATR_PERIOD)

    assert atr14_as_of(_frame(_flat_rows(days)), "AAA", days[-1]) == pytest.approx(2.0)


def test_atr14_as_of_unknown_symbol_returns_none() -> None:
    days = _days(20)

    assert atr14_as_of(_frame(_flat_rows(days)), "ZZZ", days[-1]) is None


def test_atr14_as_of_all_nan_bars_returns_none() -> None:
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

    assert atr14_as_of(_frame(rows), "AAA", days[-1]) is None


def test_atr14_as_of_ignores_bars_after_cutoff() -> None:
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

    before = atr14_as_of(frame, "AAA", days[19])
    at_cutoff = atr14_as_of(frame, "AAA", spike_day)

    assert before == pytest.approx(2.0)
    assert at_cutoff == pytest.approx(2.0 + 8.0 / 14.0)
