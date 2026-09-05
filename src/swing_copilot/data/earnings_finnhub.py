"""Finnhub earnings-calendar adapter with bounded retry and throttling."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from swing_copilot.clock import SystemClock
from swing_copilot.data.earnings import EarningsEvent
from swing_copilot.ratelimit import (
    FINNHUB_MIN_REQUEST_INTERVAL_SECONDS,
    MinIntervalThrottle,
)
from swing_copilot.retry import retry_external_call

if TYPE_CHECKING:
    from collections.abc import Callable

    from swing_copilot.clock import Clock

FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
# 60 calls/minute cap, shared with `text/news_finnhub.py` per account.
_MIN_REQUEST_INTERVAL_SECONDS = FINNHUB_MIN_REQUEST_INTERVAL_SECONDS


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

    def throttle_fields_set(self) -> tuple[str, ...]:
        """Name the throttle-owned fields this instance overrides.

        `rate_clock` and `sleep_fn` configure the client's own throttle, while
        `backoff_fn` drives retry backoff regardless. An injected throttle owns
        the first two, so the client uses this to reject a combination whose
        halves would silently contradict each other.
        """
        return tuple(
            name
            for name, injected, default in (
                ("rate_clock", self.rate_clock, time.monotonic),
                ("sleep_fn", self.sleep_fn, time.sleep),
            )
            if injected is not default
        )


class FinnhubEarningsClient:
    """Finnhub `/calendar/earnings` implementation."""

    def __init__(
        self,
        api_key: str,
        *,
        http_get: _HttpGet = _real_http_get,
        clock: Clock | None = None,
        timing: EarningsTiming | None = None,
        throttle: MinIntervalThrottle | None = None,
    ) -> None:
        """Create an offline-injectable client.

        Args:
            api_key: Finnhub API key.
            http_get: Injectable JSON GET boundary.
            clock: Audit timestamp source.
            timing: Injectable rate-limit and retry timing functions. Its
                `rate_clock`/`sleep_fn` configure this client's own throttle
                and are mutually exclusive with `throttle`; `backoff_fn` drives
                retry backoff either way and stays free to inject.
            throttle: Rate-limit budget to count this client's requests
                against. Defaults to one private to this instance; pass the
                same instance to every client on one Finnhub account to bound
                their combined rate (Issue #263).

        Raises:
            ValueError: `throttle` was supplied together with a `timing` that
                overrides `rate_clock` or `sleep_fn`. A shared budget can only
                be measured on one clock, so silently dropping the caller's
                would leave them believing a timeline that never runs.
        """
        dead_timing_fields = (
            timing.throttle_fields_set()
            if timing is not None and throttle is not None
            else ()
        )
        if dead_timing_fields:
            msg = (
                f"EarningsTiming.{'/'.join(dead_timing_fields)} and throttle are "
                "mutually exclusive: an injected throttle measures its whole "
                "budget on its own clock (backoff_fn stays injectable)"
            )
            raise ValueError(msg)
        self._api_key = api_key
        self._http_get = http_get
        self._clock = clock or SystemClock()
        resolved_timing = timing or EarningsTiming()
        self._backoff_fn = resolved_timing.backoff_fn
        self._throttle = throttle or MinIntervalThrottle(
            _MIN_REQUEST_INTERVAL_SECONDS,
            clock=resolved_timing.rate_clock,
            sleep_fn=resolved_timing.sleep_fn,
        )

    def fetch_next_earnings(
        self, symbol: str, start: date, end: date
    ) -> EarningsEvent | None:
        """Fetch the earliest matching event with a three-attempt ceiling.

        Raises:
            TypeError: If Finnhub's `earningsCalendar` field is not a list.
            ValueError: If a matching calendar row contains a malformed ISO
                date string.
        """
        params = {
            "symbol": symbol,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "token": self._api_key,
        }
        payload = retry_external_call(
            lambda: self._http_get(FINNHUB_EARNINGS_URL, params),
            before_attempt=self._throttle.before_request,
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
