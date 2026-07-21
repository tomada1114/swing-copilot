"""Tests for the EDGAR filing-text collection wrapper (FR-07)."""

from __future__ import annotations

from datetime import UTC, datetime

from swing_copilot.text.base import TextItem
from swing_copilot.text.edgar_filings import fetch_recent_filings_text


class FakeEdgarClient:
    def __init__(self, items: list[TextItem]) -> None:
        self._items = items
        self.calls: list[tuple[str, list[str]]] = []

    def fetch_filing_texts(self, symbol: str, form_types: list[str]) -> list[TextItem]:
        self.calls.append((symbol, form_types))
        return self._items


def test_delegates_to_edgar_client_fetch_filing_texts():
    items = [
        TextItem(
            source_id="edgar:acc-1",
            symbol="AAPL",
            source_type="filing",
            published_at=datetime(2027, 1, 1, tzinfo=UTC),
            title="8-K - AAPL",
            source_url="https://www.sec.gov/example",
            content_text="filing body",
            fetched_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
    ]
    fake = FakeEdgarClient(items)

    result = fetch_recent_filings_text(fake, "AAPL", ["8-K", "10-Q"])

    assert result == items
    assert fake.calls == [("AAPL", ["8-K", "10-Q"])]
