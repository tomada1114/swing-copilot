"""Persistence for immutable-in-run FTD state-transition history."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from swing_copilot.regime.ftd import FtdSnapshot
    from swing_copilot.storage.database import Database


def record_ftd_history(database: Database, run_id: UUID, snapshot: FtdSnapshot) -> None:
    """Replace one run's derived transition history atomically for correction reruns."""
    rows = [
        (result.symbol, sequence, transition)
        for result in (snapshot.spy, snapshot.qqq)
        for sequence, transition in enumerate(result.transitions)
    ]
    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                "DELETE FROM ftd_state_history WHERE run_id = ?", [str(run_id)]
            )
            for symbol, sequence, transition in rows:
                conn.execute(
                    """
                    INSERT INTO ftd_state_history (
                        run_id, symbol, sequence, as_of, state, day_number, quality_score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(run_id),
                        symbol,
                        sequence,
                        transition.date,
                        transition.state.value,
                        transition.day_number,
                        transition.quality_score,
                    ],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
