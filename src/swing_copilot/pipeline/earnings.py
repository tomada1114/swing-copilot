"""Fail-soft earnings-calendar collection for the daily pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from swing_copilot.data.earnings import EarningsLookup

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from swing_copilot.data.earnings import EarningsCalendarClient, EarningsEvent

logger = logging.getLogger(__name__)
_DISABLED_NOTICE = "NO_EARNINGS_DATA: FINNHUB_API_KEY is not configured"
_HISTORICAL_NOTICE = (
    "NO_EARNINGS_DATA: historical replay cannot reconstruct what earnings "
    "dates were known at the requested as_of"
)


class _EarningsStore(Protocol):
    def upsert_earnings_calendar(self, events: Sequence[EarningsEvent]) -> None:
        """Correction-upsert fetched events."""
        ...  # pragma: no cover

    def get_earnings_event(self, symbol: str) -> EarningsEvent | None:
        """Return the last known event for `symbol`, if any."""
        ...  # pragma: no cover


@dataclass(frozen=True, slots=True)
class EarningsCollection:
    """One run's usable earnings data and guard state."""

    is_enabled: bool
    lookups_by_symbol: dict[str, EarningsLookup]
    notice: str | None = None


# PLR0913: the client/store pair plus symbols/as_of/lookahead_days are each
# independently meaningful to a caller; bundling would only add construction
# noise at the one call site that matters, pipeline/daily.py.
def collect_earnings_calendar(  # noqa: PLR0913
    client: EarningsCalendarClient | None,
    symbols: list[str],
    as_of: date,
    store: _EarningsStore,
    *,
    lookahead_days: int,
    is_historical: bool = False,
) -> EarningsCollection:
    """Fetch current data, or fail soft when point-in-time truth is unavailable.

    Args:
        client: External earnings-calendar port, or `None` when disabled.
        symbols: Symbols to look up.
        as_of: Inclusive window start, and the point-in-time cutoff.
        store: Correction-upsert target and last-known-event source.
        lookahead_days: Calendar days ahead of `as_of` to search
            (`RiskConfig.earnings_lookahead_days`).
        is_historical: `True` for a historical replay, which cannot
            reconstruct what was known as of a past `as_of`.

    Returns:
        Every requested symbol's `found`/`none_in_window`/`fetch_failed`
        status, its upcoming event when found, and its last known event from
        storage regardless of this run's fetch outcome.
    """
    if is_historical:
        return EarningsCollection(False, {}, _HISTORICAL_NOTICE)
    if client is None:
        return EarningsCollection(False, {}, _DISABLED_NOTICE)
    end = as_of + timedelta(days=lookahead_days)
    lookups_by_symbol: dict[str, EarningsLookup] = {}
    events: list[EarningsEvent] = []
    for symbol in symbols:
        recent_event = store.get_earnings_event(symbol)
        try:
            event = client.fetch_next_earnings(symbol, as_of, end)
        except Exception:
            logger.exception("earnings calendar fetch failed for %s", symbol)
            lookups_by_symbol[symbol] = EarningsLookup(
                "fetch_failed", None, recent_event
            )
            continue
        if event is None:
            lookups_by_symbol[symbol] = EarningsLookup(
                "none_in_window", None, recent_event
            )
            continue
        lookups_by_symbol[symbol] = EarningsLookup("found", event, recent_event)
        events.append(event)
    store.upsert_earnings_calendar(events)
    return EarningsCollection(True, lookups_by_symbol)
