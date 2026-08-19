"""Minimum-interval throttling, shareable across one provider account."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

#: Finnhub caps calls per *account* (60 calls/minute), not per client. Both
#: `text/news_finnhub.py` and `data/earnings_finnhub.py` speak to that one
#: account with the same API key, so the number lives here once rather than as
#: two constants that only happen to be equal (Issue #263).
FINNHUB_MIN_REQUEST_INTERVAL_SECONDS = 1.0


class MinIntervalThrottle:
    """Hold requests to at most one per `min_interval_seconds`.

    One instance is one rate-limit budget. Sharing an instance between the
    clients that talk to the same account bounds their *combined* issue rate;
    an instance per client (what every adapter builds when none is injected)
    bounds only that client, which is exactly how two clients on one Finnhub
    key could together exceed a per-account cap (Issue #263).

    Not thread-safe: the pipeline runs single-threaded, and a shared throttle
    is what makes the account-wide rate observable in the first place. Adding
    concurrency to the fetch steps means adding a lock here.
    """

    __slots__ = ("_clock", "_last_request_at", "_min_interval_seconds", "_sleep_fn")

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a throttle over one rate-limited budget.

        Args:
            min_interval_seconds: Minimum spacing between issued requests.
            clock: Monotonic clock reading; injectable for offline tests.
            sleep_fn: Blocking sleep; injectable for offline tests.
        """
        self._min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._sleep_fn = sleep_fn
        self._last_request_at: float | None = None

    def before_request(self) -> None:
        """Wait out the remainder of the interval, then stamp the issue instant.

        Records when the request is actually issued (after any wait), not when
        the throttle decision started. Recording the pre-sleep reading drops
        the slept interval from the next gap calculation and lets the effective
        request rate exceed 1/`min_interval_seconds` (Issue #253).
        """
        now = self._clock()
        issued_at = now
        if self._last_request_at is not None:
            wait = self._min_interval_seconds - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
                issued_at = now + wait
        self._last_request_at = issued_at
