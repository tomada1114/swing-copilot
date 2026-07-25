"""Pipeline fail-soft collection for the earnings guard (P4-18)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx

from swing_copilot.data.earnings import EarningsEvent
from swing_copilot.pipeline.earnings import collect_earnings_calendar


class FakeStore:
    def __init__(self):
        self.events: list[EarningsEvent] = []

    def upsert_earnings_calendar(self, events):
        self.events.extend(events)


class FakeClient:
    def fetch_next_earnings(self, symbol, start, end):
        del start, end
        if symbol == "FAIL":
            message = "all attempts exhausted"
            raise httpx.ReadTimeout(message)
        return EarningsEvent(
            symbol,
            date(2026, 7, 28),
            "amc",
            datetime(2026, 7, 21, 12, tzinfo=UTC),
        )


def test_missing_api_key_disables_whole_guard_with_reason():
    result = collect_earnings_calendar(None, ["AAPL"], date(2026, 7, 21), FakeStore())
    assert result.is_enabled is False
    assert result.notice == "NO_EARNINGS_DATA: FINNHUB_API_KEY is not configured"
    assert result.events_by_symbol == {}


def test_exhausted_symbol_is_unknown_while_other_symbols_continue():
    store = FakeStore()
    result = collect_earnings_calendar(
        FakeClient(), ["FAIL", "AAPL"], date(2026, 7, 21), store
    )

    assert result.is_enabled is True
    assert result.events_by_symbol["FAIL"] is None
    assert result.events_by_symbol["AAPL"] is not None
    assert [event.symbol for event in store.events] == ["AAPL"]
