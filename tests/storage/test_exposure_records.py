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


class TestPromotedExposureColumns:
    """Issue #192: the decision's inputs as columns, not `detail_json`."""

    def test_records_gate_dd_level_downgrade_flag_and_multiplier(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        state_store.record_exposure_decision(
            run_id,
            ExposureDecision(
                ExposureVerdict.REDUCE_ONLY,
                GateVerdict.BEAR,
                DistributionLevel.CAUTION,
                DataQuality.OK,
                is_conservatively_downgraded=True,
                reduce_only_risk_multiplier=0.25,
                spy_sma200=500.0,
                vix_close=31.0,
                spy_ftd_state="FTD_CONFIRMED",
                is_ftd_active=True,
            ),
        )

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT gate_verdict, dd_level, is_conservatively_downgraded, "
                "reduce_only_risk_multiplier, spy_sma200, spy_ftd_state, ftd_active "
                "FROM exposure_decisions WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()

        assert row == ("BEAR", "CAUTION", True, 0.25, 500.0, "FTD_CONFIRMED", True)

    def test_a_correction_rewrites_the_promoted_columns(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.record_exposure_decision(
            run_id, _decision(ExposureVerdict.NEW_ENTRY_ALLOWED)
        )

        state_store.record_exposure_decision(
            run_id,
            ExposureDecision(
                ExposureVerdict.CASH_PRIORITY,
                GateVerdict.UNKNOWN,
                DistributionLevel.UNKNOWN,
                DataQuality.INSUFFICIENT,
                is_conservatively_downgraded=True,
            ),
        )

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT gate_verdict, dd_level, is_conservatively_downgraded "
                "FROM exposure_decisions WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()

        assert row == ("UNKNOWN", "UNKNOWN", True)
