"""DuckDB persistence contracts for FTD state-transition history."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from swing_copilot.regime.distribution import DataQuality
from swing_copilot.regime.ftd import FtdResult, FtdSnapshot, FtdState, FtdTransition


def test_replaces_a_run_transition_history_atomically(state_store):
    run_id = uuid4()
    first = FtdSnapshot(
        date(2026, 7, 21),
        FtdResult(
            "SPY",
            FtdState.FTD_CONFIRMED,
            DataQuality.OK,
            5,
            70,
            date(2026, 7, 21),
            (
                FtdTransition(
                    date(2026, 7, 19), FtdState.CORRECTION_CONFIRMED, None, None
                ),
            ),
        ),
        FtdResult(
            "QQQ", FtdState.AWAITING_CORRECTION, DataQuality.OK, None, None, None, ()
        ),
    )
    corrected = FtdSnapshot(
        date(2026, 7, 21),
        FtdResult("SPY", FtdState.EXPIRED, DataQuality.OK, None, None, None, ()),
        FtdResult(
            "QQQ", FtdState.AWAITING_CORRECTION, DataQuality.OK, None, None, None, ()
        ),
    )

    state_store.record_ftd_history(run_id, first)
    state_store.record_ftd_history(run_id, corrected)

    with state_store.database.connect() as conn:
        rows = conn.execute(
            "SELECT symbol, state FROM ftd_state_history WHERE run_id = ?",
            [str(run_id)],
        ).fetchall()
    assert rows == []
