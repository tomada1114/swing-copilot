"""Tests for FredCalendarClient (FR-07, Issue #82)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import pairwise
from typing import Any

import httpx
import pytest

from swing_copilot.text.calendar_fred import (
    _MIN_REQUEST_INTERVAL_SECONDS,
    FRED_RELEASE_DATES_URL,
    FRED_RELEASE_SERIES_URL,
    FRED_SERIES_OBSERVATIONS_URL,
    FredCalendarClient,
    FredCalendarTiming,
    _real_http_get,
)

AS_OF = date(2027, 2, 1)
RANGE_END = date(2027, 2, 28)


class FakeClock:
    def now(self):
        return datetime(2027, 2, 1, 12, tzinfo=UTC)

    def today(self):
        return date(2027, 2, 1)


class SpacedRateClock:
    """Monotonic clock whose every tick is past the throttle interval.

    Keeps rate limiting inert so retry-backoff assertions stay readable; the
    throttle itself is asserted separately with a clock returning close ticks.
    """

    def __init__(self, step: float = 10.0) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


class ThrottleTimeline:
    """Monotonic clock that only sleeping and request latency advance.

    Models the one timeline the rate limit is actually defined over: `sleep`
    (throttle wait and retry backoff alike) and each request's round trip both
    move it forward, and every issued request stamps the instant it went out.
    That makes the *issue* interval observable, not just the sleep arguments.
    """

    def __init__(self, request_seconds: float) -> None:
        self.now = 0.0
        self.issued_at: list[float] = []
        self._request_seconds = request_seconds

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def issue_request(self) -> None:
        self.issued_at.append(self.now)
        self.now += self._request_seconds

    @property
    def issue_gaps(self) -> list[float]:
        return [later - earlier for earlier, later in pairwise(self.issued_at)]

    def gaps_below(self, minimum: float, tolerance: float = 1e-9) -> list[float]:
        """Return every issue gap shorter than `minimum`, float slop aside."""
        return [gap for gap in self.issue_gaps if gap < minimum - tolerance]


class ScriptedRateClock:
    """Returns the scripted ticks in order, then repeats the last one."""

    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)
        self._last = ticks[-1]

    def __call__(self) -> float:
        if self._ticks:
            self._last = self._ticks.pop(0)
        return self._last


def _release_dates_payload() -> dict[str, Any]:
    return {
        "release_dates": [
            {
                "release_id": 50,
                "release_name": "Employment Situation",
                "date": "2027-02-05",
            },
        ]
    }


SERIES_PAYLOAD = {
    "seriess": [
        {
            "id": "PAYEMS",
            "title": "All Employees, Total Nonfarm",
            "units_short": "Thous. of Persons",
        }
    ]
}

OBSERVATIONS_PAYLOAD = {
    "observations": [
        {"date": "2027-01-01", "value": "158200.0"},
        {"date": "2026-12-01", "value": "158000.0"},
    ]
}


class FakeFred:
    """Offline stand-in for the three FRED endpoints, recording every call."""

    def __init__(
        self,
        *,
        release_dates: dict[str, Any] | None = None,
        series: dict[str, Any] | None = None,
        observations: dict[str, Any] | None = None,
        fail_on: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self._payloads = {
            FRED_RELEASE_DATES_URL: release_dates or _release_dates_payload(),
            FRED_RELEASE_SERIES_URL: series if series is not None else SERIES_PAYLOAD,
            FRED_SERIES_OBSERVATIONS_URL: (
                observations if observations is not None else OBSERVATIONS_PAYLOAD
            ),
        }
        self._fail_on = fail_on
        self._error = error or httpx.ConnectError("boom")
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, params))
        if self._fail_on == url:
            raise self._error
        return self._payloads[url]

    def params_for(self, url: str) -> dict[str, Any]:
        return next(params for called, params in self.calls if called == url)

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


class _FailFirstFred(FakeFred):
    """Fails the first `releases/dates` attempt, then behaves normally."""

    def __init__(self) -> None:
        super().__init__()
        self._has_failed = False

    def __call__(self, url, params):
        if url == FRED_RELEASE_DATES_URL and not self._has_failed:
            self._has_failed = True
            self.calls.append((url, params))
            msg = "boom"
            raise httpx.ConnectError(msg)
        return super().__call__(url, params)


class _TimedFred(FakeFred):
    """`FakeFred` that stamps every request onto a `ThrottleTimeline`."""

    def __init__(self, timeline: ThrottleTimeline, *, fail_first: bool = False) -> None:
        super().__init__()
        self._timeline = timeline
        self._fail_first = fail_first

    def __call__(self, url, params):
        self._timeline.issue_request()
        if self._fail_first and url == FRED_RELEASE_DATES_URL:
            self._fail_first = False
            self.calls.append((url, params))
            msg = "boom"
            raise httpx.ConnectError(msg)
        return super().__call__(url, params)


def _client(http_get: Any, **kwargs: Any) -> FredCalendarClient:
    """Build a client whose clocks are fake and whose throttle stays inert."""
    return FredCalendarClient(
        "test-key",
        http_get=http_get,
        timing=FredCalendarTiming(
            clock=FakeClock(),
            rate_clock=SpacedRateClock(),
            sleep_fn=kwargs.pop("sleep_fn", lambda _seconds: None),
        ),
        **kwargs,
    )


class TestFetchCalendarEvents:
    def test_normalizes_to_text_item_schema(self):
        client = _client(FakeFred())

        items = client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert len(items) == 1
        item = items[0]
        assert item.source_id == "fred:50:2027-02-05"
        assert item.symbol is None
        assert item.source_type == "calendar"
        assert item.title == "Employment Situation"
        assert item.published_at.date() == date(2027, 2, 5)
        assert item.fetched_at == datetime(2027, 2, 1, 12, tzinfo=UTC)

    def test_passes_date_range_as_query_params(self):
        fred = FakeFred(release_dates={"release_dates": []})

        _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        params = fred.params_for(FRED_RELEASE_DATES_URL)
        assert params["realtime_start"] == "2027-02-01"
        assert params["realtime_end"] == "2027-02-28"
        assert params["api_key"] == "test-key"

    def test_empty_response_returns_empty_list_without_value_lookups(self):
        fred = FakeFred(release_dates={"release_dates": []})

        assert _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF) == []
        assert fred.urls() == [FRED_RELEASE_DATES_URL]


class TestSummaryEnrichment:
    def test_chains_release_series_then_observations_into_the_summary(self):
        fred = FakeFred()

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert fred.urls() == [
            FRED_RELEASE_DATES_URL,
            FRED_RELEASE_SERIES_URL,
            FRED_SERIES_OBSERVATIONS_URL,
        ]
        assert fred.params_for(FRED_RELEASE_SERIES_URL)["release_id"] == 50
        assert items[0].content_text == (
            "Scheduled for 2027-02-05: Employment Situation (FRED release 50). "
            "Representative series PAYEMS (All Employees, Total Nonfarm): "
            "latest 2027-01-01 = 158200.0 Thous. of Persons, "
            "prior 2026-12-01 = 158000.0 Thous. of Persons (change +200). "
            "Market consensus is not published by FRED."
        )

    def test_summary_is_never_identical_to_the_title(self):
        items = _client(FakeFred()).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert items[0].content_text != items[0].title

    def test_observation_request_is_bounded_by_as_of(self):
        fred = FakeFred()

        _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        params = fred.params_for(FRED_SERIES_OBSERVATIONS_URL)
        assert params["observation_end"] == "2027-02-01"

    @pytest.mark.parametrize(
        ("observed_on", "expected_latest"),
        [
            pytest.param("2027-01-31", "2027-01-31", id="just-before-as-of"),
            pytest.param("2027-02-01", "2027-02-01", id="exactly-at-as-of"),
            pytest.param("2027-02-02", "2026-12-01", id="just-after-as-of-dropped"),
        ],
    )
    def test_observations_after_as_of_are_excluded(self, observed_on, expected_latest):
        fred = FakeFred(
            observations={
                "observations": [
                    {"date": observed_on, "value": "158200.0"},
                    {"date": "2026-12-01", "value": "158000.0"},
                ]
            }
        )

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert f"latest {expected_latest} =" in items[0].content_text

    def test_missing_observation_values_are_skipped(self):
        fred = FakeFred(
            observations={
                "observations": [
                    {"date": "2027-01-01", "value": "."},
                    {"date": "2026-12-01", "value": "158000.0"},
                    {"date": "2026-11-01", "value": "157000.0"},
                ]
            }
        )

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert "latest 2026-12-01 = 158000.0" in items[0].content_text
        assert "prior 2026-11-01 = 157000.0" in items[0].content_text

    def test_single_observation_reports_prior_as_unavailable(self):
        fred = FakeFred(
            observations={"observations": [{"date": "2027-01-01", "value": "3.5"}]}
        )

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert "latest 2027-01-01 = 3.5" in items[0].content_text
        assert "prior value unavailable" in items[0].content_text

    def test_no_visible_observation_is_stated_explicitly(self):
        fred = FakeFred(observations={"observations": []})

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert "has no observation on or before the as-of date" in items[0].content_text
        assert items[0].content_text != items[0].title

    def test_non_numeric_values_omit_the_change_figure(self):
        fred = FakeFred(
            observations={
                "observations": [
                    {"date": "2027-01-01", "value": "up"},
                    {"date": "2026-12-01", "value": "down"},
                ]
            }
        )

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert "change" not in items[0].content_text
        assert "latest 2027-01-01 = up" in items[0].content_text

    def test_series_without_title_or_units_still_summarizes(self):
        fred = FakeFred(series={"seriess": [{"id": "PAYEMS"}]})

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert (
            "Representative series PAYEMS: latest 2027-01-01 = 158200.0,"
            in items[0].content_text
        )

    def test_release_without_name_falls_back_to_the_release_id(self):
        fred = FakeFred(
            release_dates={"release_dates": [{"release_id": 50, "date": "2027-02-05"}]}
        )

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert items[0].title is None
        assert items[0].content_text.startswith(
            "Scheduled for 2027-02-05: FRED release 50 (FRED release 50)."
        )

    def test_value_lookup_runs_once_per_release_across_dates(self):
        fred = FakeFred(
            release_dates={
                "release_dates": [
                    {
                        "release_id": 50,
                        "release_name": "Employment Situation",
                        "date": "2027-02-05",
                    },
                    {
                        "release_id": 50,
                        "release_name": "Employment Situation",
                        "date": "2027-02-19",
                    },
                ]
            }
        )

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert len(items) == 2
        assert fred.urls().count(FRED_RELEASE_SERIES_URL) == 1
        assert fred.urls().count(FRED_SERIES_OBSERVATIONS_URL) == 1
        assert "2027-02-19" in items[1].content_text

    def test_enrichment_is_capped_at_max_enriched_releases(self):
        fred = FakeFred(
            release_dates={
                "release_dates": [
                    {
                        "release_id": release_id,
                        "release_name": f"Release {release_id}",
                        "date": f"2027-02-{release_id:02d}",
                    }
                    for release_id in (5, 6, 7)
                ]
            }
        )

        items = _client(fred, max_enriched_releases=2).fetch_calendar_events(
            AS_OF, RANGE_END, as_of=AS_OF
        )

        assert fred.urls().count(FRED_RELEASE_SERIES_URL) == 2
        # The newest release dates are enriched first; the oldest degrades.
        oldest = next(item for item in items if item.source_id == "fred:5:2027-02-05")
        assert "Latest and prior values are unavailable" in oldest.content_text


class TestSummaryFailSoft:
    def test_series_lookup_failure_degrades_without_raising(self, caplog):
        fred = FakeFred(fail_on=FRED_RELEASE_SERIES_URL)

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert len(items) == 1
        assert items[0].content_text != items[0].title
        assert "Latest and prior values are unavailable" in items[0].content_text
        assert "FRED value lookup failed for release 50" in caplog.text

    def test_observations_failure_degrades_without_raising(self):
        fred = FakeFred(fail_on=FRED_SERIES_OBSERVATIONS_URL)

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert "Latest and prior values are unavailable" in items[0].content_text

    def test_release_without_any_series_skips_the_observation_call(self):
        fred = FakeFred(series={"seriess": []})

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert FRED_SERIES_OBSERVATIONS_URL not in fred.urls()
        assert "Latest and prior values are unavailable" in items[0].content_text

    def test_malformed_observation_row_degrades_without_raising(self):
        fred = FakeFred(observations={"observations": [{"date": "2027-01-01"}]})

        items = _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert "Latest and prior values are unavailable" in items[0].content_text

    def test_api_key_is_redacted_from_the_failure_log(self, caplog):
        url = "https://api.stlouisfed.org/fred/release/series?api_key=super-secret"
        request = httpx.Request("GET", url)
        fred = FakeFred(
            fail_on=FRED_RELEASE_SERIES_URL,
            error=httpx.HTTPStatusError(
                f"Server error for url {url}",
                request=request,
                response=httpx.Response(500, request=request),
            ),
        )

        _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert "super-secret" not in caplog.text
        assert "api_key=***" in caplog.text

    def test_release_dates_failure_still_propagates(self):
        fred = FakeFred(fail_on=FRED_RELEASE_DATES_URL)

        with pytest.raises(httpx.ConnectError):
            _client(fred).fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)


class TestRateLimiting:
    def test_throttles_every_request_including_retry_attempts(self):
        sleeps: list[float] = []
        # Ticks: `releases/dates` attempt 1 (fails), attempt 2, then the two
        # enrichment requests -- each re-entering the throttle only 0.1s after
        # the previous request was issued, on a clock that the throttle's own
        # sleep advances (the retry backoff is treated as instantaneous so the
        # throttle stays the only thing under test).
        client = FredCalendarClient(
            "test-key",
            http_get=_FailFirstFred(),
            timing=FredCalendarTiming(
                clock=FakeClock(),
                rate_clock=ScriptedRateClock([0.0, 0.1, 0.6, 1.1]),
                sleep_fn=sleeps.append,
            ),
        )

        client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        # 1.0 is the retry backoff after the first attempt failed; the three
        # 0.4s throttle the retry attempt and each of the two enrichment calls.
        assert sleeps == [
            1.0,
            pytest.approx(0.4),
            pytest.approx(0.4),
            pytest.approx(0.4),
        ]

    def test_no_throttle_when_requests_are_already_spaced_out(self):
        sleeps: list[float] = []
        client = FredCalendarClient(
            "test-key",
            http_get=FakeFred(),
            timing=FredCalendarTiming(
                clock=FakeClock(),
                rate_clock=SpacedRateClock(),
                sleep_fn=sleeps.append,
            ),
        )

        client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert sleeps == []

    def test_successive_requests_are_issued_at_least_one_interval_apart(self):
        """Issue #253: the throttle must count from the request it let through.

        Recording the pre-sleep clock reading dropped the slept interval, so
        every other request went out early and the effective rate exceeded
        FRED's 120/minute cap. Asserted on issue instants, not sleep arguments.
        """
        timeline = ThrottleTimeline(request_seconds=0.1)
        client = FredCalendarClient(
            "test-key",
            http_get=_TimedFred(timeline),
            timing=FredCalendarTiming(
                clock=FakeClock(),
                rate_clock=timeline.clock,
                sleep_fn=timeline.sleep,
            ),
        )

        client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        # `releases/dates` plus the two enrichment requests.
        assert len(timeline.issued_at) == 3
        assert timeline.issue_gaps == [pytest.approx(_MIN_REQUEST_INTERVAL_SECONDS)] * 2

    def test_retried_attempts_keep_the_minimum_issue_interval(self):
        """Issue #253: a retry attempt is a request and resets the same clock.

        `before_attempt` fires once per attempt, so the failed attempt counts
        against the rate limit too; every subsequent request must still be at
        least one interval behind the one actually issued before it.
        """
        timeline = ThrottleTimeline(request_seconds=0.1)
        client = FredCalendarClient(
            "test-key",
            http_get=_TimedFred(timeline, fail_first=True),
            timing=FredCalendarTiming(
                clock=FakeClock(),
                rate_clock=timeline.clock,
                sleep_fn=timeline.sleep,
            ),
        )

        client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        # The failed attempt, its retry, and the two enrichment requests.
        assert len(timeline.issued_at) == 4
        assert timeline.gaps_below(_MIN_REQUEST_INTERVAL_SECONDS) == []


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
    """Retry behaviour of the `releases/dates` request itself.

    Enrichment is disabled or the release list is empty, so the recorded sleeps
    are purely retry backoff.
    """

    def test_transient_failure_twice_then_success_returns_events(self):
        http_get = _CountingFailThenSucceed(
            httpx.ConnectError("boom"),
            fail_times=2,
            payload=_release_dates_payload(),
        )
        sleeps: list[float] = []
        client = _client(http_get, sleep_fn=sleeps.append, max_enriched_releases=0)

        items = client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert len(items) == 1
        assert http_get.calls == 3
        assert sleeps == [1.0, 2.0]

    def test_persistent_transient_failure_exhausts_retries_and_propagates(self):
        http_get = _AlwaysFail(httpx.ConnectError("boom"))
        sleeps: list[float] = []
        client = _client(http_get, sleep_fn=sleeps.append)

        with pytest.raises(httpx.ConnectError):
            client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert http_get.calls == 3
        assert sleeps == [1.0, 2.0]

    def test_client_error_status_propagates_without_retry(self):
        http_get = _AlwaysFail(_make_status_error("Unauthorized", 401))
        sleeps: list[float] = []
        client = _client(http_get, sleep_fn=sleeps.append)

        with pytest.raises(httpx.HTTPStatusError):
            client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert http_get.calls == 1
        assert sleeps == []

    def test_server_error_status_is_retried(self):
        http_get = _CountingFailThenSucceed(
            _make_status_error("Server Error", 503),
            fail_times=2,
            payload={"release_dates": []},
        )
        sleeps: list[float] = []
        client = _client(http_get, sleep_fn=sleeps.append)

        items = client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert items == []
        assert http_get.calls == 3
        assert sleeps == [1.0, 2.0]

    def test_request_timeout_status_is_retried(self):
        http_get = _CountingFailThenSucceed(
            _make_status_error("Request Timeout", 408),
            fail_times=1,
            payload={"release_dates": []},
        )
        sleeps: list[float] = []
        client = _client(http_get, sleep_fn=sleeps.append)

        assert client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF) == []
        assert http_get.calls == 2
        assert sleeps == [1.0]

    def test_malformed_release_dates_row_propagates_without_retry(self):
        def malformed_response(*_args, **_kwargs):
            return {"release_dates": [{"release_id": 50}]}  # missing "date"

        sleeps: list[float] = []
        client = _client(malformed_response, sleep_fn=sleeps.append)

        with pytest.raises(KeyError):
            client.fetch_calendar_events(AS_OF, RANGE_END, as_of=AS_OF)

        assert sleeps == []
