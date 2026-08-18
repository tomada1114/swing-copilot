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
        return 0  # only `event_date == as_of` reaches here (Issue #231)
    cursor = as_of + timedelta(days=1)
    count = 0
    while cursor <= event_date:
        if cursor.weekday() < _WEEKEND_START:
            count += 1
        cursor += timedelta(days=1)
    return count


def business_days_since(as_of: date, event_date: date) -> int:
    """Business days from `event_date` (exclusive) through `as_of` (inclusive).

    The mirror of `_business_days_until`'s counting style, used to classify
    how recently a symbol reported rather than how soon it will (P8-115).
    """
    if event_date >= as_of:
        return 0
    cursor = event_date + timedelta(days=1)
    count = 0
    while cursor <= as_of:
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
    """Classify an earnings date using inclusive weekday-only boundaries.

    An `earnings_date` strictly before `as_of` is stale rather than imminent:
    the supplier handed back an estimate the point-in-time cutoff has already
    overtaken. Classifying it by business-day distance would yield `0` and
    therefore `block`, which would keep the symbol blocked indefinitely until
    some later run happened to supply a fresh event (Issue #231). "Stale" is
    measured only against the caller's explicit `as_of`; no wall clock is
    consulted here.

    Args:
        as_of: Point-in-time cutoff for this classification.
        earnings_date: The next known earnings date, or `None` if unknown.
        block_business_days: Inclusive business-day distance that blocks entry.
        warn_business_days: Inclusive business-day distance that warns.

    Returns:
        The classification, with `business_days` `None` whenever the date is
        unusable (absent or stale).
    """
    if earnings_date is None:
        return EarningsProximity("unknown", None)
    if earnings_date < as_of:
        # Consumer-side defense layer: fail toward "we don't know" rather than
        # the fail-closed `block`, matching how the supplier-side calendars
        # already demote a projection `as_of` has overtaken. The `== as_of`
        # boundary is deliberately *not* stale -- reporting today is exactly
        # the event the guard exists to keep entries away from.
        return EarningsProximity("unknown", None)
    business_days = _business_days_until(as_of, earnings_date)
    if business_days <= block_business_days:
        return EarningsProximity("block", business_days)
    if business_days <= warn_business_days:
        return EarningsProximity("warn", business_days)
    return EarningsProximity("clear", business_days)
