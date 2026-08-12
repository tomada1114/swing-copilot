"""Pipeline fail-soft collection for the earnings guard (P4-18, P8-115)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from swing_copilot.data.earnings import EarningsEvent
from swing_copilot.pipeline.earnings import collect_earnings_calendar

_LOOKAHEAD_DAYS = 45


class FakeStore:
    def __init__(self, recent_events: dict[str, EarningsEvent] | None = None):
        self.events: list[EarningsEvent] = []
        self._recent_events = recent_events or {}

    def upsert_earnings_calendar(self, events):
        self.events.extend(events)

    def get_earnings_event(self, symbol):
        return self._recent_events.get(symbol)


class FakeClient:
    """FAIL raises, EMPTY returns no match, everything else is found."""

    def __init__(self):
        self.calls = []

    def fetch_next_earnings(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        del start
        if symbol == "FAIL":
            message = "all attempts exhausted"
            raise httpx.ReadTimeout(message)
        if symbol == "EMPTY":
            return None
        return EarningsEvent(
            symbol,
            date(2026, 7, 28),
            "amc",
            datetime(2026, 7, 21, 12, tzinfo=UTC),
        )


def test_missing_api_key_disables_whole_guard_with_reason():
    result = collect_earnings_calendar(
        None, ["AAPL"], date(2026, 7, 21), FakeStore(), lookahead_days=_LOOKAHEAD_DAYS
    )
    assert result.is_enabled is False
    assert result.notice == "NO_EARNINGS_DATA: FINNHUB_API_KEY is not configured"
    assert result.lookups_by_symbol == {}


def test_a_fetch_failure_is_marked_fetch_failed_while_other_symbols_continue():
    store = FakeStore()
    result = collect_earnings_calendar(
        FakeClient(),
        ["FAIL", "AAPL"],
        date(2026, 7, 21),
        store,
        lookahead_days=_LOOKAHEAD_DAYS,
    )

    assert result.is_enabled is True
    failed = result.lookups_by_symbol["FAIL"]
    assert failed.status == "fetch_failed"
    assert failed.event is None
    found = result.lookups_by_symbol["AAPL"]
    assert found.status == "found"
    assert found.event is not None
    # Only the successfully found event is upserted; the failed fetch leaves
    # the stored calendar row untouched.
    assert [event.symbol for event in store.events] == ["AAPL"]


def test_no_match_in_window_is_marked_none_in_window_and_not_upserted():
    store = FakeStore()
    result = collect_earnings_calendar(
        FakeClient(),
        ["EMPTY"],
        date(2026, 7, 21),
        store,
        lookahead_days=_LOOKAHEAD_DAYS,
    )

    lookup = result.lookups_by_symbol["EMPTY"]
    assert lookup.status == "none_in_window"
    assert lookup.event is None
    assert store.events == []


def test_lookahead_days_sets_the_query_window_end_date():
    client = FakeClient()
    as_of = date(2026, 7, 21)

    collect_earnings_calendar(
        client, ["AAPL"], as_of, FakeStore(), lookahead_days=_LOOKAHEAD_DAYS
    )

    (_symbol, start, end) = client.calls[0]
    assert start == as_of
    assert end == as_of + timedelta(days=_LOOKAHEAD_DAYS)


def test_recent_event_is_read_regardless_of_this_runs_fetch_outcome():
    recent = EarningsEvent(
        "FAIL", date(2026, 7, 16), "amc", datetime(2026, 7, 9, tzinfo=UTC)
    )
    store = FakeStore({"FAIL": recent, "EMPTY": recent, "AAPL": recent})

    result = collect_earnings_calendar(
        FakeClient(),
        ["FAIL", "EMPTY", "AAPL"],
        date(2026, 7, 21),
        store,
        lookahead_days=_LOOKAHEAD_DAYS,
    )

    assert result.lookups_by_symbol["FAIL"].recent_event == recent
    assert result.lookups_by_symbol["EMPTY"].recent_event == recent
    assert result.lookups_by_symbol["AAPL"].recent_event == recent


def test_no_stored_recent_event_leaves_it_none():
    result = collect_earnings_calendar(
        FakeClient(),
        ["AAPL"],
        date(2026, 7, 21),
        FakeStore(),
        lookahead_days=_LOOKAHEAD_DAYS,
    )

    assert result.lookups_by_symbol["AAPL"].recent_event is None


@pytest.mark.parametrize(
    "as_of",
    [
        pytest.param(date(2026, 7, 20), id="immediately-before"),
        pytest.param(date(2026, 7, 21), id="exactly-at"),
        pytest.param(date(2026, 7, 22), id="immediately-after"),
    ],
)
def test_historical_replay_never_calls_current_earnings_source(as_of):
    client = FakeClient()
    store = FakeStore()

    result = collect_earnings_calendar(
        client,
        ["AAPL"],
        as_of,
        store,
        lookahead_days=_LOOKAHEAD_DAYS,
        is_historical=True,
    )

    assert result.is_enabled is False
    assert result.lookups_by_symbol == {}
    assert result.notice is not None
    assert "historical replay" in result.notice
    assert client.calls == []
    assert store.events == []
