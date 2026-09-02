"""DuckDB persistence for the latest earnings schedule per symbol."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swing_copilot.data.earnings import EarningsEvent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.storage.database import Database


def upsert_earnings_calendar(
    database: Database, events: Sequence[EarningsEvent]
) -> None:
    """Correction-upsert a logical batch atomically."""
    if not events:
        return
    with database.transaction() as conn:
        for event in events:
            conn.execute(
                """
                INSERT INTO earnings_calendar (
                    symbol, earnings_date, session, fetched_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (symbol) DO UPDATE SET
                    earnings_date = EXCLUDED.earnings_date,
                    session = EXCLUDED.session,
                    fetched_at = EXCLUDED.fetched_at
                """,
                [
                    event.symbol,
                    event.earnings_date,
                    event.session,
                    event.fetched_at,
                ],
            )


def get_earnings_event(database: Database, symbol: str) -> EarningsEvent | None:
    """Read the latest corrected event for one symbol."""
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT symbol, earnings_date, session, fetched_at
            FROM earnings_calendar
            WHERE symbol = ?
            """,
            [symbol],
        ).fetchone()
    return None if row is None else EarningsEvent(*row)
