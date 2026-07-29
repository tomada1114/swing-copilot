"""Finnhub earnings-calendar adapter boundary contracts (P4-18)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from swing_copilot.data.earnings_finnhub import (
    EarningsTiming,
    FinnhubEarningsClient,
    _real_http_get,
)


class FakeClock:
    def today(self) -> date:
        return date(2026, 7, 21)

    def now(self) -> datetime:
        return datetime(2026, 7, 21, 12, tzinfo=UTC)


def _payload(earnings_date: str = "2026-07-28") -> dict[str, object]:
    return {
        "earningsCalendar": [{"symbol": "AAPL", "date": earnings_date, "hour": "amc"}]
    }


def test_normalizes_earliest_matching_event_and_query_parameters():
    captured: dict[str, object] = {}

    def fake_get(url, params):
        captured.update(url=url, params=params)
        return _payload()

    client = FinnhubEarningsClient("test-key", http_get=fake_get, clock=FakeClock())

    event = client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))

    assert event is not None
    assert event.symbol == "AAPL"
    assert event.earnings_date == date(2026, 7, 28)
    assert event.session == "amc"
    assert captured["params"] == {
        "symbol": "AAPL",
        "from": "2026-07-21",
        "to": "2026-08-20",
        "token": "test-key",
    }


@pytest.mark.parametrize(
    ("earnings_date", "expected_date"),
    [
        pytest.param("2026-08-19", date(2026, 8, 19), id="immediately-before"),
        pytest.param("2026-08-20", date(2026, 8, 20), id="exactly-at"),
        pytest.param("2026-08-21", None, id="immediately-after"),
    ],
)
def test_filters_event_dates_at_inclusive_end_boundary(earnings_date, expected_date):
    client = FinnhubEarningsClient(
        "test-key",
        http_get=lambda _url, _params: _payload(earnings_date),
        clock=FakeClock(),
    )

    event = client.fetch_next_earnings(
        "AAPL",
        date(2026, 7, 21),
        date(2026, 8, 20),
    )

    assert (None if event is None else event.earnings_date) == expected_date


def test_timeout_retries_to_total_attempt_ceiling_with_deterministic_backoff():
    attempts = 0
    backoffs: list[float] = []

    def timeout_get(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        message = "timed out"
        raise httpx.ReadTimeout(message)

    client = FinnhubEarningsClient(
        "test-key",
        http_get=timeout_get,
        clock=FakeClock(),
        timing=EarningsTiming(
            rate_clock=lambda: 10.0,
            sleep_fn=lambda _seconds: None,
            backoff_fn=backoffs.append,
        ),
    )

    with pytest.raises(httpx.ReadTimeout, match="timed out"):
        client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))

    assert attempts == 3
    assert backoffs == [1.0, 2.0]


def test_rate_limit_is_applied_before_every_retry_attempt():
    times = iter([0.0, 0.2, 0.4])
    throttle_sleeps: list[float] = []
    attempts = 0

    def flaky_get(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            message = "temporary"
            raise httpx.ConnectError(message)
        return _payload()

    client = FinnhubEarningsClient(
        "test-key",
        http_get=flaky_get,
        clock=FakeClock(),
        timing=EarningsTiming(
            rate_clock=lambda: next(times),
            sleep_fn=throttle_sleeps.append,
            backoff_fn=lambda _seconds: None,
        ),
    )

    event = client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))

    assert event is not None
    assert attempts == 3
    assert throttle_sleeps == pytest.approx([0.8, 0.8])


def test_client_error_is_not_retried():
    attempts = 0
    request = httpx.Request("GET", "https://example.com")

    def bad_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        response = httpx.Response(400, request=request)
        message = "bad request"
        raise httpx.HTTPStatusError(message, request=request, response=response)

    client = FinnhubEarningsClient(
        "test-key",
        http_get=bad_request,
        clock=FakeClock(),
        timing=EarningsTiming(
            rate_clock=lambda: 10.0,
            sleep_fn=lambda _seconds: None,
            backoff_fn=lambda _seconds: None,
        ),
    )

    with pytest.raises(httpx.HTTPStatusError, match="bad request"):
        client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))

    assert attempts == 1


def test_real_http_boundary_uses_explicit_ten_second_timeout(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"earningsCalendar": []}

    def fake_get(url, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr("swing_copilot.data.earnings_finnhub.httpx.get", fake_get)

    result = _real_http_get("https://example.com", {"symbol": "AAPL"})

    assert result == {"earningsCalendar": []}
    assert captured["timeout"] == 10.0
