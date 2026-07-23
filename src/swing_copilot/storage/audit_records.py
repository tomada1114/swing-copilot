"""Signals/candidates/risk-assessment writes, split out of `state_store.py`.

Kept as plain functions (taking `Database` directly) rather than a second
class so `StateStore` stays the single public entry point (each method here
is a one-line delegate) while its own module stays under the project's
300-line guideline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from uuid import UUID

    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate, RejectionRecord, SignalHit
    from swing_copilot.storage.database import Database


@dataclass(frozen=True, slots=True)
class ScreeningRunMeta:
    """Per-run identifiers `record_screening_results` needs beyond the rows themselves.

    Grouped into one value (rather than three positional params) to keep
    `record_screening_results`/`StateStore.record_screening_results` within
    the project's parameter-count guideline.
    """

    run_id: UUID
    strategy_key: str
    as_of: date


def record_signals(
    database: Database, signals: Sequence[SignalHit], run_date: date, strategy_key: str
) -> None:
    """Upsert signal hits so same-date reruns can incorporate corrected input."""
    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            for hit in signals:
                conn.execute(
                    """
                    INSERT INTO signals (
                        run_date, symbol, strategy_key, signal_name, strength, metrics_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (run_date, symbol, strategy_key, signal_name) DO UPDATE SET
                        strength = EXCLUDED.strength,
                        metrics_json = EXCLUDED.metrics_json
                    """,
                    [
                        run_date,
                        hit.symbol,
                        strategy_key,
                        hit.signal_name,
                        hit.strength,
                        json.dumps(dict(hit.metrics)),
                    ],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def record_candidates(
    database: Database, candidates: Sequence[Candidate], run_id: UUID, strategy_key: str
) -> None:
    """Record one run's ranked candidates, keyed by `(run_id, symbol, strategy_key)`."""
    with database.connect() as conn:
        for candidate in candidates:
            conn.execute(
                """
                INSERT INTO candidates (
                    run_id, symbol, strategy_key, rank, signal_names, metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, symbol, strategy_key) DO UPDATE SET
                    rank = EXCLUDED.rank,
                    signal_names = EXCLUDED.signal_names,
                    metrics_json = EXCLUDED.metrics_json
                """,
                [
                    str(run_id),
                    candidate.symbol,
                    strategy_key,
                    candidate.rank,
                    list(candidate.signal_names),
                    json.dumps(dict(candidate.metrics)),
                ],
            )


def record_screening_results(
    database: Database,
    candidates: Sequence[Candidate],
    rejections: Sequence[RejectionRecord],
    meta: ScreeningRunMeta,
) -> None:
    """Record one run's candidates and rejections atomically (REQ-004/REQ-020).

    Both writes share one transaction: a failure partway through (either
    table) leaves neither table's rows from this run committed. Mirrors
    `record_signals`'s explicit transaction pattern above -- the pre-P1-02
    `record_candidates` below has no such wrapper, which is the actual gap
    this function closes for the production `_run_step_screening` call site.
    """
    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            for candidate in candidates:
                conn.execute(
                    """
                    INSERT INTO candidates (
                        run_id, symbol, strategy_key, rank, signal_names, metrics_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (run_id, symbol, strategy_key) DO UPDATE SET
                        rank = EXCLUDED.rank,
                        signal_names = EXCLUDED.signal_names,
                        metrics_json = EXCLUDED.metrics_json
                    """,
                    [
                        str(meta.run_id),
                        candidate.symbol,
                        meta.strategy_key,
                        candidate.rank,
                        list(candidate.signal_names),
                        json.dumps(dict(candidate.metrics)),
                    ],
                )
            for rejection in rejections:
                conn.execute(
                    """
                    INSERT INTO screening_rejections (
                        run_id, symbol, stage, reason_code, detail, as_of
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (run_id, symbol) DO UPDATE SET
                        stage = EXCLUDED.stage,
                        reason_code = EXCLUDED.reason_code,
                        detail = EXCLUDED.detail,
                        as_of = EXCLUDED.as_of
                    """,
                    [
                        str(meta.run_id),
                        rejection.symbol,
                        rejection.stage.value,
                        rejection.reason_code.value,
                        json.dumps(dict(rejection.detail)),
                        meta.as_of,
                    ],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def record_risk_assessments(
    database: Database, assessments: Sequence[RiskAssessment], run_id: UUID
) -> None:
    """Record one run's risk assessments, keyed by `(run_id, symbol)`."""
    with database.connect() as conn:
        for assessment in assessments:
            conn.execute(
                """
                INSERT INTO risk_assessments (
                    run_id, symbol, status, max_shares, entry_price,
                    stop_price, reasons_json, warnings_json,
                    shares_by_risk, shares_by_position_cap,
                    binding_constraint, sizing_warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, symbol) DO UPDATE SET
                    status = EXCLUDED.status,
                    max_shares = EXCLUDED.max_shares,
                    entry_price = EXCLUDED.entry_price,
                    stop_price = EXCLUDED.stop_price,
                    reasons_json = EXCLUDED.reasons_json,
                    warnings_json = EXCLUDED.warnings_json,
                    shares_by_risk = EXCLUDED.shares_by_risk,
                    shares_by_position_cap = EXCLUDED.shares_by_position_cap,
                    binding_constraint = EXCLUDED.binding_constraint,
                    sizing_warnings_json = EXCLUDED.sizing_warnings_json
                """,
                [
                    str(run_id),
                    assessment.symbol,
                    assessment.status,
                    assessment.max_shares,
                    assessment.entry_price,
                    assessment.stop_price,
                    json.dumps(list(assessment.reasons)),
                    json.dumps(
                        [
                            {
                                "warning_type": warning.warning_type,
                                "correlated_symbol": warning.correlated_symbol,
                                "correlation": warning.correlation,
                            }
                            for warning in assessment.warnings
                        ]
                    ),
                    assessment.shares_by_risk,
                    assessment.shares_by_position_cap,
                    assessment.binding_constraint,
                    json.dumps(list(assessment.sizing_warnings)),
                ],
            )
