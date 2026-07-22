"""Discord webhook report notification (FR-09, NFR-07).

`Notifier` is a small Protocol so a future channel (email, Slack) plugs into
`pipeline/daily.py` step 8 by adding one new class, never touching the
pipeline itself. `DiscordNotifier.notify()` never raises — it has no
`StateStore` dependency and returns whether the send succeeded so the
*caller* (the pipeline) can record a `run_steps` failure without stopping
the batch (`docs/04_detailed_design.md` 3.18's `-> None` signature is a
stale placeholder here: without a return value the caller would have no
way to detect a failed send that raised nothing, so this module returns
`bool` instead — see the P2-4 divergence note).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 10.0


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


class DiscordNotifier:
    """`Notifier` implementation sending to a Discord webhook (FR-09, opt-in)."""

    def __init__(
        self, webhook_url: str, *, http_post: _HttpPost = _real_http_post
    ) -> None:
        """Create the notifier.

        Args:
            webhook_url: Discord webhook URL (from `Secrets`).
            http_post: Injectable HTTP POST, used by tests to avoid real
                network/webhook calls.
        """
        self._webhook_url = webhook_url
        self._http_post = http_post

    def notify(self, summary: str, report_path: Path | None) -> bool:
        """See `Notifier.notify`."""
        content = summary if report_path is None else f"{summary}\n{report_path}"
        try:
            response = self._http_post(self._webhook_url, json={"content": content})
            response.raise_for_status()
        except Exception:
            logger.exception("Discord webhook notification failed")
            return False
        return True
