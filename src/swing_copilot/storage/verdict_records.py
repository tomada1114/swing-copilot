"""`verdicts` / `verdict_sources` / `verdict_outcomes` repository (P8-30).

The retrospective mechanism's three tables, written only by `retro/`
(`docs/goal-prompts/swing-copilot-retrospective/design.md` §4). Both writers
are *full replacements* rather than plain upserts:

* `replace_run_verdicts` replaces one run's entire verdict set, so
  re-ingesting a corrected `analysis_result.json` both updates changed rows
  and drops symbols that are no longer part of the answer.
* `replace_verdict_outcomes` replaces one `(run_id, horizon_days)` slice,
  mirroring `audit_records.replace_signal_outcomes`, so re-evaluating after a
  price correction reclassifies instead of duplicating.

Each replacement is one transaction: a failure after an earlier statement
succeeded rolls the whole write back and leaves the previous state intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from swing_copilot.storage.json_guard import dumps_safe

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from swing_copilot.storage.database import Database

_INSERT_VERDICT = """
    INSERT INTO verdicts (
        run_id, symbol, as_of, strategy_key, recommendation, reasons_json, no_trade
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_VERDICT_SOURCE = """
    INSERT INTO verdict_sources (run_id, symbol, source_id, source_type)
    VALUES (?, ?, ?, ?)
"""

_INSERT_VERDICT_OUTCOME = """
    INSERT INTO verdict_outcomes (
        run_id, symbol, horizon_days, as_of, recommendation,
        forward_return_pct, classification
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True, slots=True)
class VerdictReasonRecord:
    """One reason behind a verdict, as persisted inside `reasons_json`.

    `source_ids` may be empty: a reason resting only on deterministic inputs
    the pipeline itself computed has no news/filing source to cite (mirrors
    `analysis.schemas.VerdictReason`).
    """

    text: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerdictRecord:
    """One symbol's qualitative verdict from one past run."""

    run_id: UUID
    symbol: str
    as_of: date
    strategy_key: str
    recommendation: str
    reasons: tuple[VerdictReasonRecord, ...]
    no_trade: bool


@dataclass(frozen=True, slots=True)
class VerdictSourceRecord:
    """One `source_id` the analysis of `symbol` cited in that run.

    `source_type` is resolved from the code-owned `analysis_input.json`, never
    echoed back from the skill's answer (design §4).
    """

    run_id: UUID
    symbol: str
    source_id: str
    source_type: str


@dataclass(frozen=True, slots=True)
class VerdictOutcomeRecord:
    """One matured `(run, symbol, horizon)` verdict classification."""

    run_id: UUID
    symbol: str
    horizon_days: int
    as_of: date
    recommendation: str
    forward_return_pct: float
    classification: str


@dataclass(frozen=True, slots=True)
class VerdictRow:
    """The `verdicts` columns `retro/evaluate.py` needs to classify a run."""

    run_id: UUID
    symbol: str
    as_of: date
    recommendation: str


def replace_run_verdicts(
    database: Database,
    run_id: UUID,
    verdicts: Sequence[VerdictRecord],
    sources: Sequence[VerdictSourceRecord],
) -> None:
    """Atomically replace one run's complete verdict and citation set.

    Args:
        database: Shared DuckDB connection owner.
        run_id: The run whose rows are being replaced wholesale.
        verdicts: The run's verdicts. Empty clears the run (a re-ingest of a
            result that no longer analyzes any symbol).
        sources: The `source_id`s those verdicts' analyses cited.

    Raises:
        ValueError: A record belongs to a different run than `run_id`.
    """
    _reject_foreign_run(run_id, (record.run_id for record in verdicts))
    _reject_foreign_run(run_id, (record.run_id for record in sources))

    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute("DELETE FROM verdicts WHERE run_id = ?", [str(run_id)])
            conn.execute("DELETE FROM verdict_sources WHERE run_id = ?", [str(run_id)])
            for verdict in verdicts:
                conn.execute(
                    _INSERT_VERDICT,
                    [
                        str(verdict.run_id),
                        verdict.symbol,
                        verdict.as_of,
                        verdict.strategy_key,
                        verdict.recommendation,
                        dumps_safe(
                            [
                                {
                                    "text": reason.text,
                                    "source_ids": list(reason.source_ids),
                                }
                                for reason in verdict.reasons
                            ]
                        ),
                        verdict.no_trade,
                    ],
                )
            for source in sources:
                conn.execute(
                    _INSERT_VERDICT_SOURCE,
                    [
                        str(source.run_id),
                        source.symbol,
                        source.source_id,
                        source.source_type,
                    ],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def replace_verdict_outcomes(
    database: Database,
    run_id: UUID,
    horizon_days: int,
    outcomes: Sequence[VerdictOutcomeRecord],
) -> None:
    """Atomically replace one `(run_id, horizon_days)` slice of classifications.

    Args:
        database: Shared DuckDB connection owner.
        run_id: The evaluated run.
        horizon_days: The evaluated horizon (5 or 20).
        outcomes: Classified rows. Empty clears the slice.

    Raises:
        ValueError: A record's `run_id`/`horizon_days` disagrees with the
            replacement scope.
    """
    if any(
        outcome.run_id != run_id or outcome.horizon_days != horizon_days
        for outcome in outcomes
    ):
        msg = "all outcomes must match the replacement run_id and horizon_days"
        raise ValueError(msg)

    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                "DELETE FROM verdict_outcomes WHERE run_id = ? AND horizon_days = ?",
                [str(run_id), horizon_days],
            )
            for outcome in outcomes:
                conn.execute(
                    _INSERT_VERDICT_OUTCOME,
                    [
                        str(outcome.run_id),
                        outcome.symbol,
                        outcome.horizon_days,
                        outcome.as_of,
                        outcome.recommendation,
                        outcome.forward_return_pct,
                        outcome.classification,
                    ],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def get_verdicts_in_window(
    database: Database, window_start: date, as_of: date
) -> tuple[VerdictRow, ...]:
    """Return collected verdicts whose run date falls in `[window_start, as_of]`.

    Both ends are inclusive: the run dated exactly `as_of` is in scope, and a
    run dated one day later is not (the point-in-time cutoff is a `<=`).

    Args:
        database: Shared DuckDB connection owner.
        window_start: Inclusive earliest run date to evaluate.
        as_of: Inclusive latest run date to evaluate.

    Returns:
        Rows ordered by `(as_of, run_id, symbol)` for a deterministic
        evaluation order.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, symbol, as_of, recommendation
            FROM verdicts
            WHERE as_of >= ? AND as_of <= ?
            ORDER BY as_of, run_id, symbol
            """,
            [window_start, as_of],
        ).fetchall()
    return tuple(
        VerdictRow(
            run_id=UUID(str(run_id)),
            symbol=symbol,
            as_of=row_as_of,
            recommendation=recommendation,
        )
        for run_id, symbol, row_as_of, recommendation in rows
    )


def _reject_foreign_run(run_id: UUID, candidates: Iterable[UUID]) -> None:
    """Raise if any record's run identity disagrees with the replacement scope."""
    if any(candidate != run_id for candidate in candidates):
        msg = "all records must match the replacement run_id"
        raise ValueError(msg)
