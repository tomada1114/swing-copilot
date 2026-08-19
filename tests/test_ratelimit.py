"""Rate-limit budget shared by the clients on one account (Issue #263)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from swing_copilot.data.earnings_finnhub import EarningsTiming, FinnhubEarningsClient
from swing_copilot.ratelimit import (
    FINNHUB_MIN_REQUEST_INTERVAL_SECONDS,
    MinIntervalThrottle,
)
from swing_copilot.text.news_finnhub import FinnhubNewsClient
from tests.conftest import ThrottleTimeline

_AS_OF = date(2027, 1, 10)
_REQUEST_SECONDS = 0.3


class FakeClock:
    """Calendar/audit clock; the rate limit runs on `ThrottleTimeline`."""

    def today(self) -> date:
        return _AS_OF

    def now(self) -> datetime:
        return datetime(2027, 1, 10, 12, tzinfo=UTC)


def _news_payload(*_args, **_kwargs):
    return [
        {
            "id": 123,
            "datetime": int(datetime(2027, 1, 5, tzinfo=UTC).timestamp()),
            "headline": "Apple announces new product",
            "url": "https://example.com/article",
            "summary": "Apple announced a new product today.",
        }
    ]


def _earnings_payload(*_args, **_kwargs):
    return {
        "earningsCalendar": [{"symbol": "AAPL", "date": "2027-01-20", "hour": "amc"}]
    }


def _finnhub_pair(
    timeline: ThrottleTimeline, *, throttle: MinIntervalThrottle | None = None
) -> tuple[FinnhubNewsClient, FinnhubEarningsClient]:
    """Build both Finnhub clients over one timeline, optionally sharing a budget."""

    def news_get(_url, _params):
        timeline.issue_request()
        return _news_payload()

    def earnings_get(_url, _params):
        timeline.issue_request()
        return _earnings_payload()

    news_client = FinnhubNewsClient(
        "test-key",
        http_get=news_get,
        date_clock=FakeClock(),
        rate_clock=timeline.clock,
        sleep_fn=timeline.sleep,
        throttle=throttle,
    )
    earnings_client = FinnhubEarningsClient(
        "test-key",
        http_get=earnings_get,
        clock=FakeClock(),
        timing=EarningsTiming(
            rate_clock=timeline.clock,
            sleep_fn=timeline.sleep,
            backoff_fn=timeline.sleep,
        ),
        throttle=throttle,
    )
    return news_client, earnings_client


def _alternate(
    news_client: FinnhubNewsClient,
    earnings_client: FinnhubEarningsClient,
    rounds: int,
) -> None:
    """Call news then earnings `rounds` times, as one run's fetch steps would."""
    for _ in range(rounds):
        news_client.fetch_company_news("AAPL", date(2027, 1, 1), as_of=_AS_OF)
        earnings_client.fetch_next_earnings("AAPL", _AS_OF, date(2027, 2, 10))


class TestMinIntervalThrottle:
    def test_first_request_is_issued_without_waiting(self):
        sleeps: list[float] = []
        throttle = MinIntervalThrottle(1.0, clock=lambda: 0.0, sleep_fn=sleeps.append)

        throttle.before_request()

        assert sleeps == []

    def test_waits_only_the_remainder_of_the_interval(self):
        sleeps: list[float] = []
        times = iter([0.0, 0.3])
        throttle = MinIntervalThrottle(
            1.0, clock=lambda: next(times), sleep_fn=sleeps.append
        )

        throttle.before_request()
        throttle.before_request()

        assert sleeps == [pytest.approx(0.7)]

    def test_request_already_past_the_interval_is_not_delayed(self):
        sleeps: list[float] = []
        times = iter([0.0, 5.0])
        throttle = MinIntervalThrottle(
            1.0, clock=lambda: next(times), sleep_fn=sleeps.append
        )

        throttle.before_request()
        throttle.before_request()

        assert sleeps == []


class TestSharedFinnhubThrottle:
    def test_shared_budget_keeps_alternating_clients_one_interval_apart(self):
        """Issue #263: Finnhub's 60/minute cap is per account, not per client.

        The news and earnings clients hold the same API key, so their combined
        issue rate is what the provider meters. Asserted on issue instants
        across both clients, not on either client's own sleeps.
        """
        timeline = ThrottleTimeline(request_seconds=_REQUEST_SECONDS)
        throttle = MinIntervalThrottle(
            FINNHUB_MIN_REQUEST_INTERVAL_SECONDS,
            clock=timeline.clock,
            sleep_fn=timeline.sleep,
        )
        news_client, earnings_client = _finnhub_pair(timeline, throttle=throttle)

        _alternate(news_client, earnings_client, rounds=3)

        assert len(timeline.issued_at) == 6
        assert timeline.gaps_below(FINNHUB_MIN_REQUEST_INTERVAL_SECONDS) == []
        assert (
            timeline.issue_gaps
            == [pytest.approx(FINNHUB_MIN_REQUEST_INTERVAL_SECONDS)] * 5
        )

    def test_shared_budget_survives_a_retry_inside_one_client(self):
        """A retried attempt spends the shared budget too.

        `retry_external_call` runs `before_attempt` per attempt, so the failed
        attempt is a real request against the account; the other client must
        still be held one interval behind it.
        """
        timeline = ThrottleTimeline(request_seconds=_REQUEST_SECONDS)
        throttle = MinIntervalThrottle(
            FINNHUB_MIN_REQUEST_INTERVAL_SECONDS,
            clock=timeline.clock,
            sleep_fn=timeline.sleep,
        )
        attempts = 0

        def rate_limited_then_succeeds(_url, _params):
            nonlocal attempts
            attempts += 1
            timeline.issue_request()
            if attempts == 1:
                message = "rate limited"
                raise TimeoutError(message)
            return _news_payload()

        news_client = FinnhubNewsClient(
            "test-key",
            http_get=rate_limited_then_succeeds,
            date_clock=FakeClock(),
            sleep_fn=timeline.sleep,
            throttle=throttle,
        )
        _, earnings_client = _finnhub_pair(timeline, throttle=throttle)

        _alternate(news_client, earnings_client, rounds=2)

        # Five issued requests: the failure, its retry, one more news fetch,
        # and one earnings fetch per round.
        assert len(timeline.issued_at) == 5
        assert timeline.gaps_below(FINNHUB_MIN_REQUEST_INTERVAL_SECONDS) == []

    def test_without_a_shared_budget_each_client_still_throttles_only_itself(self):
        """The default stays per-instance, so existing callers are unaffected.

        This is the behavior Issue #263 leaves in place for anyone who does not
        inject a throttle -- and the reason the composition root has to inject
        one: each client alone honors the interval, while the pair's combined
        issue rate reaches roughly twice the account cap.
        """
        timeline = ThrottleTimeline(request_seconds=_REQUEST_SECONDS)
        news_client, earnings_client = _finnhub_pair(timeline)

        _alternate(news_client, earnings_client, rounds=3)

        # Calls alternate news, earnings, news, ... so the even-indexed issues
        # are the news client's and the odd-indexed ones the earnings client's.
        assert timeline.issued_at[0::2] == [pytest.approx(t) for t in (0.0, 1.0, 2.0)]
        assert timeline.issued_at[1::2] == [pytest.approx(t) for t in (0.3, 1.3, 2.3)]
        assert timeline.gaps_below(FINNHUB_MIN_REQUEST_INTERVAL_SECONDS) == [
            pytest.approx(gap) for gap in (0.3, 0.7, 0.3, 0.7, 0.3)
        ]
