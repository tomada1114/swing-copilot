"""Signals/candidates/risk-assessment writes, split out of `state_store.py`.

Kept as plain functions (taking `Database` directly) rather than a second
class so `StateStore` stays the single public entry point (each method here
is a one-line delegate) while its own module stays under the project's
300-line guideline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.storage.json_guard import dumps_safe

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from uuid import UUID

    import duckdb

    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import (
        Candidate,
        ScreeningResult,
        SignalHit,
        TruncatedCandidate,
    )
    from swing_copilot.storage.database import Database

#: How far past `candidate_limit` the truncated ranking is persisted
#: (Issue #188). The questions these rows exist for -- "would a limit of 8
#: have helped", "is rank 6-10 really worse than 1-5" -- all live just below
#: the cut; the long tail of a few hundred also-rans would answer none of
#: them while writing two orders of magnitude more rows every day. Three
#: times the limit keeps roughly two further "pages" of the same size.
PERSISTED_TRUNCATION_MULTIPLIER = 3

#: `universe_forward_returns.outcome_class` values (Issue #188), matching the
#: table's own CHECK constraint. Named here because the postmortem writer and
#: the research accessors must agree on them exactly.
OUTCOME_CLASS_CANDIDATE = "candidate"
OUTCOME_CLASS_TRUNCATED = "truncated"
OUTCOME_CLASS_REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ScreeningRunMeta:
    """Per-run values `record_screening_results` needs beyond the rows themselves.

    Grouped into one value (rather than four positional params) to keep
    `record_screening_results`/`StateStore.record_screening_results` within
    the project's parameter-count guideline.

    `candidate_limit` is the run's configured cap, carried here so the
    truncation retention rule (`PERSISTED_TRUNCATION_MULTIPLIER`) is applied
    once, at the write boundary, instead of at each call site.
    """

    run_id: UUID
    strategy_key: str
    as_of: date
    candidate_limit: int


#: `candidates`' promoted score columns, in the order `_score_columns` and
#: every INSERT below list them. Mirrors `screening/pipeline.py`'s
#: `_SCORE_COMPONENT_KEYS` plus the composite it sums to; the same keys are
#: still written inside `metrics_json`, which stays the raw indicator set.
_CANDIDATE_SCORE_KEYS = (
    "score",
    "score_rsi_pullback",
    "score_trend_quality",
    "score_liquidity",
    "score_atr_pct",
    "score_pivot_proximity",
    "score_rs_percentile",
    "score_criteria_met",
)


def _score_columns(candidate: Candidate) -> list[float | None]:
    """Return the promoted score columns for one candidate (Issue #192).

    `None` for a key the candidate's metrics do not carry, rather than a
    substituted zero: an absent component means the run did not compute one,
    which a reader aggregating score contributions must be able to tell from
    a computed contribution of nothing.
    """
    return [candidate.metrics.get(key) for key in _CANDIDATE_SCORE_KEYS]


def record_signals(
    database: Database, signals: Sequence[SignalHit], run_date: date, strategy_key: str
) -> None:
    """Upsert signal hits so same-date reruns can incorporate corrected input.

    Legacy (Issue #192): writes the `run_date`-keyed `signals` table, where a
    same-date `dry_run` and `live` run overwrite each other. The daily
    pipeline now writes `signal_hits` through `record_screening_results`
    instead; this entry point is kept for the existing rows' shape only.
    """
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
                        dumps_safe(dict(hit.metrics)),
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
            _insert_candidate(conn, candidate, str(run_id), strategy_key)


_INSERT_CANDIDATE = """
INSERT INTO candidates (
    run_id, symbol, strategy_key, rank, signal_names, metrics_json,
    score, score_rsi_pullback, score_trend_quality, score_liquidity,
    score_atr_pct, score_pivot_proximity, score_rs_percentile,
    score_criteria_met, execution_state, execution_distance
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (run_id, symbol, strategy_key) DO UPDATE SET
    rank = EXCLUDED.rank,
    signal_names = EXCLUDED.signal_names,
    metrics_json = EXCLUDED.metrics_json,
    score = EXCLUDED.score,
    score_rsi_pullback = EXCLUDED.score_rsi_pullback,
    score_trend_quality = EXCLUDED.score_trend_quality,
    score_liquidity = EXCLUDED.score_liquidity,
    score_atr_pct = EXCLUDED.score_atr_pct,
    score_pivot_proximity = EXCLUDED.score_pivot_proximity,
    score_rs_percentile = EXCLUDED.score_rs_percentile,
    score_criteria_met = EXCLUDED.score_criteria_met,
    execution_state = EXCLUDED.execution_state,
    execution_distance = EXCLUDED.execution_distance
"""


def _insert_candidate(
    conn: duckdb.DuckDBPyConnection,
    candidate: Candidate,
    run_id: str,
    strategy_key: str,
) -> None:
    """Correction-upsert one candidate row inside the caller's connection.

    Every promoted column is in the `DO UPDATE SET` list: a rerun whose
    ranking moved must overwrite the previous run's score and execution
    state, not leave a stale pair of them beside a corrected `metrics_json`.
    """
    conn.execute(
        _INSERT_CANDIDATE,
        [
            run_id,
            candidate.symbol,
            strategy_key,
            candidate.rank,
            list(candidate.signal_names),
            dumps_safe(dict(candidate.metrics)),
            *_score_columns(candidate),
            candidate.execution_state,
            candidate.execution_distance,
        ],
    )


def select_persisted_truncations(
    truncations: Sequence[TruncatedCandidate], candidate_limit: int
) -> tuple[TruncatedCandidate, ...]:
    """Return the retained near-misses, closest to the cut first (Issue #188).

    Args:
        truncations: One ranking's full truncated tail, in any order.
        candidate_limit: The run's configured cap. A non-positive limit
            retains nothing -- with no candidates there is no "just below the
            cut" to compare against.

    Returns:
        At most `candidate_limit * PERSISTED_TRUNCATION_MULTIPLIER` rows,
        sorted by `rank` ascending. Sorting here rather than trusting the
        caller is what makes the retention rule mean "the best-ranked
        near-misses" no matter how the sequence arrived.
    """
    if candidate_limit <= 0:
        return ()
    ordered = sorted(truncations, key=lambda item: item.rank)
    return tuple(ordered[: candidate_limit * PERSISTED_TRUNCATION_MULTIPLIER])


def record_screening_results(
    database: Database,
    result: ScreeningResult,
    meta: ScreeningRunMeta,
) -> None:
    """Record one run's candidates, rejections, truncations, and hits atomically.

    REQ-004/REQ-020 plus Issues #188/#192: all four writes share one
    transaction, so a failure partway through (any table) leaves none of this
    run's rows committed. Mirrors `record_signals`'s explicit transaction
    pattern above -- the pre-P1-02 `record_candidates` below has no such
    wrapper, which is the actual gap this function closes for the production
    `_run_step_screening` call site.

    Candidates and truncations are the two halves of one ranking, which is
    why they cannot be two writes: a committed candidate set paired with a
    missing (or stale) truncated tail would misstate where the cut fell.
    The truncated tail is therefore written as a *replacement* -- this
    strategy's existing rows for the run are deleted first -- rather than
    upserted row by row, so a rerun whose ranking moved a symbol above the
    cut does not leave it behind as a phantom near-miss. `signal_hits` is
    replaced on the same reasoning: a rerun on corrected bars where a signal
    stopped firing on a symbol must not leave that hit behind.

    Args:
        database: Shared DuckDB connection owner.
        result: The screening run's four outcomes -- ranked candidates, the
            classified rejections for the rest of the universe, the truncated
            tail (retained down to `select_persisted_truncations`' cap), and
            every signal hit the run produced. Taken as one value rather than
            four sequences precisely because they must be written together.
        meta: `(run_id, strategy_key, as_of, candidate_limit)` shared by
            every row.
    """
    retained = select_persisted_truncations(result.truncated, meta.candidate_limit)
    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            for candidate in result.candidates:
                _insert_candidate(conn, candidate, str(meta.run_id), meta.strategy_key)
            for rejection in result.rejections:
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
                        dumps_safe(dict(rejection.detail)),
                        meta.as_of,
                    ],
                )
            _replace_truncations(conn, retained, meta)
            _replace_signal_hits(conn, result.signal_hits, meta)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _replace_signal_hits(
    conn: duckdb.DuckDBPyConnection,
    hits: Sequence[SignalHit],
    meta: ScreeningRunMeta,
) -> None:
    """Replace this run/strategy's signal hits inside the caller's transaction.

    DELETE-then-INSERT rather than a row-by-row upsert (Issue #192, and the
    same reasoning as `_replace_truncations`): a rerun on corrected bars where
    a signal stopped firing on a symbol must drop that hit, not leave it
    behind next to the corrected ones.
    """
    conn.execute(
        "DELETE FROM signal_hits WHERE run_id = ? AND strategy_key = ?",
        [str(meta.run_id), meta.strategy_key],
    )
    for hit in hits:
        conn.execute(
            """
            INSERT INTO signal_hits (
                run_id, symbol, strategy_key, signal_name, strength, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                str(meta.run_id),
                hit.symbol,
                meta.strategy_key,
                hit.signal_name,
                hit.strength,
                dumps_safe(dict(hit.metrics)),
            ],
        )


def _replace_truncations(
    conn: duckdb.DuckDBPyConnection,
    truncations: Sequence[TruncatedCandidate],
    meta: ScreeningRunMeta,
) -> None:
    """Replace this run/strategy's truncated tail inside the caller's transaction."""
    conn.execute(
        "DELETE FROM screening_truncations WHERE run_id = ? AND strategy_key = ?",
        [str(meta.run_id), meta.strategy_key],
    )
    for truncation in truncations:
        breakdown = truncation.score_breakdown
        conn.execute(
            """
            INSERT INTO screening_truncations (
                run_id, symbol, strategy_key, rank, score,
                score_rsi_pullback, score_trend_quality, score_liquidity,
                score_atr_pct, score_pivot_proximity, score_rs_percentile,
                score_criteria_met, execution_state, execution_distance, as_of
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(meta.run_id),
                truncation.symbol,
                meta.strategy_key,
                truncation.rank,
                truncation.score,
                breakdown.get("score_rsi_pullback"),
                breakdown.get("score_trend_quality"),
                breakdown.get("score_liquidity"),
                breakdown.get("score_atr_pct"),
                breakdown.get("score_pivot_proximity"),
                breakdown.get("score_rs_percentile"),
                breakdown.get("score_criteria_met"),
                truncation.execution_state,
                truncation.execution_distance,
                meta.as_of,
            ],
        )


@dataclass(frozen=True, slots=True)
class SignalOutcomeRecord:
    """One classified forward-return outcome for a past run's candidate (P2-11).

    `run_id` is the HISTORICAL run whose candidate is being evaluated, not
    the run performing today's postmortem pass. `signal_names` is a
    denormalized copy of that candidate's signal names, so the P2-11
    markdown aggregation never needs to join back to `candidates`.
    """

    run_id: UUID
    symbol: str
    horizon_days: int
    as_of: date
    signal_names: tuple[str, ...]
    forward_return_pct: float
    classification: str


_UPSERT_SIGNAL_OUTCOMES = """
INSERT INTO signal_outcomes (
    run_id, symbol, horizon_days, as_of, signal_names,
    forward_return_pct, classification
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (run_id, symbol, horizon_days) DO UPDATE SET
    as_of = EXCLUDED.as_of,
    signal_names = EXCLUDED.signal_names,
    forward_return_pct = EXCLUDED.forward_return_pct,
    classification = EXCLUDED.classification
"""


def record_signal_outcomes(
    database: Database, outcomes: Sequence[SignalOutcomeRecord]
) -> None:
    """Upsert postmortem outcomes, keyed by `(run_id, symbol, horizon_days)`.

    A rerun that recomputes a horizon with corrected price data must update
    the existing row rather than duplicate it or silently no-op (AGENTS.md's
    correction-upsert invariant); mirrors `MarketStore.upsert_fundamentals`'s
    guard-then-transaction pattern.

    Args:
        database: Shared DuckDB connection owner.
        outcomes: Rows to upsert; a no-op if empty.
    """
    if not outcomes:
        return
    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            for outcome in outcomes:
                conn.execute(
                    _UPSERT_SIGNAL_OUTCOMES,
                    [
                        str(outcome.run_id),
                        outcome.symbol,
                        outcome.horizon_days,
                        outcome.as_of,
                        list(outcome.signal_names),
                        outcome.forward_return_pct,
                        outcome.classification,
                    ],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def replace_signal_outcomes(
    database: Database,
    run_id: UUID,
    horizon_days: int,
    outcomes: Sequence[SignalOutcomeRecord],
) -> None:
    """Atomically replace one historical run/horizon's complete outcome set."""
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
                "DELETE FROM signal_outcomes WHERE run_id = ? AND horizon_days = ?",
                [str(run_id), horizon_days],
            )
            for outcome in outcomes:
                conn.execute(
                    _UPSERT_SIGNAL_OUTCOMES,
                    [
                        str(outcome.run_id),
                        outcome.symbol,
                        outcome.horizon_days,
                        outcome.as_of,
                        list(outcome.signal_names),
                        outcome.forward_return_pct,
                        outcome.classification,
                    ],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


@dataclass(frozen=True, slots=True)
class UniverseForwardReturnRecord:
    """One symbol's realized return over one horizon, tagged by its screening fate.

    `run_id` is the HISTORICAL run whose screening decision is being measured
    (same convention as `SignalOutcomeRecord`), and `reason_code` is that
    run's `screening_rejections.reason_code` when `outcome_class` is
    `rejected`, `None` otherwise -- a candidate or a near-miss was rejected
    by nothing, so there is no code to carry.
    """

    run_id: UUID
    symbol: str
    horizon_days: int
    as_of: date
    outcome_class: str
    reason_code: str | None
    forward_return_pct: float


def replace_universe_forward_returns(
    database: Database,
    run_id: UUID,
    horizon_days: int,
    returns: Sequence[UniverseForwardReturnRecord],
) -> None:
    """Atomically replace one historical run/horizon's control-group returns.

    Full replacement (DELETE then INSERT in one transaction), mirroring
    `replace_signal_outcomes`: a rerun against corrected bars must not leave
    behind a row whose symbol dropped out of the recomputed set, and a
    partially rewritten horizon must never be readable. This is what makes
    the postmortem step idempotent (Issue #188's DoD).

    Args:
        database: Shared DuckDB connection owner.
        run_id: The historical run being evaluated.
        horizon_days: The horizon being replaced.
        returns: The complete recomputed set for that `(run_id, horizon)`.

    Raises:
        ValueError: A record disagrees with the `(run_id, horizon_days)`
            slice being replaced -- writing it would silently touch another
            slice the DELETE above did not clear.
    """
    if any(
        record.run_id != run_id or record.horizon_days != horizon_days
        for record in returns
    ):
        msg = "all returns must match the replacement run_id and horizon_days"
        raise ValueError(msg)

    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                "DELETE FROM universe_forward_returns "
                "WHERE run_id = ? AND horizon_days = ?",
                [str(run_id), horizon_days],
            )
            for record in returns:
                conn.execute(
                    """
                    INSERT INTO universe_forward_returns (
                        run_id, symbol, horizon_days, as_of, outcome_class,
                        reason_code, forward_return_pct
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (run_id, symbol, horizon_days) DO UPDATE SET
                        as_of = EXCLUDED.as_of,
                        outcome_class = EXCLUDED.outcome_class,
                        reason_code = EXCLUDED.reason_code,
                        forward_return_pct = EXCLUDED.forward_return_pct
                    """,
                    [
                        str(record.run_id),
                        record.symbol,
                        record.horizon_days,
                        record.as_of,
                        record.outcome_class,
                        record.reason_code,
                        record.forward_return_pct,
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
    """Record one run's risk assessments, keyed by `(run_id, symbol)`.

    One run's assessments are one logical write: they commit together or not
    at all. Without the explicit transaction, DuckDB autocommits per row, and
    a mid-batch failure would leave a partial run — indistinguishable, when
    read back, from a run whose later symbols were never assessed.
    """
    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            _insert_risk_assessments(conn, assessments, run_id)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _insert_risk_assessments(
    conn: duckdb.DuckDBPyConnection,
    assessments: Sequence[RiskAssessment],
    run_id: UUID,
) -> None:
    for assessment in assessments:
        conn.execute(
            """
                INSERT INTO risk_assessments (
                    run_id, symbol, status, max_shares, entry_price, limit_price,
                    stop_price, reasons_json, warnings_json,
                    shares_by_risk, shares_by_position_cap,
                    binding_constraint, sizing_warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, symbol) DO UPDATE SET
                    status = EXCLUDED.status,
                    max_shares = EXCLUDED.max_shares,
                    entry_price = EXCLUDED.entry_price,
                    limit_price = EXCLUDED.limit_price,
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
                assessment.limit_price,
                assessment.stop_price,
                dumps_safe(list(assessment.reasons)),
                dumps_safe(
                    [
                        {
                            "warning_type": warning.warning_type,
                            "correlated_symbol": warning.correlated_symbol,
                            # P1-04: risk/checks.py intentionally uses NaN
                            # as `CorrelationWarning.correlation`'s
                            # sentinel for "data_quality" (insufficient
                            # history to compute a correlation). JSON has
                            # no NaN literal, so persist that sentinel as
                            # `null` -- the spec-compliant representation
                            # of "not computable" -- rather than letting
                            # dumps_safe reject the whole row.
                            "correlation": (
                                warning.correlation
                                if math.isfinite(warning.correlation)
                                else None
                            ),
                        }
                        for warning in assessment.warnings
                    ]
                ),
                assessment.shares_by_risk,
                assessment.shares_by_position_cap,
                assessment.binding_constraint,
                dumps_safe(list(assessment.sizing_warnings)),
            ],
        )
