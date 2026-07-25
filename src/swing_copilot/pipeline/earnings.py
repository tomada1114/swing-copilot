"""Fail-soft earnings-calendar collection for the daily pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from swing_copilot.data.earnings import EarningsCalendarClient, EarningsEvent

logger = logging.getLogger(__name__)
_LOOKAHEAD_CALENDAR_DAYS = 30
_DISABLED_NOTICE = "NO_EARNINGS_DATA: FINNHUB_API_KEY is not configured"


class _EarningsStore(Protocol):
    def upsert_earnings_calendar(self, events: Sequence[EarningsEvent]) -> None:
        """Correction-upsert fetched events."""
        ...  # pragma: no cover


@dataclass(frozen=True, slots=True)
class EarningsCollection:
    """One run's usable earnings data and guard state."""

    is_enabled: bool
    events_by_symbol: dict[str, EarningsEvent | None]
    notice: str | None = None


def collect_earnings_calendar(
    client: EarningsCalendarClient | None,
    symbols: list[str],
    as_of: date,
    store: _EarningsStore,
) -> EarningsCollection:
    """Fetch each symbol independently so one failure never stops the batch."""
    if client is None:
        return EarningsCollection(False, {}, _DISABLED_NOTICE)
    end = as_of + timedelta(days=_LOOKAHEAD_CALENDAR_DAYS)
    events_by_symbol: dict[str, EarningsEvent | None] = {}
    events: list[EarningsEvent] = []
    for symbol in symbols:
        try:
            event = client.fetch_next_earnings(symbol, as_of, end)
        except Exception:
            logger.exception("earnings calendar fetch failed for %s", symbol)
            event = None
        events_by_symbol[symbol] = event
        if event is not None:
            events.append(event)
    store.upsert_earnings_calendar(events)
    return EarningsCollection(True, events_by_symbol)
