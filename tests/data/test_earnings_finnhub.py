"""Finnhub earnings-calendar adapter boundary contracts (P4-18)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from swing_copilot.data.earnings_finnhub import (
    _MIN_REQUEST_INTERVAL_SECONDS,
    EarningsTiming,
    FinnhubEarningsClient,
    _real_http_get,
)
from tests.conftest import ThrottleTimeline


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


def test_non_list_calendar_fails_instead_of_reading_as_no_earnings():
    # `pipeline/earnings.py` turns a raised exception into `fetch_failed` and a
    # `None` return into `none_in_window`, and the risk guard warns on the
    # first while staying silent on the second. A malformed payload must take
    # the loud path: silently reading it as "nothing scheduled" would drop the
    # earnings-proximity guard for a symbol that is about to report.
    client = FinnhubEarningsClient(
        "test-key",
        http_get=lambda _url, _params: {"earningsCalendar": {"symbol": "AAPL"}},
        clock=FakeClock(),
    )

    with pytest.raises(TypeError, match="earningsCalendar response must be a list"):
        client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))


def test_unusable_calendar_items_are_skipped_without_hiding_a_real_event():
    # The endpoint answers for the whole window, so rows for other symbols and
    # rows with a missing or non-string date do arrive. Skipping them keeps the
    # symbol's own event reachable; failing on them would report `fetch_failed`
    # for a symbol whose date was right there in the same response.
    payload = {
        "earningsCalendar": [
            "not-an-object",
            {"symbol": "MSFT", "date": "2026-07-22", "hour": "amc"},
            {"symbol": "AAPL", "hour": "bmo"},
            {"symbol": "AAPL", "date": None, "hour": "bmo"},
            {"symbol": "AAPL", "date": "2026-08-04", "hour": "amc"},
            {"symbol": "AAPL", "date": "2026-07-28", "hour": "bmo"},
        ]
    }
    client = FinnhubEarningsClient(
        "test-key",
        http_get=lambda _url, _params: payload,
        clock=FakeClock(),
    )

    event = client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))

    assert event is not None
    assert (event.symbol, event.earnings_date, event.session) == (
        "AAPL",
        date(2026, 7, 28),
        "bmo",
    )


def test_no_events_for_the_symbol_reads_as_an_empty_window():
    client = FinnhubEarningsClient(
        "test-key",
        http_get=lambda _url, _params: {
            "earningsCalendar": [
                {"symbol": "MSFT", "date": "2026-07-28", "hour": "amc"}
            ]
        },
        clock=FakeClock(),
    )

    assert (
        client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20)) is None
    )


def test_rate_limit_does_not_sleep_once_the_interval_has_already_elapsed():
    # The complement of the throttle-on-every-attempt contract: the limiter
    # must only wait out the remainder of the interval, never impose a fixed
    # delay on calls that are already far enough apart.
    throttle_sleeps: list[float] = []
    times = iter([0.0, 5.0])
    client = FinnhubEarningsClient(
        "test-key",
        http_get=lambda _url, _params: _payload(),
        clock=FakeClock(),
        timing=EarningsTiming(
            rate_clock=lambda: next(times),
            sleep_fn=throttle_sleeps.append,
            backoff_fn=lambda _seconds: None,
        ),
    )

    client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))
    client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))

    assert throttle_sleeps == []


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
    # Ticks model a 0.2s request on a clock that the throttle's own sleep
    # advances (`backoff_fn` is inert so the throttle stays the only thing
    # under test): attempt 1 throttles at 0.0, attempt 2 re-enters the throttle
    # 0.2s later and is held until one interval, attempt 3 re-enters at 1.2.
    times = iter([0.0, 0.2, 1.2])
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
    assert throttle_sleeps == pytest.approx(
        [
            _MIN_REQUEST_INTERVAL_SECONDS - 0.2,
            (2 * _MIN_REQUEST_INTERVAL_SECONDS) - 1.2,
        ]
    )


def test_successive_requests_are_issued_at_least_one_interval_apart():
    """Issue #253: the throttle must count from the request it let through.

    Recording the pre-sleep clock reading dropped the slept interval, so every
    other request went out early and the effective rate exceeded the 60/minute
    cap. Asserted on issue instants, not on sleep arguments.
    """
    timeline = ThrottleTimeline(request_seconds=0.3)

    def timed_get(*_args, **_kwargs):
        timeline.issue_request()
        return _payload()

    client = FinnhubEarningsClient(
        "test-key",
        http_get=timed_get,
        clock=FakeClock(),
        timing=EarningsTiming(
            rate_clock=timeline.clock,
            sleep_fn=timeline.sleep,
            backoff_fn=timeline.sleep,
        ),
    )

    for _ in range(3):
        client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))

    assert timeline.issue_gaps == [pytest.approx(_MIN_REQUEST_INTERVAL_SECONDS)] * 2


def test_retried_attempts_keep_the_minimum_issue_interval():
    """Issue #253: a retry attempt is a request and resets the same clock.

    `before_attempt` fires once per attempt, so the failed attempt counts
    against the rate limit too; every subsequent request must still be at least
    one interval behind the one actually issued before it.
    """
    timeline = ThrottleTimeline(request_seconds=0.3)
    attempts = 0

    def flaky_get(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        timeline.issue_request()
        if attempts == 1:
            message = "temporary"
            raise httpx.ConnectError(message)
        return _payload()

    client = FinnhubEarningsClient(
        "test-key",
        http_get=flaky_get,
        clock=FakeClock(),
        timing=EarningsTiming(
            rate_clock=timeline.clock,
            sleep_fn=timeline.sleep,
            backoff_fn=timeline.sleep,
        ),
    )

    for _ in range(3):
        client.fetch_next_earnings("AAPL", date(2026, 7, 21), date(2026, 8, 20))

    # Four issued requests: the failure, its retry, then one per later fetch.
    assert attempts == 4
    assert timeline.gaps_below(_MIN_REQUEST_INTERVAL_SECONDS) == []


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
