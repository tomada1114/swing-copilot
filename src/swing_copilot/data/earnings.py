"""Normalized earnings-calendar boundary values (P4-18)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class EarningsEvent:
    """One symbol's next known earnings event."""

    symbol: str
    earnings_date: date
    session: str
    fetched_at: datetime


class EarningsCalendarClient(Protocol):
    """External earnings-calendar port."""

    def fetch_next_earnings(
        self, symbol: str, start: date, end: date
    ) -> EarningsEvent | None:
        """Fetch the earliest event for `symbol` in inclusive `[start, end]`."""
        ...  # pragma: no cover
