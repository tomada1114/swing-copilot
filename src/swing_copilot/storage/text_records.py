"""Text item persistence and source-URL resolution, split out of `state_store.py`.

Same extraction pattern as `audit_records.py`/`llm_records.py`: plain functions
taking `Database` directly, so `StateStore` stays the single public entry
point while its own module stays under the project's 300-line guideline.
Persisting collected text (FR-07 adapters' in-memory `TextItem`s) lets the
report resolve an LLM fact's `source_ids` back to a clickable URL
(`docs/05_ui_design.md` 7.5) without the report module depending on any
text-source adapter directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.storage.database import Database
    from swing_copilot.text.base import TextItem


def record_text_items(database: Database, items: Sequence[TextItem]) -> None:
    """Upsert text items by `source_id`, correcting same-key reruns."""
    if not items:
        return
    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            for item in items:
                conn.execute(
                    """
                    INSERT INTO text_items (
                        source_id, symbol, source_type, published_at, title,
                        source_url, content_text, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_id) DO UPDATE SET
                        symbol = EXCLUDED.symbol,
                        source_type = EXCLUDED.source_type,
                        published_at = EXCLUDED.published_at,
                        title = EXCLUDED.title,
                        source_url = EXCLUDED.source_url,
                        content_text = EXCLUDED.content_text,
                        fetched_at = EXCLUDED.fetched_at
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
                    ],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


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
            f"SELECT source_id, source_url FROM text_items "  # noqa: S608
            f"WHERE source_id IN ({placeholders})",
            list(source_ids),
        ).fetchall()
    return dict(rows)
