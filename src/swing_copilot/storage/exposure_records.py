"""Persistence for a run's code-owned Exposure Ceiling decision."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swing_copilot.storage.json_guard import dumps_safe

if TYPE_CHECKING:
    from uuid import UUID

    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.storage.database import Database


def record_exposure_decision(
    database: Database, run_id: UUID, decision: ExposureDecision
) -> None:
    """Correction-upsert the exposure decision and all inputs for one run."""
    detail = {
        "gate": decision.gate.value,
        "dd_level": decision.dd_level.value,
        "data_quality": decision.data_quality.value,
        "conservatively_downgraded": decision.is_conservatively_downgraded,
        "reduce_only_risk_multiplier": decision.reduce_only_risk_multiplier,
    }
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO exposure_decisions (
                run_id, verdict, data_quality, detail_json,
                gate_verdict, dd_level, is_conservatively_downgraded,
                reduce_only_risk_multiplier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                verdict = EXCLUDED.verdict,
                data_quality = EXCLUDED.data_quality,
                detail_json = EXCLUDED.detail_json,
                gate_verdict = EXCLUDED.gate_verdict,
                dd_level = EXCLUDED.dd_level,
                is_conservatively_downgraded =
                    EXCLUDED.is_conservatively_downgraded,
                reduce_only_risk_multiplier =
                    EXCLUDED.reduce_only_risk_multiplier
            """,
            [
                str(run_id),
                decision.verdict.value,
                decision.data_quality.value,
                dumps_safe(detail),
                # Issue #192: `detail`'s inputs as columns, so reviewing the
                # ceiling's own parameters is not a JSON walk. `detail_json`
                # is still written, unchanged.
                decision.gate.value,
                decision.dd_level.value,
                decision.is_conservatively_downgraded,
                decision.reduce_only_risk_multiplier,
            ],
        )
