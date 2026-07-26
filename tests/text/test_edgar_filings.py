"""Tests for the EDGAR filing-text collection wrapper (FR-07, P6-26)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from swing_copilot.text.base import TextItem
from swing_copilot.text.edgar_filings import (
    FilingLookbackBounds,
    fetch_recent_filings_text,
)


class FakeEdgarClient:
    def __init__(self, items: list[TextItem]) -> None:
        self._items = items
        self.calls: list[
            tuple[str, list[str], datetime, datetime | None, int | None]
        ] = []

    def fetch_filing_texts(
        self,
        symbol: str,
        form_types: list[str],
        *,
        as_of: datetime,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TextItem]:
        self.calls.append((symbol, form_types, as_of, since, limit))
        return self._items


def _item(source_id: str = "edgar:acc-1") -> TextItem:
    return TextItem(
        source_id=source_id,
        symbol="AAPL",
        source_type="filing",
        published_at=datetime(2027, 1, 1, tzinfo=UTC),
        title="8-K - AAPL",
        source_url="https://www.sec.gov/example",
        content_text="filing body",
        fetched_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


def test_delegates_to_edgar_client_fetch_filing_texts():
    items = [_item()]
    fake = FakeEdgarClient(items)

    as_of = date(2027, 1, 2)
    result = fetch_recent_filings_text(
        fake,
        "AAPL",
        ["8-K", "10-Q"],
        as_of,
        FilingLookbackBounds(lookback_days=90, limit=3),
    )

    assert result == items
    assert fake.calls == [
        (
            "AAPL",
            ["8-K", "10-Q"],
            datetime(2027, 1, 2, 23, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 10, 4, 0, 0, 0, tzinfo=UTC),  # as_of - 90 days, start of day
            3,
        )
    ]


def test_lookback_days_and_limit_are_forwarded_to_the_edgar_client():
    fake = FakeEdgarClient([])

    fetch_recent_filings_text(
        fake,
        "MSFT",
        ["8-K"],
        date(2027, 3, 1),
        FilingLookbackBounds(lookback_days=30, limit=1),
    )

    _, _, _, since, limit = fake.calls[0]
    assert since == datetime(2027, 1, 30, 0, 0, 0, tzinfo=UTC)  # 2027-03-01 - 30 days
    assert limit == 1
