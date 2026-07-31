"""Common text data schema every text source normalizes into (FR-07)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class FilingSection:
    """One code-extracted section of a filing's primary document.

    `content_text` remains the complete audit copy. Sections are an optional
    analysis aid produced while the filing HTML is still available; an empty
    tuple means the adapter could not establish a trustworthy structure.
    """

    name: str
    content_text: str


@dataclass(frozen=True, slots=True)
class TextItem:
    """One collected text unit (news article, filing excerpt, or calendar event).

    The stored columns match the `text_items` table schema
    (`docs/04_detailed_design.md` 4.2) directly, so every source's output
    stores identically regardless of origin. The trailing fields are
    collection-time analysis aids that are not persisted.
    """

    source_id: str
    symbol: str | None
    source_type: str  # "news" | "filing" | "calendar"
    published_at: datetime
    title: str | None
    source_url: str
    content_text: str
    fetched_at: datetime
    filing_sections: tuple[FilingSection, ...] = ()
    # Provider-declared tickers (Finnhub `related`), upper-cased and
    # de-duplicated in the provider's own order. Empty means "the source did
    # not say", which is deliberately not "unrelated": a source that carries no
    # ticker metadata must not be demoted for lacking it.
    related_symbols: tuple[str, ...] = ()
    # Provider's own label for the item (Finnhub `category`), e.g. "company".
    category: str | None = None
