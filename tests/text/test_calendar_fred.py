"""Tests for FredCalendarClient (FR-07)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from swing_copilot.text.calendar_fred import FredCalendarClient, _real_http_get


class FakeClock:
    def now(self):
        return datetime(2027, 2, 1, 12, tzinfo=UTC)

    def today(self):
        return date(2027, 2, 1)


def _fake_response(*_args, **_kwargs):
    return {
        "release_dates": [
            {
                "release_id": 50,
                "release_name": "Employment Situation",
                "date": "2027-02-05",
            },
        ]
    }


class TestFetchCalendarEvents:
    def test_normalizes_to_text_item_schema(self):
        client = FredCalendarClient(
            "test-key", http_get=_fake_response, clock=FakeClock()
        )

        items = client.fetch_calendar_events(date(2027, 2, 1), date(2027, 2, 28))

        assert len(items) == 1
        item = items[0]
        assert item.source_id == "fred:50:2027-02-05"
        assert item.symbol is None
        assert item.source_type == "calendar"
        assert item.title == "Employment Situation"
        assert item.published_at.date() == date(2027, 2, 5)
        assert item.fetched_at == datetime(2027, 2, 1, 12, tzinfo=UTC)

    def test_passes_date_range_as_query_params(self):
        captured = {}

        def capturing_get(url, params):
            captured["params"] = params
            return {"release_dates": []}

        client = FredCalendarClient("test-key", http_get=capturing_get)
        client.fetch_calendar_events(date(2027, 2, 1), date(2027, 2, 28))

        assert captured["params"]["realtime_start"] == "2027-02-01"
        assert captured["params"]["realtime_end"] == "2027-02-28"
        assert captured["params"]["api_key"] == "test-key"

    def test_empty_response_returns_empty_list(self):
        client = FredCalendarClient(
            "test-key", http_get=lambda *_a, **_k: {"release_dates": []}
        )
        assert client.fetch_calendar_events(date(2027, 2, 1), date(2027, 2, 28)) == []


class TestRealHttpGet:
    def test_parses_json_response_from_httpx(self, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"release_dates": []}

        monkeypatch.setattr(
            "swing_copilot.text.calendar_fred.httpx.get",
            lambda url, params, timeout: FakeResponse(),  # noqa: ARG005 - matching httpx.get's call shape
        )

        result = _real_http_get("https://example.com", {"a": "b"})

        assert result == {"release_dates": []}


def _make_status_error(message: str, status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


class _CountingFailThenSucceed:
    """Fails with `error` for `fail_times` calls, then returns `payload`."""

    def __init__(
        self, error: Exception, fail_times: int, payload: dict[str, Any]
    ) -> None:
        self._error = error
        self._fail_times = fail_times
        self._payload = payload
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        return self._payload


class _AlwaysFail:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        raise self._error


class TestFetchCalendarEventsRetry:
    def test_transient_failure_twice_then_success_returns_events(self):
        http_get = _CountingFailThenSucceed(
            httpx.ConnectError("boom"),
            fail_times=2,
            payload={
                "release_dates": [
                    {
                        "release_id": 50,
                        "release_name": "Employment Situation",
                        "date": "2027-02-05",
                    },
                ]
            },
        )
        sleeps: list[float] = []
        client = FredCalendarClient(
            "test-key", http_get=http_get, sleep_fn=sleeps.append
        )

        items = client.fetch_calendar_events(date(2027, 2, 1), date(2027, 2, 28))

        assert len(items) == 1
        assert http_get.calls == 3
        assert sleeps == [1.0, 2.0]

    def test_persistent_transient_failure_exhausts_retries_and_propagates(self):
        http_get = _AlwaysFail(httpx.ConnectError("boom"))
        sleeps: list[float] = []
        client = FredCalendarClient(
            "test-key", http_get=http_get, sleep_fn=sleeps.append
        )

        with pytest.raises(httpx.ConnectError):
            client.fetch_calendar_events(date(2027, 2, 1), date(2027, 2, 28))

        assert http_get.calls == 3
        assert sleeps == [1.0, 2.0]

    def test_client_error_status_propagates_without_retry(self):
        error = _make_status_error("Unauthorized", 401)
        http_get = _AlwaysFail(error)
        sleeps: list[float] = []
        client = FredCalendarClient(
            "test-key", http_get=http_get, sleep_fn=sleeps.append
        )

        with pytest.raises(httpx.HTTPStatusError):
            client.fetch_calendar_events(date(2027, 2, 1), date(2027, 2, 28))

        assert http_get.calls == 1
        assert sleeps == []

    def test_server_error_status_is_retried(self):
        http_get = _CountingFailThenSucceed(
            _make_status_error("Server Error", 503),
            fail_times=2,
            payload={"release_dates": []},
        )
        sleeps: list[float] = []
        client = FredCalendarClient(
            "test-key", http_get=http_get, sleep_fn=sleeps.append
        )

        items = client.fetch_calendar_events(date(2027, 2, 1), date(2027, 2, 28))

        assert items == []
        assert http_get.calls == 3
        assert sleeps == [1.0, 2.0]

    def test_malformed_response_body_propagates_without_retry(self):
        def malformed_response(*_args, **_kwargs):
            return {"release_dates": [{"release_id": 50}]}  # missing "date"

        sleeps: list[float] = []
        client = FredCalendarClient(
            "test-key", http_get=malformed_response, sleep_fn=sleeps.append
        )

        with pytest.raises(KeyError):
            client.fetch_calendar_events(date(2027, 2, 1), date(2027, 2, 28))

        assert sleeps == []
