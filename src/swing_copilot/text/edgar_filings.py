"""EDGAR filing-text collection (FR-07): a thin pass-through to `EdgarClient`.

Identity/throttling/company-lookup already live in `data/edgar.py`'s
`EdgarClient`; this module just gives text collection its documented,
FR-07-shaped entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.text.base import TextItem


@dataclass(frozen=True, slots=True)
class FilingLookbackBounds:
    """Recency bounds for filing text collection (roadmap §5 P6-26).

    Mirrors `settings.llm.filing_lookback_days`/`max_filings_per_symbol`,
    grouped so `fetch_recent_filings_text()` stays under the project's
    5-parameter guideline.
    """

    lookback_days: int
    limit: int


class _EdgarClientLike(Protocol):
    """Structural stand-in for `data.edgar.EdgarClient`, for fake injection."""

    def fetch_filing_texts(
        self,
        symbol: str,
        form_types: list[str],
        *,
        as_of: datetime,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TextItem]:
        """Return recent filings' full text, normalized for text collection."""
        ...  # pragma: no cover


def fetch_recent_filings_text(
    edgar_client: _EdgarClientLike,
    symbol: str,
    form_types: list[str],
    as_of: date,
    bounds: FilingLookbackBounds,
) -> list[TextItem]:
    """Fetch recent filings' full text for `symbol`, bounded to a recent window.

    Previously this fetched every filing ever submitted for `symbol` (bounded
    only by `as_of`'s point-in-time upper edge), an unbounded-disclosure
    structure that -- unlike news collection's own `since`/`max_items`
    bounds -- had no lower bound or count cap (roadmap §5 P6-26). `bounds`
    gives it the same shape as news collection:
    `settings.llm.filing_lookback_days`/`max_filings_per_symbol`.

    Args:
        edgar_client: Client to fetch through.
        symbol: Ticker symbol.
        form_types: SEC form types to fetch (e.g. `["8-K", "10-Q"]`).
        as_of: Point-in-time cutoff for eligible filings (inclusive upper
            bound).
        bounds: Recency lower bound (`lookback_days`, relative to `as_of`)
            and count cap (`limit`), most-recent (`filed_at` descending)
            first.

    Returns:
        One `TextItem` per matching filing.
    """
    cutoff = datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
    since = datetime.combine(
        as_of - timedelta(days=bounds.lookback_days), datetime.min.time(), tzinfo=UTC
    )
    return edgar_client.fetch_filing_texts(
        symbol, form_types, as_of=cutoff, since=since, limit=bounds.limit
    )
