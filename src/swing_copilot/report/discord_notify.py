"""Discord webhook report notification (FR-09, NFR-07).

`Notifier` is a small Protocol so a future channel (email, Slack) plugs into
its caller by adding one new class, never touching the caller itself. Since
Issue #383, that caller is `scripts/notify_daily.py` -- a CI step that runs
once per day, after `copilot-ingest-analysis` has (or has not) landed a
verdict, not `pipeline/daily.py` itself (which never had a verdict to report
at the point its old step 7 used to run). `DiscordNotifier.notify()` never
raises — it has no `StateStore` dependency and returns whether the send
succeeded so the caller can decide how to react (`scripts/notify_daily.py`
exits non-zero; a `continue-on-error: true` workflow step keeps that from
failing the job) without stopping any batch of its own
(`docs/04_detailed_design.md` 3.18's `-> None` signature is a stale
placeholder here: without a return value the caller would have no way to
detect a failed send that raised nothing, so this module returns `bool`
instead — see the P2-4 divergence note).

Retries follow the bounded-retry convention established by
`data/edgar.py`'s `EdgarClient._with_retries` (injectable sleep function,
a fixed backoff-delay tuple, three total attempts): a transport-level
error (timeout/connect/network) or an HTTP 429/5xx response is retryable;
any other 4xx response is treated as a validation error (bad webhook URL
or payload) and returns `False` after a single attempt, per AGENTS.md's
"do not retry validation/programming errors" boundary rule. Unlike EDGAR's
throttle, no rate limiter is added here: the codebase's existing
throttles (`EdgarClient`, `text/news_finnhub.py`) are each hand-written
per adapter against that provider's own documented call-rate limit, not a
shared/reusable component, and no Discord-specific rate limit is part of
this fix's scope.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 10.0
_RETRY_DELAYS_SECONDS = (1.0, 2.0)  # 3 total attempts, matches data/edgar.py
_RATE_LIMITED_STATUS = 429


class Notifier(Protocol):
    """Abstract notification sender (NFR-07): swap channels without touching the pipeline."""

    def notify(self, summary: str, report_path: Path | None) -> bool:
        """Send `summary`, optionally mentioning `report_path`.

        Args:
            summary: Notification body text.
            report_path: Path to the generated report, or `None` to omit it.

        Returns:
            `True` if the notification was sent successfully, `False`
            otherwise. Never raises.
        """
        ...  # pragma: no cover


class _HttpPost(Protocol):
    def __call__(self, url: str, json: dict[str, object]) -> httpx.Response:
        """POST `json` to `url` and return the response."""
        ...  # pragma: no cover


def _real_http_post(url: str, json: dict[str, object]) -> httpx.Response:
    return httpx.post(url, json=json, timeout=_REQUEST_TIMEOUT_SECONDS)


def _is_retryable_status(status_code: int) -> bool:
    return (
        status_code == _RATE_LIMITED_STATUS
        or status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    )


class DiscordNotifier:
    """`Notifier` implementation sending to a Discord webhook (FR-09, opt-in)."""

    def __init__(
        self,
        webhook_url: str,
        *,
        http_post: _HttpPost = _real_http_post,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        """Create the notifier.

        Args:
            webhook_url: Discord webhook URL (from `Secrets`).
            http_post: Injectable HTTP POST, used by tests to avoid real
                network/webhook calls.
            sleep_fn: Injectable backoff sleep, used by tests to assert
                deterministic delays without real waiting.
        """
        self._webhook_url = webhook_url
        self._http_post = http_post
        self._sleep_fn = sleep_fn or time.sleep

    def notify(self, summary: str, report_path: Path | None) -> bool:
        """See `Notifier.notify`."""
        content = summary if report_path is None else f"{summary}\n{report_path}"
        payload: dict[str, object] = {"content": content}
        last_attempt_index = len(_RETRY_DELAYS_SECONDS)

        for attempt in range(last_attempt_index + 1):
            try:
                response = self._http_post(self._webhook_url, json=payload)
            except httpx.TransportError:
                logger.exception(
                    "Discord webhook transport error (attempt %d)", attempt + 1
                )
            else:
                if response.status_code < httpx.codes.BAD_REQUEST:
                    return True
                if not _is_retryable_status(response.status_code):
                    logger.warning(
                        "Discord webhook returned non-retryable status %d",
                        response.status_code,
                    )
                    return False
                logger.warning(
                    "Discord webhook returned retryable status %d (attempt %d)",
                    response.status_code,
                    attempt + 1,
                )

            if attempt < last_attempt_index:
                self._sleep_fn(_RETRY_DELAYS_SECONDS[attempt])

        return False
