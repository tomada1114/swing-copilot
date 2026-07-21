"""EDGAR filing-text collection (FR-07): a thin pass-through to `EdgarClient`.

Identity/throttling/company-lookup already live in `data/edgar.py`'s
`EdgarClient`; this module just gives text collection its documented,
FR-07-shaped entry point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from swing_copilot.text.base import TextItem


class _EdgarClientLike(Protocol):
    """Structural stand-in for `data.edgar.EdgarClient`, for fake injection."""

    def fetch_filing_texts(self, symbol: str, form_types: list[str]) -> list[TextItem]:
        """Return recent filings' full text, normalized for text collection."""
        ...  # pragma: no cover


def fetch_recent_filings_text(
    edgar_client: _EdgarClientLike, symbol: str, form_types: list[str]
) -> list[TextItem]:
    """Fetch recent filings' full text for `symbol`.

    Args:
        edgar_client: Client to fetch through.
        symbol: Ticker symbol.
        form_types: SEC form types to fetch (e.g. `["8-K", "10-Q"]`).

    Returns:
        One `TextItem` per matching filing.
    """
    return edgar_client.fetch_filing_texts(symbol, form_types)
