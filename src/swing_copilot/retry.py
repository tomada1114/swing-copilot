"""Small, explicit retry primitives for failure-prone external boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

RETRY_DELAYS_SECONDS = (1.0, 2.0)
_SERVER_ERROR_STATUS_MIN = 500
#: The default caught-exception set for `retry_external_call`. Public (no
#: leading underscore) so an adapter whose vendor library raises its own
#: non-httpx failure type can extend it via `retryable_types=(*EXTERNAL_FAILURES,
#: SomeVendorError)` instead of this module importing that vendor library.
EXTERNAL_FAILURES = (
    ConnectionError,
    TimeoutError,
    httpx.TransportError,
    httpx.HTTPStatusError,
)


def is_retryable_external_error(error: Exception) -> bool:
    """Return whether an external transport failure is safe to retry."""
    if isinstance(error, (ConnectionError, TimeoutError, httpx.TransportError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        return status_code in (408, 429) or status_code >= _SERVER_ERROR_STATUS_MIN
    return False


def retry_external_call[T](
    operation: Callable[[], T],
    *,
    before_attempt: Callable[[], None],
    sleep_fn: Callable[[float], None],
    is_retryable: Callable[[Exception], bool] = is_retryable_external_error,
    retryable_types: tuple[type[Exception], ...] = EXTERNAL_FAILURES,
) -> T:
    """Run an operation at most three times with deterministic backoff.

    Only the supplied retry predicate may turn an exception into another
    attempt; every non-retryable error is re-raised unchanged.

    Args:
        operation: The zero-argument call to attempt.
        before_attempt: Invoked before every attempt, including the first
            (an adapter's rate-limit throttle).
        sleep_fn: Invoked with each backoff delay between attempts.
        is_retryable: Predicate deciding whether a caught exception earns
            another attempt. Never consulted for an exception type outside
            `retryable_types` -- that tuple is checked first.
        retryable_types: The exception types this call will even attempt to
            catch, defaulting to the shared external-failure set. An adapter
            whose underlying library raises its own non-httpx failure type
            (e.g. edgartools' `TooManyRequestsError`) extends this tuple
            rather than this module importing that vendor library.
    """
    for delay in RETRY_DELAYS_SECONDS:
        before_attempt()
        try:
            return operation()
        except retryable_types as exc:
            if not is_retryable(exc):
                raise
            sleep_fn(delay)
    before_attempt()
    return operation()
