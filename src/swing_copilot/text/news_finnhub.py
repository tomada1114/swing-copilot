"""Finnhub company-news client, throttled to <=60 calls/minute (FR-07)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from swing_copilot.clock import SystemClock
from swing_copilot.ratelimit import (
    FINNHUB_MIN_REQUEST_INTERVAL_SECONDS,
    MinIntervalThrottle,
)
from swing_copilot.retry import retry_external_call
from swing_copilot.text.base import TextItem

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from swing_copilot.clock import Clock

FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
# 60 calls/minute cap, shared with `data/earnings_finnhub.py` per account.
_MIN_REQUEST_INTERVAL_SECONDS = FINNHUB_MIN_REQUEST_INTERVAL_SECONDS


class _HttpGet(Protocol):
    def __call__(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the parsed JSON array from a GET request."""
        ...  # pragma: no cover


def _related_symbols(raw: Any) -> tuple[str, ...]:
    """Normalize Finnhub's `related` field into a ticker tuple.

    Finnhub returns the tickers it attached to an article as one
    comma-separated string (`"AAPL,MSFT"`), sometimes blank, sometimes with
    stray whitespace or empty segments, and occasionally absent. Anything that
    is not a string yields an empty tuple, which downstream selection reads as
    "the source did not say" rather than "unrelated".

    Returns:
        Upper-cased tickers, de-duplicated in the provider's own order.
    """
    if not isinstance(raw, str):
        return ()
    tickers: dict[str, None] = {}
    for token in raw.split(","):
        ticker = token.strip().upper()
        if ticker:
            tickers[ticker] = None
    return tuple(tickers)


def _category(raw: Any) -> str | None:
    """Normalize Finnhub's `category` label, treating blank as absent."""
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _real_http_get(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:

    response = httpx.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    result: list[dict[str, Any]] = response.json()
    return result


class FinnhubNewsClient:
    """Throttled Finnhub `company-news` client."""

    # PLR0913: the key plus five keyword-only injection seams, each an
    # independent boundary (HTTP, calendar clock, rate clock, sleep, throttle).
    # `data/earnings_finnhub.py` groups its timing trio into `EarningsTiming`;
    # doing the same here would rename this client's existing `rate_clock` /
    # `sleep_fn` seams, which Issue #263 is explicitly not touching.
    def __init__(  # noqa: PLR0913
        self,
        api_key: str,
        *,
        http_get: _HttpGet = _real_http_get,
        date_clock: Clock | None = None,
        rate_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        throttle: MinIntervalThrottle | None = None,
    ) -> None:
        """Create the client.

        Args:
            api_key: Finnhub API key.
            http_get: Injectable HTTP GET, used by tests to avoid real calls.
            date_clock: Injectable calendar clock for the query's end date.
            rate_clock: Injectable monotonic clock for rate-limit tests. Unused
                when `throttle` is injected, since a shared throttle carries
                the clock its whole budget is measured on.
            sleep_fn: Injectable sleep function for rate-limit and retry tests.
            throttle: Rate-limit budget to count this client's requests
                against. Defaults to one private to this instance; pass the
                same instance to every client on one Finnhub account to bound
                their combined rate (Issue #263).
        """
        self._api_key = api_key
        self._http_get = http_get
        self._date_clock = date_clock or SystemClock()
        self._sleep_fn = sleep_fn or time.sleep
        self._throttle = throttle or MinIntervalThrottle(
            _MIN_REQUEST_INTERVAL_SECONDS,
            clock=rate_clock or time.monotonic,
            sleep_fn=self._sleep_fn,
        )

    def fetch_company_news(
        self, symbol: str, since: date, *, as_of: date
    ) -> list[TextItem]:
        """Fetch recent news for `symbol` published on or after `since`.

        Args:
            symbol: Ticker symbol.
            since: Earliest publication date to include.
            as_of: Latest publication date to request; never inferred from today.

        Returns:
            News items normalized to `TextItem` (`source_type="news"`),
            carrying the response's `related` tickers and `category` label so
            the export step can rank an article's relevance to a candidate.
        """
        params = {
            "symbol": symbol,
            "from": since.isoformat(),
            "to": as_of.isoformat(),
            "token": self._api_key,
        }
        raw_items = retry_external_call(
            lambda: self._http_get(FINNHUB_NEWS_URL, params),
            before_attempt=self._throttle.before_request,
            sleep_fn=self._sleep_fn,
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
                related_symbols=_related_symbols(item.get("related")),
                category=_category(item.get("category")),
            )
            for item in raw_items
        ]
