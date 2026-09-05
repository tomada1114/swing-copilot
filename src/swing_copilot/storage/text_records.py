"""Text item persistence and source-URL resolution, split out of `state_store.py`.

Same extraction pattern as `audit_records.py`: plain functions
taking `Database` directly, so `StateStore` stays the single public entry
point while its own module stays under the project's 300-line guideline.
Persisting collected text (FR-07 adapters' in-memory `TextItem`s) lets the
report resolve an analysis fact's `source_ids` back to a clickable URL
(`docs/05_ui_design.md` 7.5) without the report module depending on any
text-source adapter directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from swing_copilot.storage.database import Database
    from swing_copilot.text.base import TextItem


def record_text_items(database: Database, items: Sequence[TextItem]) -> None:
    """Upsert text items by `source_id`, correcting same-key reruns."""
    if not items:
        return
    with database.transaction() as conn:
        for item in items:
            conn.execute(
                """
                INSERT INTO text_items (
                    source_id, symbol, source_type, published_at, title,
                    source_url, content_text, fetched_at, related_symbols,
                    category
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_id) DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    source_type = EXCLUDED.source_type,
                    published_at = EXCLUDED.published_at,
                    title = EXCLUDED.title,
                    source_url = EXCLUDED.source_url,
                    content_text = EXCLUDED.content_text,
                    fetched_at = EXCLUDED.fetched_at,
                    related_symbols = EXCLUDED.related_symbols,
                    category = EXCLUDED.category
                """,
                [
                    item.source_id,
                    item.symbol,
                    item.source_type,
                    item.published_at,
                    item.title,
                    item.source_url,
                    item.content_text,
                    item.fetched_at,
                    ",".join(item.related_symbols) or None,
                    item.category,
                ],
            )


def latest_filing_dates(
    database: Database, symbols: Sequence[str], *, as_of: date
) -> dict[str, date]:
    """Read each symbol's most recent *collected* filing date, visible at `as_of`.

    The filing text collected by FR-07 (`source_type = "filing"`) is filing
    metadata this application already holds, so asking it "has anything been
    filed lately?" costs no extra EDGAR request — which is what lets the
    fundamentals step's incremental refresh (`docs/03_basic_design.md` 8.3,
    Issue #258) detect a new filing without reintroducing the per-symbol
    network call it exists to avoid.

    Three properties the caller must plan around:

    - Coverage is whatever text collection covered, i.e. each past run's held
      + candidate symbols (`_TEXT_SYMBOL_LIMIT`), not the whole universe.
      This is a *trigger* for an early refresh, never the only refresh rule;
      the elapsed-days rule is what covers every remaining symbol. In
      particular a symbol that becomes a candidate for the first time today
      has never had its filings collected, so the trigger cannot help it.
    - `text_items` persists across runs, so this is not scoped to one run's
      30 symbols: it can return a filing for any symbol text collection has
      *ever* touched. The caller's retry window is a week wide, so the set
      that can still be armed on a given day is drawn from roughly a week of
      collection sets -- on the order of 150-210 symbols, not 30 -- of which
      the ones actually armed are those whose newest collected filing is
      both inside that window and not yet ingested.
    - Text collection runs after the fundamentals step within a run, so a
      filing collected today is first acted on by tomorrow's run.

    `published_at` is the filing's SEC filing date (`data/edgar.py`'s
    `_filing_text_item`), so the `as_of` cutoff here is the same
    point-in-time boundary the rest of the pipeline applies to filings: one
    filed *on* `as_of` is visible, one filed the next day is not.

    Args:
        database: Shared DuckDB connection owner.
        symbols: Tickers to look up; an empty sequence returns `{}` without
            touching the database.
        as_of: Inclusive point-in-time cutoff on the filing date.

    Returns:
        `{symbol: latest visible filing date}`, omitting symbols with no
        collected filing.
    """
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with database.connect() as conn:
        rows = conn.execute(
            f"SELECT symbol, MAX(CAST(published_at AS DATE)) "  # noqa: S608 - placeholders only, values are bound
            f"FROM text_items "
            f"WHERE source_type = 'filing' AND symbol IN ({placeholders}) "
            f"AND CAST(published_at AS DATE) <= ? "
            f"GROUP BY symbol",
            [*symbols, as_of],
        ).fetchall()
    return dict(rows)


def get_source_urls(database: Database, source_ids: Sequence[str]) -> dict[str, str]:
    """Resolve known `source_ids` to their `source_url`.

    Args:
        database: Shared DuckDB connection owner.
        source_ids: Source IDs to resolve.

    Returns:
        A mapping for every `source_id` that has a recorded text item.
        Unknown IDs are silently omitted (never raised) so a report can
        still render the rest of a fact's sources.
    """
    if not source_ids:
        return {}
    with database.connect() as conn:
        placeholders = ",".join("?" for _ in source_ids)
        rows = conn.execute(
            f"SELECT source_id, source_url FROM text_items "  # noqa: S608 - placeholder count is generated locally and values are bound
            f"WHERE source_id IN ({placeholders})",
            list(source_ids),
        ).fetchall()
    return dict(rows)
