"""Finnhub earnings-calendar adapter with bounded retry and throttling."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from swing_copilot.clock import SystemClock
from swing_copilot.data.earnings import EarningsEvent
from swing_copilot.retry import retry_external_call

if TYPE_CHECKING:
    from collections.abc import Callable

    from swing_copilot.clock import Clock

FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
_MIN_REQUEST_INTERVAL_SECONDS = 1.0


class _HttpGet(Protocol):
    def __call__(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Return a parsed Finnhub JSON object."""
        ...  # pragma: no cover


def _real_http_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = httpx.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


@dataclass(frozen=True, slots=True)
class EarningsTiming:
    """Injectable rate-limit and backoff timing functions."""

    rate_clock: Callable[[], float] = time.monotonic
    sleep_fn: Callable[[float], None] = time.sleep
    backoff_fn: Callable[[float], None] = time.sleep


class FinnhubEarningsClient:
    """Finnhub `/calendar/earnings` implementation."""

    def __init__(
        self,
        api_key: str,
        *,
        http_get: _HttpGet = _real_http_get,
        clock: Clock | None = None,
        timing: EarningsTiming | None = None,
    ) -> None:
        """Create an offline-injectable client.

        Args:
            api_key: Finnhub API key.
            http_get: Injectable JSON GET boundary.
            clock: Audit timestamp source.
            timing: Injectable rate-limit and retry timing functions.
        """
        self._api_key = api_key
        self._http_get = http_get
        self._clock = clock or SystemClock()
        resolved_timing = timing or EarningsTiming()
        self._rate_clock = resolved_timing.rate_clock
        self._sleep_fn = resolved_timing.sleep_fn
        self._backoff_fn = resolved_timing.backoff_fn
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        now = self._rate_clock()
        if self._last_request_at is not None:
            wait = _MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
        self._last_request_at = now

    def fetch_next_earnings(
        self, symbol: str, start: date, end: date
    ) -> EarningsEvent | None:
        """Fetch the earliest matching event with a three-attempt ceiling."""
        params = {
            "symbol": symbol,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "token": self._api_key,
        }
        payload = retry_external_call(
            lambda: self._http_get(FINNHUB_EARNINGS_URL, params),
            before_attempt=self._throttle,
            sleep_fn=self._backoff_fn,
        )
        calendar = payload.get("earningsCalendar")
        if not isinstance(calendar, list):
            msg = "Finnhub earningsCalendar response must be a list"
            raise TypeError(msg)
        matching: list[tuple[date, dict[str, Any]]] = []
        for item in calendar:
            if (
                not isinstance(item, dict)
                or item.get("symbol") != symbol
                or not isinstance(item.get("date"), str)
            ):
                continue
            earnings_date = date.fromisoformat(item["date"])
            if start <= earnings_date <= end:
                matching.append((earnings_date, item))
        matching.sort(key=lambda match: match[0])
        if not matching:
            return None
        earnings_date, item = matching[0]
        return EarningsEvent(
            symbol=symbol,
            earnings_date=earnings_date,
            session=str(item.get("hour") or "unknown"),
            fetched_at=self._clock.now(),
        )
