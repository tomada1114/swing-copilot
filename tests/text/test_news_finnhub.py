"""Tests for FinnhubNewsClient (FR-07)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from swing_copilot.text.news_finnhub import (
    _MIN_REQUEST_INTERVAL_SECONDS,
    FinnhubNewsClient,
    _real_http_get,
)
from tests.conftest import ThrottleTimeline


class FakeClock:
    def __init__(self, times):
        self._times = list(times)

    def __call__(self):
        return self._times.pop(0)


class FakeDateClock:
    def __init__(self, today):
        self._today = today

    def today(self):
        return self._today

    def now(self):
        return datetime.combine(self._today, datetime.min.time(), tzinfo=UTC)


def _fake_response(*_args, **_kwargs):
    return [
        {
            "id": 123,
            "datetime": int(datetime(2027, 1, 5, tzinfo=UTC).timestamp()),
            "headline": "Apple announces new product",
            "url": "https://example.com/article",
            "summary": "Apple announced a new product today.",
        }
    ]


class TestFetchCompanyNews:
    def test_normalizes_to_text_item_schema(self):
        client = FinnhubNewsClient(
            "test-key",
            http_get=_fake_response,
            date_clock=FakeDateClock(date(2027, 1, 10)),
        )

        items = client.fetch_company_news(
            "AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10)
        )

        assert len(items) == 1
        item = items[0]
        assert item.source_id == "finnhub:123"
        assert item.symbol == "AAPL"
        assert item.source_type == "news"
        assert item.title == "Apple announces new product"
        assert item.source_url == "https://example.com/article"
        assert item.content_text == "Apple announced a new product today."

    def test_passes_since_and_until_as_query_params(self):
        captured = {}

        def capturing_get(url, params):
            captured["url"] = url
            captured["params"] = params
            return []

        client = FinnhubNewsClient(
            "test-key",
            http_get=capturing_get,
            date_clock=FakeDateClock(date(2027, 1, 10)),
        )
        client.fetch_company_news("AAPL", date(2027, 1, 1), as_of=date(2027, 1, 8))

        assert captured["params"]["symbol"] == "AAPL"
        assert captured["params"]["from"] == "2027-01-01"
        assert captured["params"]["to"] == "2027-01-08"
        assert captured["params"]["token"] == "test-key"  # noqa: S105 - test fixture, not a real credential

    def test_keeps_related_tickers_and_category_from_the_response(self):
        def responding_get(*_args, **_kwargs):
            return [
                {
                    "id": 123,
                    "datetime": int(datetime(2027, 1, 5, tzinfo=UTC).timestamp()),
                    "headline": "Apple and Microsoft extend their deal",
                    "url": "https://example.com/article",
                    "summary": "Both companies confirmed the extension.",
                    "related": "AAPL,MSFT",
                    "category": "company",
                }
            ]

        client = FinnhubNewsClient(
            "test-key",
            http_get=responding_get,
            date_clock=FakeDateClock(date(2027, 1, 10)),
        )

        item = client.fetch_company_news(
            "AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10)
        )[0]

        assert item.related_symbols == ("AAPL", "MSFT")
        assert item.category == "company"

    @pytest.mark.parametrize(
        ("related", "expected"),
        [
            pytest.param(" aapl , msft ", ("AAPL", "MSFT"), id="normalized"),
            pytest.param("AAPL,,AAPL,", ("AAPL",), id="blank-and-duplicate-segments"),
            pytest.param("", (), id="empty-string"),
            pytest.param(None, (), id="absent"),
            pytest.param(["AAPL"], (), id="unexpected-type"),
        ],
    )
    def test_related_tickers_are_normalized_and_missing_ones_stay_empty(
        self, related, expected
    ):
        def responding_get(*_args, **_kwargs):
            item = {
                "id": 123,
                "datetime": int(datetime(2027, 1, 5, tzinfo=UTC).timestamp()),
                "headline": "Headline",
                "url": "https://example.com/article",
                "summary": "Summary.",
            }
            if related is not None:
                item["related"] = related
            return [item]

        client = FinnhubNewsClient(
            "test-key",
            http_get=responding_get,
            date_clock=FakeDateClock(date(2027, 1, 10)),
        )

        item = client.fetch_company_news(
            "AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10)
        )[0]

        assert item.related_symbols == expected

    @pytest.mark.parametrize(
        ("category", "expected"),
        [
            pytest.param(" company ", "company", id="trimmed"),
            pytest.param("   ", None, id="blank"),
            pytest.param(None, None, id="absent"),
            pytest.param(7, None, id="unexpected-type"),
        ],
    )
    def test_a_blank_or_absent_category_becomes_none(self, category, expected):
        def responding_get(*_args, **_kwargs):
            item = {
                "id": 123,
                "datetime": int(datetime(2027, 1, 5, tzinfo=UTC).timestamp()),
                "headline": "Headline",
                "url": "https://example.com/article",
                "summary": "Summary.",
            }
            if category is not None:
                item["category"] = category
            return [item]

        client = FinnhubNewsClient(
            "test-key",
            http_get=responding_get,
            date_clock=FakeDateClock(date(2027, 1, 10)),
        )

        item = client.fetch_company_news(
            "AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10)
        )[0]

        assert item.category == expected

    def test_a_response_without_relevance_metadata_stays_empty(self):
        client = FinnhubNewsClient(
            "test-key",
            http_get=_fake_response,
            date_clock=FakeDateClock(date(2027, 1, 10)),
        )

        item = client.fetch_company_news(
            "AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10)
        )[0]

        assert item.related_symbols == ()
        assert item.category is None

    def test_empty_response_returns_empty_list(self):
        client = FinnhubNewsClient(
            "test-key",
            http_get=lambda *_a, **_k: [],
            date_clock=FakeDateClock(date(2027, 1, 10)),
        )
        assert (
            client.fetch_company_news("AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10))
            == []
        )


class TestRateLimiting:
    def test_throttles_to_at_least_one_second_between_calls(self):
        sleeps: list[float] = []
        client = FinnhubNewsClient(
            "test-key",
            http_get=_fake_response,
            date_clock=FakeDateClock(date(2027, 1, 10)),
            rate_clock=FakeClock([0.0, 0.3]),
            sleep_fn=sleeps.append,
        )

        client.fetch_company_news("AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10))
        client.fetch_company_news("MSFT", date(2027, 1, 1), as_of=date(2027, 1, 10))

        # Interval 1.05s (Issue #283) minus the 0.3s already elapsed.
        assert sleeps == [pytest.approx(0.75)]

    def test_no_throttle_when_calls_are_already_spaced_out(self):
        sleeps: list[float] = []
        client = FinnhubNewsClient(
            "test-key",
            http_get=_fake_response,
            date_clock=FakeDateClock(date(2027, 1, 10)),
            rate_clock=FakeClock([0.0, 5.0]),
            sleep_fn=sleeps.append,
        )

        client.fetch_company_news("AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10))
        client.fetch_company_news("MSFT", date(2027, 1, 1), as_of=date(2027, 1, 10))

        assert sleeps == []

    def test_successive_requests_are_issued_at_least_one_interval_apart(self):
        """Issue #253: the throttle must count from the request it let through.

        Recording the pre-sleep clock reading dropped the slept interval, so
        every other request went out early and the effective rate exceeded the
        60/minute cap. Asserted on issue instants, not on sleep arguments.
        """
        timeline = ThrottleTimeline(request_seconds=0.3)

        def timed_get(_url, _params):
            timeline.issue_request()
            return _fake_response()

        client = FinnhubNewsClient(
            "test-key",
            http_get=timed_get,
            date_clock=FakeDateClock(date(2027, 1, 10)),
            rate_clock=timeline.clock,
            sleep_fn=timeline.sleep,
        )

        for symbol in ("AAPL", "MSFT", "NVDA"):
            client.fetch_company_news(symbol, date(2027, 1, 1), as_of=date(2027, 1, 10))

        assert timeline.issue_gaps == [pytest.approx(_MIN_REQUEST_INTERVAL_SECONDS)] * 2


class TestRetries:
    def test_retries_rate_limited_request_and_throttles_every_attempt(self):
        calls = 0
        sleeps: list[float] = []

        def rate_limited_then_succeeds(_url, _params):
            nonlocal calls
            calls += 1
            if calls == 1:
                request = httpx.Request("GET", "https://example.com")
                response = httpx.Response(429, request=request)
                msg = "rate limited"
                raise httpx.HTTPStatusError(msg, request=request, response=response)
            return _fake_response()

        client = FinnhubNewsClient(
            "test-key",
            http_get=rate_limited_then_succeeds,
            date_clock=FakeDateClock(date(2027, 1, 10)),
            rate_clock=FakeClock([0.0, 1.0]),
            sleep_fn=sleeps.append,
        )

        result = client.fetch_company_news(
            "AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10)
        )

        assert len(result) == 1
        assert calls == 2
        # RETRY_DELAYS_SECONDS[0] backoff, then the throttle's own wait for
        # the retried attempt (interval minus the 1.0s the fake clock already
        # advanced between the two rate_clock reads).
        assert sleeps == [
            pytest.approx(1.0),
            pytest.approx(_MIN_REQUEST_INTERVAL_SECONDS - 1.0),
        ]

    def test_retried_attempts_keep_the_minimum_issue_interval(self):
        """Issue #253: a retry attempt is a request and resets the same clock.

        `before_attempt` fires once per attempt, so the failed attempt counts
        against the rate limit too; every subsequent request must still be at
        least one interval behind the one actually issued before it.
        """
        timeline = ThrottleTimeline(request_seconds=0.3)
        calls = 0

        def rate_limited_then_succeeds(_url, _params):
            nonlocal calls
            calls += 1
            timeline.issue_request()
            if calls == 1:
                request = httpx.Request("GET", "https://example.com")
                response = httpx.Response(429, request=request)
                msg = "rate limited"
                raise httpx.HTTPStatusError(msg, request=request, response=response)
            return _fake_response()

        client = FinnhubNewsClient(
            "test-key",
            http_get=rate_limited_then_succeeds,
            date_clock=FakeDateClock(date(2027, 1, 10)),
            rate_clock=timeline.clock,
            sleep_fn=timeline.sleep,
        )

        for symbol in ("AAPL", "MSFT", "NVDA"):
            client.fetch_company_news(symbol, date(2027, 1, 1), as_of=date(2027, 1, 10))

        # Four issued requests: the 429, its retry, and one per later symbol.
        assert calls == 4
        assert timeline.gaps_below(_MIN_REQUEST_INTERVAL_SECONDS) == []

    def test_does_not_retry_non_transient_http_error(self):
        calls = 0
        sleeps: list[float] = []

        def unauthorized(_url, _params):
            nonlocal calls
            calls += 1
            request = httpx.Request("GET", "https://example.com")
            response = httpx.Response(401, request=request)
            msg = "unauthorized"
            raise httpx.HTTPStatusError(msg, request=request, response=response)

        client = FinnhubNewsClient(
            "test-key",
            http_get=unauthorized,
            date_clock=FakeDateClock(date(2027, 1, 10)),
            sleep_fn=sleeps.append,
        )

        with pytest.raises(httpx.HTTPStatusError, match="unauthorized"):
            client.fetch_company_news("AAPL", date(2027, 1, 1), as_of=date(2027, 1, 10))
        assert calls == 1
        assert sleeps == []


class TestRealHttpGet:
    def test_parses_json_response_from_httpx(self, monkeypatch):

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"id": 1}]

        monkeypatch.setattr(
            "swing_copilot.text.news_finnhub.httpx.get",
            lambda url, params, timeout: FakeResponse(),  # noqa: ARG005 - matching httpx.get's call shape
        )

        result = _real_http_get("https://example.com", {"a": "b"})

        assert result == [{"id": 1}]
