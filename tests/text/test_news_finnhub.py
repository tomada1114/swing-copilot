"""Tests for FinnhubNewsClient (FR-07)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from swing_copilot.text.news_finnhub import FinnhubNewsClient, _real_http_get


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

        items = client.fetch_company_news("AAPL", date(2027, 1, 1))

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
        client.fetch_company_news("AAPL", date(2027, 1, 1))

        assert captured["params"]["symbol"] == "AAPL"
        assert captured["params"]["from"] == "2027-01-01"
        assert captured["params"]["to"] == "2027-01-10"
        assert captured["params"]["token"] == "test-key"  # noqa: S105 - test fixture, not a real credential

    def test_empty_response_returns_empty_list(self):
        client = FinnhubNewsClient(
            "test-key",
            http_get=lambda *_a, **_k: [],
            date_clock=FakeDateClock(date(2027, 1, 10)),
        )
        assert client.fetch_company_news("AAPL", date(2027, 1, 1)) == []


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

        client.fetch_company_news("AAPL", date(2027, 1, 1))
        client.fetch_company_news("MSFT", date(2027, 1, 1))

        assert sleeps == [0.7]

    def test_no_throttle_when_calls_are_already_spaced_out(self):
        sleeps: list[float] = []
        client = FinnhubNewsClient(
            "test-key",
            http_get=_fake_response,
            date_clock=FakeDateClock(date(2027, 1, 10)),
            rate_clock=FakeClock([0.0, 5.0]),
            sleep_fn=sleeps.append,
        )

        client.fetch_company_news("AAPL", date(2027, 1, 1))
        client.fetch_company_news("MSFT", date(2027, 1, 1))

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
