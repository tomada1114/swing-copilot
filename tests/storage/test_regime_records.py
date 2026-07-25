"""Persistence contracts for P3-13 regime snapshots."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    DistributionResult,
)
from swing_copilot.regime.gate import GateVerdict, MarketGate, RegimeSnapshot

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore


def _snapshot(*, gate: GateVerdict = GateVerdict.BULL) -> RegimeSnapshot:
    result = DistributionResult(1.0, 0.0, 0.0, DistributionLevel.NORMAL, DataQuality.OK)
    return RegimeSnapshot(
        date(2026, 7, 21),
        MarketGate(gate, 520.0, 500.0, 15.0),
        result,
        result,
        DistributionLevel.NORMAL,
        DataQuality.OK,
    )


def test_regime_snapshot_upserts_corrections(state_store: StateStore) -> None:
    run_id = uuid4()
    state_store.record_regime_snapshot(run_id, _snapshot())
    state_store.record_regime_snapshot(run_id, _snapshot(gate=GateVerdict.BEAR))

    with state_store.database.connect() as conn:
        rows = conn.execute(
            "SELECT gate_verdict, dd_level, data_quality FROM regime_snapshots WHERE run_id = ?",
            [str(run_id)],
        ).fetchall()
    assert rows == [("BEAR", "NORMAL", "OK")]
