"""Common text data schema every text source normalizes into (FR-07)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class TextItem:
    """One collected text unit (news article, filing excerpt, or calendar event).

    Matches the `text_items` table schema (`docs/04_detailed_design.md` 4.2)
    directly, so every source's output stores identically regardless of
    origin.
    """

    source_id: str
    symbol: str | None
    source_type: str  # "news" | "filing" | "calendar"
    published_at: datetime
    title: str | None
    source_url: str
    content_text: str
    fetched_at: datetime
