"""Normalized earnings-calendar boundary values (P4-18)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

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


#: Why a symbol's lookahead fetch produced the `event` it did (P8-115):
#: an event was found in the window, the window was searched but empty, or
#: the fetch itself failed. `none_in_window` and `fetch_failed` both leave
#: `event` `None`, but only the latter is a genuine "we don't know" -- the
#: distinction is what lets the risk guard warn on one and stay silent on
#: the other.
EarningsLookupStatus = Literal["found", "none_in_window", "fetch_failed"]


@dataclass(frozen=True, slots=True)
class EarningsLookup:
    """One symbol's earnings-guard input for one run.

    `event` is the upcoming event within the lookahead window when `status`
    is `"found"`, else `None`. `recent_event` is independent of `status`: the
    last event `earnings_calendar` has on record for the symbol (`symbol` is
    its primary key, so this is "the most recently known event", not a
    history), whether that event is in the future, in the past, or absent.
    """

    status: EarningsLookupStatus
    event: EarningsEvent | None
    recent_event: EarningsEvent | None
