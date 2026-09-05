"""DuckDB persistence contracts for FTD state-transition history."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from swing_copilot.regime.distribution import DataQuality
from swing_copilot.regime.ftd import FtdResult, FtdSnapshot, FtdState, FtdTransition

if TYPE_CHECKING:
    import duckdb


class _FlakyFtdConnection:
    """Wraps a real connection; raises on a later FTD history insert."""

    def __init__(self, real_conn: duckdb.DuckDBPyConnection, fail_on_call: int):
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._insert_calls = 0

    def execute(self, sql, parameters=None):
        if sql.lstrip().startswith("INSERT INTO ftd_state_history"):
            self._insert_calls += 1
            if self._insert_calls == self._fail_on_call:
                msg = "simulated failure on a later FTD history insert"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._real.close()


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


def test_rolls_back_entirely_when_a_later_transition_insert_fails(
    state_store, monkeypatch
):
    run_id = uuid4()
    snapshot = FtdSnapshot(
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
            "QQQ",
            FtdState.FTD_CONFIRMED,
            DataQuality.OK,
            4,
            65,
            date(2026, 7, 21),
            (FtdTransition(date(2026, 7, 20), FtdState.DAY1, 1, 65),),
        ),
    )

    real_connect = state_store.database.connect
    monkeypatch.setattr(
        state_store.database,
        "connect",
        lambda: _FlakyFtdConnection(real_connect(), fail_on_call=2),
    )

    with pytest.raises(RuntimeError, match="simulated failure"):
        state_store.record_ftd_history(run_id, snapshot)

    with state_store.database.connect() as conn:
        rows = conn.execute(
            "SELECT symbol, sequence FROM ftd_state_history WHERE run_id = ?",
            [str(run_id)],
        ).fetchall()
    assert rows == []
