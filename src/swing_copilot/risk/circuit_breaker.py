"""Deterministic circuit breaker derived from realized paper-trade P&L."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


class CircuitState(Enum):
    """Whether new entries are permitted by realized-loss controls."""

    HALTED = "HALTED"
    COOLDOWN = "COOLDOWN"
    TRADING_ALLOWED = "TRADING_ALLOWED"


@dataclass(frozen=True, slots=True)
class CircuitThresholds:
    """Configured loss and streak thresholds."""

    daily_loss_pct: float = 2.0
    weekly_loss_pct: float = 5.0
    monthly_loss_pct: float = 8.0
    consecutive_losses: int = 2
    cooldown_hours: int = 24


@dataclass(frozen=True, slots=True)
class RealizedTrade:
    """Minimal closed-trade input; incomplete rows remain explicit."""

    closed_at: datetime | None
    pnl_usd: float | None


@dataclass(frozen=True, slots=True)
class CircuitBreakerResult:
    """Circuit state and every rule that contributed to it."""

    state: CircuitState
    daily_loss_pct: float | None
    weekly_loss_pct: float | None
    monthly_loss_pct: float | None
    consecutive_losses: int
    triggered_rules: tuple[str, ...]
    data_quality: str
    cooldown_until: datetime | None = None


def evaluation_time_for_as_of(as_of: date) -> datetime:
    """Return the deterministic end of the requested Eastern trading day."""
    return datetime.combine(as_of, time.max, tzinfo=_ET)


def evaluate_circuit_breaker(
    trades: list[RealizedTrade],
    account_equity: float | None,
    as_of: date,
    evaluated_at: datetime,
    thresholds: CircuitThresholds,
) -> CircuitBreakerResult:
    """Evaluate realized losses through ``evaluated_at`` using ET boundaries."""
    if not trades:
        return CircuitBreakerResult(
            CircuitState.TRADING_ALLOWED, 0.0, 0.0, 0.0, 0, (), "EMPTY_STATE"
        )
    if (
        account_equity is None
        or account_equity <= 0
        or not math.isfinite(account_equity)
        or evaluated_at.tzinfo is None
        or any(
            trade.closed_at is None
            or trade.closed_at.tzinfo is None
            or trade.pnl_usd is None
            or not math.isfinite(trade.pnl_usd)
            for trade in trades
        )
    ):
        return CircuitBreakerResult(
            CircuitState.HALTED,
            None,
            None,
            None,
            0,
            ("DATA_QUALITY_PARTIAL",),
            "PARTIAL",
        )

    cutoff = evaluated_at.astimezone(UTC)
    valid = sorted(
        (
            trade
            for trade in trades
            if trade.closed_at is not None
            and trade.pnl_usd is not None
            and trade.closed_at.astimezone(UTC) <= cutoff
        ),
        key=lambda trade: trade.closed_at or cutoff,
    )
    local_cutoff = evaluated_at.astimezone(_ET)
    local_day = as_of
    day_start = datetime.combine(local_day, time.min, tzinfo=_ET)
    week_start = datetime.combine(
        local_day - timedelta(days=local_day.weekday()), time.min, tzinfo=_ET
    )
    month_start = datetime.combine(local_day.replace(day=1), time.min, tzinfo=_ET)

    def loss_pct(start: datetime) -> float:
        pnl = sum(
            trade.pnl_usd or 0.0
            for trade in valid
            if trade.closed_at is not None
            and start <= trade.closed_at.astimezone(_ET) <= local_cutoff
        )
        return max(0.0, -pnl / account_equity * 100)

    daily = loss_pct(day_start)
    weekly = loss_pct(week_start)
    monthly = loss_pct(month_start)
    streak = 0
    last_loss_at: datetime | None = None
    for trade in reversed(valid):
        if (trade.pnl_usd or 0.0) >= 0:
            break
        streak += 1
        if last_loss_at is None:
            last_loss_at = trade.closed_at

    rules: list[str] = []
    if daily >= thresholds.daily_loss_pct:
        rules.append("DAILY_LOSS")
    if weekly >= thresholds.weekly_loss_pct:
        rules.append("WEEKLY_LOSS")
    if monthly >= thresholds.monthly_loss_pct:
        rules.append("MONTHLY_LOSS")
    cooldown_until = (
        last_loss_at + timedelta(hours=thresholds.cooldown_hours)
        if streak >= thresholds.consecutive_losses and last_loss_at is not None
        else None
    )
    if cooldown_until is not None and evaluated_at < cooldown_until:
        rules.append("CONSECUTIVE_LOSSES")

    halted = any(rule.endswith("_LOSS") for rule in rules)
    state = (
        CircuitState.HALTED
        if halted
        else CircuitState.COOLDOWN
        if "CONSECUTIVE_LOSSES" in rules
        else CircuitState.TRADING_ALLOWED
    )
    return CircuitBreakerResult(
        state,
        daily,
        weekly,
        monthly,
        streak,
        tuple(rules),
        "OK",
        cooldown_until,
    )
