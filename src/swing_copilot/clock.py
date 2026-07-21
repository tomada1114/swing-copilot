"""Clock port (`docs/04_detailed_design.md` 2.2).

The only sanctioned source of "now" for the imperative shell.
Domain/application code never calls `date.today()`/`datetime.now()`
directly — it receives an explicit `as_of` instead.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol


class Clock(Protocol):
    """Abstracts wall-clock access so tests can inject a fixed instant."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        ...  # pragma: no cover

    def today(self) -> date:
        """Return the current UTC calendar date."""
        ...  # pragma: no cover


class SystemClock:
    """Real clock, backed by the system's UTC date."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        return datetime.now(UTC)

    def today(self) -> date:
        """Return `datetime.now(UTC).date()`."""
        return self.now().date()
