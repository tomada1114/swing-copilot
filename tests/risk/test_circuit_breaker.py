"""Backtest-compatible realized-loss circuit breaker contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from swing_copilot.risk.circuit_breaker import (
    CircuitState,
    CircuitThresholds,
    RealizedTrade,
    evaluate_circuit_breaker,
)

AS_OF = date(2026, 7, 21)
EVALUATED_AT = datetime(2026, 7, 21, 20, tzinfo=UTC)  # 16:00 ET
THRESHOLDS = CircuitThresholds()


def _trade(pnl: float, closed_at: datetime = EVALUATED_AT) -> RealizedTrade:
    return RealizedTrade(closed_at=closed_at, pnl_usd=pnl)


def test_empty_journal_allows_trading_with_empty_state_quality():
    result = evaluate_circuit_breaker([], 100_000.0, AS_OF, EVALUATED_AT, THRESHOLDS)
    assert result.state is CircuitState.TRADING_ALLOWED
    assert result.data_quality == "EMPTY_STATE"
    assert result.triggered_rules == ()


@pytest.mark.parametrize(
    ("loss", "expected"),
    [
        pytest.param(-2_100.0, CircuitState.HALTED, id="daily-minus-2-point-1"),
        pytest.param(-2_000.0, CircuitState.HALTED, id="daily-exact-boundary"),
        pytest.param(-1_900.0, CircuitState.TRADING_ALLOWED, id="daily-below-boundary"),
    ],
)
def test_daily_loss_boundary(loss, expected):
    result = evaluate_circuit_breaker(
        [_trade(loss)], 100_000.0, AS_OF, EVALUATED_AT, THRESHOLDS
    )
    assert result.state is expected


def test_weekly_and_monthly_exact_boundaries_trigger_and_record_all_rules():
    monday = datetime(2026, 7, 20, 20, tzinfo=UTC)
    month_start = datetime(2026, 7, 1, 20, tzinfo=UTC)
    trades = [_trade(-5_000.0, monday), _trade(-3_000.0, month_start)]

    result = evaluate_circuit_breaker(
        trades, 100_000.0, AS_OF, EVALUATED_AT, THRESHOLDS
    )

    assert result.state is CircuitState.HALTED
    assert result.weekly_loss_pct == pytest.approx(5.0)
    assert result.monthly_loss_pct == pytest.approx(8.0)
    assert result.triggered_rules == ("WEEKLY_LOSS", "MONTHLY_LOSS")


def test_two_consecutive_losses_cool_down_until_strictly_before_24_hours():
    last_loss = datetime(2026, 7, 21, 12, tzinfo=UTC)
    trades = [
        _trade(-100.0, last_loss - timedelta(hours=1)),
        _trade(-100.0, last_loss),
    ]

    before = evaluate_circuit_breaker(
        trades,
        100_000.0,
        AS_OF,
        last_loss + timedelta(hours=23, minutes=59),
        THRESHOLDS,
    )
    exact = evaluate_circuit_breaker(
        trades,
        100_000.0,
        date(2026, 7, 22),
        last_loss + timedelta(hours=24),
        THRESHOLDS,
    )

    assert before.state is CircuitState.COOLDOWN
    assert exact.state is CircuitState.TRADING_ALLOWED


def test_zero_pnl_resets_losing_streak():
    trades = [
        _trade(-100.0, EVALUATED_AT - timedelta(hours=3)),
        _trade(0.0, EVALUATED_AT - timedelta(hours=2)),
        _trade(-100.0, EVALUATED_AT - timedelta(hours=1)),
    ]
    result = evaluate_circuit_breaker(
        trades, 100_000.0, AS_OF, EVALUATED_AT, THRESHOLDS
    )
    assert result.consecutive_losses == 1
    assert result.state is CircuitState.TRADING_ALLOWED


def test_halted_beats_cooldown_but_records_both_rules():
    trades = [
        _trade(-1_000.0, EVALUATED_AT - timedelta(hours=1)),
        _trade(-1_100.0, EVALUATED_AT),
    ]
    result = evaluate_circuit_breaker(
        trades, 100_000.0, AS_OF, EVALUATED_AT, THRESHOLDS
    )
    assert result.state is CircuitState.HALTED
    assert result.triggered_rules == (
        "DAILY_LOSS",
        "CONSECUTIVE_LOSSES",
    )


def test_et_midnight_boundary_uses_zoneinfo_not_utc_date():
    immediately_before = datetime(2026, 7, 21, 3, 59, tzinfo=UTC)  # Jul 20 ET
    exactly_at = datetime(2026, 7, 21, 4, 0, tzinfo=UTC)  # Jul 21 ET
    result = evaluate_circuit_breaker(
        [_trade(-2_000.0, immediately_before), _trade(-2_000.0, exactly_at)],
        100_000.0,
        AS_OF,
        EVALUATED_AT,
        THRESHOLDS,
    )
    assert result.daily_loss_pct == pytest.approx(2.0)
    assert "DAILY_LOSS" in result.triggered_rules


def test_partial_missing_trade_data_halts_conservatively_and_marks_quality():
    result = evaluate_circuit_breaker(
        [RealizedTrade(closed_at=None, pnl_usd=None)],
        100_000.0,
        AS_OF,
        EVALUATED_AT,
        THRESHOLDS,
    )
    assert result.state is CircuitState.HALTED
    assert result.data_quality == "PARTIAL"
    assert result.triggered_rules == ("DATA_QUALITY_PARTIAL",)
