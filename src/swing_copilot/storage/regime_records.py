"""Persistence for deterministic per-run market-regime snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swing_copilot.storage.json_guard import dumps_safe

if TYPE_CHECKING:
    from uuid import UUID

    from swing_copilot.regime.gate import RegimeSnapshot
    from swing_copilot.storage.database import Database


def record_regime_snapshot(
    database: Database, run_id: UUID, snapshot: RegimeSnapshot
) -> None:
    """Correction-upsert the one regime decision for a run."""
    detail = {
        "spy": {
            "d25": snapshot.spy_distribution.d25,
            "d15": snapshot.spy_distribution.d15,
            "d5": snapshot.spy_distribution.d5,
            "level": snapshot.spy_distribution.level.value,
        },
        "qqq": {
            "d25": snapshot.qqq_distribution.d25,
            "d15": snapshot.qqq_distribution.d15,
            "d5": snapshot.qqq_distribution.d5,
            "level": snapshot.qqq_distribution.level.value,
        },
        "gate_inputs": {
            "spy_close": snapshot.gate.spy_close,
            "spy_ema": snapshot.gate.spy_ema,
            "vix_close": snapshot.gate.vix_close,
        },
    }
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO regime_snapshots (
                run_id, as_of, gate_verdict, dd_count_spy, dd_count_qqq,
                dd_level, data_quality, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                as_of = EXCLUDED.as_of,
                gate_verdict = EXCLUDED.gate_verdict,
                dd_count_spy = EXCLUDED.dd_count_spy,
                dd_count_qqq = EXCLUDED.dd_count_qqq,
                dd_level = EXCLUDED.dd_level,
                data_quality = EXCLUDED.data_quality,
                detail_json = EXCLUDED.detail_json
            """,
            [
                str(run_id),
                snapshot.as_of,
                snapshot.gate.verdict.value,
                snapshot.spy_distribution.d25,
                snapshot.qqq_distribution.d25,
                snapshot.dd_level.value,
                snapshot.data_quality.value,
                dumps_safe(detail),
            ],
        )
