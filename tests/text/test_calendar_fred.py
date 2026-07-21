"""Tests for FredCalendarClient (FR-07)."""

from __future__ import annotations

from datetime import UTC, date, datetime

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
