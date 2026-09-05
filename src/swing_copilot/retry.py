"""Small, explicit retry primitives for failure-prone external boundaries."""

from __future__ import annotations

from dataclasses import dataclass
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


def _default_delay_for(_error: Exception, default: float) -> float:
    return default


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """The three exception-handling knobs an adapter can override together.

    Grouped into one value so `retry_external_call` does not grow a fifth
    keyword-only argument per adapter override (`writing-python`'s
    parameter-count trigger); every field defaults to the shared, generic
    behavior, so a caller that passes no `policy` at all is unaffected.

    Attributes:
        is_retryable: Predicate deciding whether a caught exception earns
            another attempt. Never consulted for an exception type outside
            `retryable_types` -- that tuple is checked first.
        retryable_types: The exception types `retry_external_call` will even
            attempt to catch, defaulting to the shared external-failure set.
            An adapter whose underlying library raises its own non-httpx
            failure type (e.g. edgartools' `TooManyRequestsError`) extends
            this tuple rather than this module importing that vendor
            library.
        delay_for: Computes the actual wait from the caught exception and
            this attempt's `RETRY_DELAYS_SECONDS` entry (the `default`).
            Defaults to returning `default` unchanged, so every existing
            caller's backoff is bit-for-bit unchanged. An adapter whose
            retryable error itself dictates a different wait (e.g. SEC's
            429 `Retry-After` header) overrides this instead of this module
            special-casing that vendor's error shape.
    """

    is_retryable: Callable[[Exception], bool] = is_retryable_external_error
    retryable_types: tuple[type[Exception], ...] = EXTERNAL_FAILURES
    delay_for: Callable[[Exception, float], float] = _default_delay_for


_DEFAULT_RETRY_POLICY = RetryPolicy()


def retry_external_call[T](
    operation: Callable[[], T],
    *,
    before_attempt: Callable[[], None],
    sleep_fn: Callable[[float], None],
    policy: RetryPolicy = _DEFAULT_RETRY_POLICY,
) -> T:
    """Run an operation at most three times with deterministic backoff.

    Only the supplied retry predicate may turn an exception into another
    attempt; every non-retryable error is re-raised unchanged.

    Args:
        operation: The zero-argument call to attempt.
        before_attempt: Invoked before every attempt, including the first
            (an adapter's rate-limit throttle).
        sleep_fn: Invoked with each backoff delay between attempts.
        policy: Overrides which exceptions are caught, which of those are
            retryable, and how long to wait before the next attempt. See
            `RetryPolicy`. Defaults to the shared external-failure set,
            treated fully retryably, with the generic 1.0s/2.0s backoff.
    """
    for delay in RETRY_DELAYS_SECONDS:
        before_attempt()
        try:
            return operation()
        except policy.retryable_types as exc:
            if not policy.is_retryable(exc):
                raise
            sleep_fn(policy.delay_for(exc, delay))
    before_attempt()
    return operation()
