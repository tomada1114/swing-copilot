"""Deterministic earnings-proximity classification (P4-18)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

_WEEKEND_START = 5


@dataclass(frozen=True, slots=True)
class EarningsProximity:
    """Candidate event-risk classification."""

    status: str  # "block" | "warn" | "clear" | "unknown"
    business_days: int | None


def _business_days_until(as_of: date, event_date: date) -> int:
    if event_date <= as_of:
        return 0
    cursor = as_of + timedelta(days=1)
    count = 0
    while cursor <= event_date:
        if cursor.weekday() < _WEEKEND_START:
            count += 1
        cursor += timedelta(days=1)
    return count


def evaluate_earnings_proximity(
    as_of: date,
    earnings_date: date | None,
    *,
    block_business_days: int,
    warn_business_days: int,
) -> EarningsProximity:
    """Classify an earnings date using inclusive weekday-only boundaries."""
    if earnings_date is None:
        return EarningsProximity("unknown", None)
    business_days = _business_days_until(as_of, earnings_date)
    if business_days <= block_business_days:
        return EarningsProximity("block", business_days)
    if business_days <= warn_business_days:
        return EarningsProximity("warn", business_days)
    return EarningsProximity("clear", business_days)
