"""FRED economic release calendar client (FR-07).

Uses FRED's `releases/dates` endpoint — the closest official equivalent to
an "economic calendar" (upcoming/past release dates for series like the
Employment Situation) — since FRED itself indexes data series, not a
calendar product. No hard rate limit is documented (research.md), so
requests are sequential without an explicit throttle.

The fetch is wrapped in a bounded retry (mirroring
`swing_copilot.data.edgar.EdgarClient`) so a single transient FRED failure
(timeout, connection error, 5xx, or 429) does not fail the whole run. Other
4xx errors (auth/validation) and response-parsing failures are not
transient and propagate immediately.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from swing_copilot.clock import SystemClock
from swing_copilot.text.base import TextItem

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from swing_copilot.clock import Clock

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"
_RETRY_DELAYS_SECONDS = (1.0, 2.0)  # 3 total attempts
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_THRESHOLD = 500


class _HttpGet(Protocol):
    def __call__(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Return the parsed JSON object from a GET request."""
        ...  # pragma: no cover


def _real_http_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = httpx.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


def _is_transient_http_error(error: Exception) -> bool:
    """Return whether `error` is a retryable FRED HTTP failure.

    Transport-level `httpx.HTTPError`s (timeout, connection failure) and HTTP
    5xx/429 status errors are transient. Other 4xx status errors (auth,
    validation) and non-HTTP errors (for example response-parsing failures)
    are not retried.
    """
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        return (
            status_code == _HTTP_TOO_MANY_REQUESTS
            or status_code >= _HTTP_SERVER_ERROR_THRESHOLD
        )
    return isinstance(error, httpx.HTTPError)


class FredCalendarClient:
    """FRED economic release-date calendar client."""

    def __init__(
        self,
        api_key: str,
        *,
        http_get: _HttpGet = _real_http_get,
        clock: Clock | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        """Create the client.

        Args:
            api_key: FRED API key.
            http_get: Injectable HTTP GET, used by tests to avoid real calls.
            clock: Injectable wall clock for deterministic fetch timestamps.
            sleep_fn: Injectable sleep function for deterministic retry-backoff
                tests.
        """
        self._api_key = api_key
        self._http_get = http_get
        self._clock = clock or SystemClock()
        self._sleep_fn = sleep_fn or time.sleep

    def _fetch_with_retries(
        self, operation: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """Run one FRED HTTP GET with bounded retry on transient failures."""
        for delay in _RETRY_DELAYS_SECONDS:
            try:
                return operation()
            except Exception as error:
                if not _is_transient_http_error(error):
                    raise
                self._sleep_fn(delay)
        return operation()

    def fetch_calendar_events(self, start: date, end: date) -> list[TextItem]:
        """Fetch economic release dates within `[start, end]`.

        Args:
            start: Inclusive range start.
            end: Inclusive range end.

        Returns:
            Release-date events normalized to `TextItem` (`source_type="calendar"`).
        """
        payload = self._fetch_with_retries(
            lambda: self._http_get(
                FRED_RELEASE_DATES_URL,
                {
                    "realtime_start": start.isoformat(),
                    "realtime_end": end.isoformat(),
                    "file_type": "json",
                    "api_key": self._api_key,
                },
            )
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
