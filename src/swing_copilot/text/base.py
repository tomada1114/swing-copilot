"""Common text data schema every text source normalizes into (FR-07)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

#: Inserted into `TextItem.content_text` where an adapter cut an exhibit off at
#: its *collection-stage* ceiling (`data/edgar.py`'s
#: `_MAX_EXHIBIT_CHARS_PER_FILING`), so the loss is visible in the text itself
#: rather than in a field that stops at the collection boundary (Issue #157).
#: Declared here, beside `TextItem`, because the writer (`data/edgar.py`) and
#: the reader (`analysis/filing_selection.py`) must share one literal and
#: cannot drift apart -- and because `analysis/` importing `data/edgar.py`
#: would drag edgartools (a ~20s import) into the ingest path for a string.
EXHIBIT_TRUNCATION_MARKER = "\n[... exhibit truncated ...]"


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
    stores identically regardless of origin. `filing_sections` is a
    collection-time analysis aid that is not persisted; `related_symbols` and
    `category` are persisted (P8-123) so ticker-collision observation does
    not depend on re-fetching.
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
