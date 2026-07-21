"""FRED economic release calendar client (FR-07).

Uses FRED's `releases/dates` endpoint — the closest official equivalent to
an "economic calendar" (upcoming/past release dates for series like the
Employment Situation) — since FRED itself indexes data series, not a
calendar product. No hard rate limit is documented (research.md), so
requests are sequential without an explicit throttle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from swing_copilot.clock import SystemClock
from swing_copilot.text.base import TextItem

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.clock import Clock

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"


class _HttpGet(Protocol):
    def __call__(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Return the parsed JSON object from a GET request."""
        ...  # pragma: no cover


def _real_http_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = httpx.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


class FredCalendarClient:
    """FRED economic release-date calendar client."""

    def __init__(
        self,
        api_key: str,
        *,
        http_get: _HttpGet = _real_http_get,
        clock: Clock | None = None,
    ) -> None:
        """Create the client.

        Args:
            api_key: FRED API key.
            http_get: Injectable HTTP GET, used by tests to avoid real calls.
            clock: Injectable wall clock for deterministic fetch timestamps.
        """
        self._api_key = api_key
        self._http_get = http_get
        self._clock = clock or SystemClock()

    def fetch_calendar_events(self, start: date, end: date) -> list[TextItem]:
        """Fetch economic release dates within `[start, end]`.

        Args:
            start: Inclusive range start.
            end: Inclusive range end.

        Returns:
            Release-date events normalized to `TextItem` (`source_type="calendar"`).
        """
        payload = self._http_get(
            FRED_RELEASE_DATES_URL,
            {
                "realtime_start": start.isoformat(),
                "realtime_end": end.isoformat(),
                "file_type": "json",
                "api_key": self._api_key,
            },
        )
        fetched_at = self._clock.now()
        return [
            TextItem(
                source_id=f"fred:{item['release_id']}:{item['date']}",
                symbol=None,
                source_type="calendar",
                published_at=datetime.fromisoformat(item["date"]).replace(tzinfo=UTC),
                title=item.get("release_name"),
                source_url=f"https://fred.stlouisfed.org/release?rid={item['release_id']}",
                content_text=item.get("release_name", ""),
                fetched_at=fetched_at,
            )
            for item in payload.get("release_dates", [])
        ]
