"""Finnhub company-news client, throttled to <=60 calls/minute (FR-07)."""

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

FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
_MIN_REQUEST_INTERVAL_SECONDS = 1.0  # 60 calls/minute cap


class _HttpGet(Protocol):
    def __call__(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the parsed JSON array from a GET request."""
        ...  # pragma: no cover


def _real_http_get(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:

    response = httpx.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    result: list[dict[str, Any]] = response.json()
    return result


class FinnhubNewsClient:
    """Throttled Finnhub `company-news` client."""

    def __init__(
        self,
        api_key: str,
        *,
        http_get: _HttpGet = _real_http_get,
        date_clock: Clock | None = None,
        rate_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        """Create the client.

        Args:
            api_key: Finnhub API key.
            http_get: Injectable HTTP GET, used by tests to avoid real calls.
            date_clock: Injectable calendar clock for the query's end date.
            rate_clock: Injectable monotonic clock for rate-limit tests.
            sleep_fn: Injectable sleep function for rate-limit tests.
        """
        self._api_key = api_key
        self._http_get = http_get
        self._date_clock = date_clock or SystemClock()
        self._rate_clock = rate_clock or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        now = self._rate_clock()
        if self._last_request_at is not None:
            wait = _MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
        self._last_request_at = now

    def fetch_company_news(
        self, symbol: str, since: date, *, as_of: date
    ) -> list[TextItem]:
        """Fetch recent news for `symbol` published on or after `since`.

        Args:
            symbol: Ticker symbol.
            since: Earliest publication date to include.
            as_of: Latest publication date to request; never inferred from today.

        Returns:
            News items normalized to `TextItem` (`source_type="news"`).
        """
        self._throttle()
        raw_items = self._http_get(
            FINNHUB_NEWS_URL,
            {
                "symbol": symbol,
                "from": since.isoformat(),
                "to": as_of.isoformat(),
                "token": self._api_key,
            },
        )
        fetched_at = self._date_clock.now()
        return [
            TextItem(
                source_id=f"finnhub:{item['id']}",
                symbol=symbol,
                source_type="news",
                published_at=datetime.fromtimestamp(item["datetime"], tz=UTC),
                title=item.get("headline"),
                source_url=item.get("url", ""),
                content_text=item.get("summary", ""),
                fetched_at=fetched_at,
            )
            for item in raw_items
        ]
