"""Persistence contracts for P3-14 Exposure Ceiling decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from swing_copilot.regime.distribution import DataQuality, DistributionLevel
from swing_copilot.regime.exposure import ExposureDecision, ExposureVerdict
from swing_copilot.regime.gate import GateVerdict

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore


def _decision(verdict: ExposureVerdict) -> ExposureDecision:
    return ExposureDecision(
        verdict,
        GateVerdict.BULL,
        DistributionLevel.NORMAL,
        DataQuality.OK,
        False,
    )


def test_exposure_decision_upserts_corrections(state_store: StateStore) -> None:
    run_id = uuid4()
    state_store.record_exposure_decision(
        run_id, _decision(ExposureVerdict.NEW_ENTRY_ALLOWED)
    )
    state_store.record_exposure_decision(run_id, _decision(ExposureVerdict.REDUCE_ONLY))

    with state_store.database.connect() as conn:
        rows = conn.execute(
            "SELECT verdict, data_quality FROM exposure_decisions WHERE run_id = ?",
            [str(run_id)],
        ).fetchall()
    assert rows == [("REDUCE_ONLY", "OK")]
