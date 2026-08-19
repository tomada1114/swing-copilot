"""Read-only history queries backing `copilot-history` (P1-05).

Plain functions taking `Database` directly, mirroring the
`audit_records.py`/`paper_records.py` split-out-module pattern: every
function here is `SELECT`-only (REQ-007 — `copilot-history` never writes),
kept out of `state_store.py` so that module stays under the project's
300-line guideline and so its own write surface is unambiguous.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swing_copilot.storage import paper_records

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from uuid import UUID

    from swing_copilot.storage.database import Database
    from swing_copilot.storage.paper_records import TradeDecisionRecord


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One `runs` row plus derived counts, for the `runs` subcommand (REQ-002)."""

    run_id: UUID
    run_date: date
    candidate_count: int
    rejection_count: int
    decision_count: int


@dataclass(frozen=True, slots=True)
class RunCandidateRow:
    """One candidate as recorded for a specific run (REQ-003)."""

    symbol: str
    strategy_key: str
    rank: int
    score: float | None
    signal_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunRiskRow:
    """One risk assessment as recorded for a specific run (REQ-003)."""

    symbol: str
    status: str
    max_shares: int | None
    binding_constraint: str | None


@dataclass(frozen=True, slots=True)
class RunDetail:
    """Full detail for one run: candidates + risk + decisions (REQ-003)."""

    run_id: UUID
    run_date: date
    candidates: tuple[RunCandidateRow, ...]
    risk_assessments: tuple[RunRiskRow, ...]
    decisions: tuple[TradeDecisionRecord, ...]


@dataclass(frozen=True, slots=True)
class RejectionRow:
    """One `screening_rejections` row for one run (REQ-005, P1-02's table)."""

    symbol: str
    stage: str
    reason_code: str
    detail: dict[str, float | int | str | None]
    as_of: date


@dataclass(frozen=True, slots=True)
class TruncationRow:
    """One `screening_truncations` row for one run (Issue #188).

    Deliberately not `TruncatedCandidate`: that value carries the in-memory
    score breakdown a single ranking produced, whereas this is what a *past*
    run persisted, retained only down to the configured cap.
    """

    symbol: str
    strategy_key: str
    rank: int
    score: float
    execution_state: str
    as_of: date


@dataclass(frozen=True, slots=True)
class SymbolCandidacyRow:
    """One run where a symbol appeared as a candidate (REQ-004)."""

    run_id: UUID
    run_date: date
    strategy_key: str
    rank: int
    score: float | None


@dataclass(frozen=True, slots=True)
class SymbolDecisionRow:
    """One recorded decision for a symbol, across any strategy (REQ-004)."""

    run_id: UUID
    run_date: date
    strategy_key: str
    decision: str
    reason_memo: str | None
    realized_return_pct: float | None


@dataclass(frozen=True, slots=True)
class SignalOutcomeRow:
    """One `signal_outcomes` row, read back for the P2-11 markdown aggregation."""

    run_id: UUID
    symbol: str
    horizon_days: int
    as_of: date
    signal_names: tuple[str, ...]
    forward_return_pct: float
    classification: str


@dataclass(frozen=True, slots=True)
class SymbolTimeline:
    """One symbol's cross-run candidacy/decision timeline (REQ-004)."""

    symbol: str
    candidacies: tuple[SymbolCandidacyRow, ...]
    decisions: tuple[SymbolDecisionRow, ...]


def _extract_score(metrics_json: str) -> float | None:
    metrics: dict[str, Any] = json.loads(metrics_json)  # Any: JSON has no static shape
    score = metrics.get("score")
    return float(score) if isinstance(score, int | float) else None


def _load_detail(detail_json: str) -> dict[str, float | int | str | None]:
    value: dict[str, Any] = json.loads(detail_json)  # Any: opaque per-reason blob
    return value


def list_runs(database: Database, limit: int) -> list[RunSummary]:
    """Return the most recent `limit` runs with derived per-table counts.

    Args:
        database: Shared DuckDB connection owner.
        limit: Maximum number of runs to return.

    Returns:
        Newest-`run_date`-first `RunSummary` rows. A run with zero
        candidates, rejections, or decisions still appears with `0` in that
        column (LEFT JOIN against subqueries), never silently dropped.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT r.run_id, r.run_date,
                   COALESCE(c.candidate_count, 0),
                   COALESCE(sr.rejection_count, 0),
                   COALESCE(tj.decision_count, 0)
            FROM runs r
            LEFT JOIN (
                SELECT run_id, COUNT(*) AS candidate_count
                FROM candidates GROUP BY run_id
            ) c ON c.run_id = r.run_id
            LEFT JOIN (
                SELECT run_id, COUNT(*) AS rejection_count
                FROM screening_rejections GROUP BY run_id
            ) sr ON sr.run_id = r.run_id
            LEFT JOIN (
                SELECT run_id, COUNT(*) AS decision_count
                FROM trades_journal GROUP BY run_id
            ) tj ON tj.run_id = r.run_id
            ORDER BY r.run_date DESC, r.started_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    return [
        RunSummary(
            run_id=row[0],
            run_date=row[1],
            candidate_count=row[2],
            rejection_count=row[3],
            decision_count=row[4],
        )
        for row in rows
    ]


def run_exists(database: Database, run_id: UUID) -> bool:
    """Return whether `run_id` has a `runs` row (used for friendly not-found handling)."""
    with database.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", [str(run_id)]
        ).fetchone()
    return row is not None


def get_run_started_at(database: Database, run_id: UUID) -> datetime | None:
    """Return one run's `started_at`, or `None` if it has no `runs` row (P8-119).

    Backs `retro/collect.py`'s same-day duplicate tie-break: when two
    `reports/<date>/` run directories exist for one day, the one whose
    `runs.started_at` is later is adopted. `None` means the DB and the
    `reports/` directory tree have diverged, so the caller collects that run
    unconditionally rather than guessing an order.

    Args:
        database: Shared DuckDB connection owner.
        run_id: The run to look up.

    Returns:
        The run's start timestamp, or `None` when no such `runs` row exists.
    """
    with database.connect() as conn:
        row = conn.execute(
            "SELECT started_at FROM runs WHERE run_id = ?", [str(run_id)]
        ).fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True, slots=True)
class RunStatusRow:
    """One `runs` row's lifecycle columns, for the incomplete-run scan (#129)."""

    status: str
    started_at: datetime


def get_run_statuses(
    database: Database, run_ids: Sequence[UUID]
) -> dict[UUID, RunStatusRow]:
    """Return `status`/`started_at` for each requested run, keyed by `run_id`.

    Backs `report/incomplete_runs.py`, whose primary signal is the
    filesystem: the DB is consulted only to tell a run whose analysis phase
    never finished apart from one whose deterministic pipeline itself failed
    or is still running.

    Args:
        database: Shared DuckDB connection owner.
        run_ids: Runs to look up, as discovered under `reports/`. An empty
            sequence short-circuits without opening a connection.

    Returns:
        A mapping containing only the runs that have a `runs` row. A
        `run_id` absent from the result means the `reports/` tree and the
        database have diverged; the caller reports that divergence rather
        than substituting a default status.
    """
    if not run_ids:
        return {}
    # S608: the interpolated fragment is a placeholder list derived solely
    # from `len(run_ids)`; every value is still bound as a parameter.
    placeholders = ", ".join("?" for _ in run_ids)
    with database.connect() as conn:
        rows = conn.execute(
            f"SELECT run_id, status, started_at FROM runs WHERE run_id IN ({placeholders})",  # noqa: S608
            [str(run_id) for run_id in run_ids],
        ).fetchall()
    return {row[0]: RunStatusRow(status=row[1], started_at=row[2]) for row in rows}


@dataclass(frozen=True, slots=True)
class SuccessfulRun:
    """One `run_date`'s already-completed successful run (P8-118)."""

    run_id: UUID
    report_path: Path | None


def get_successful_run(database: Database, run_date: date) -> SuccessfulRun | None:
    """Return the most recently started `status='success'` run on `run_date`.

    Backs `daily_runner.py`'s same-day rerun guard: `run_date` is resolved
    from the latest prefetched bar rather than wall-clock, so this can only
    be checked after that resolution, immediately before `start_run`. Only
    `status='success'` counts -- a prior `failed` or still-`running` row must
    not block a legitimate retry (P8-118 design; `degraded` also does not
    count, since a degraded run still produced a usable report and verdict).

    Args:
        database: Shared DuckDB connection owner.
        run_date: The resolved run date to check for a prior success.

    Returns:
        The existing run's identity and report path (`None` if it was never
        recorded), or `None` if no successful run exists on that date.
    """
    with database.connect() as conn:
        row = conn.execute(
            "SELECT run_id, report_path FROM runs "
            "WHERE run_date = ? AND status = 'success' "
            "ORDER BY started_at DESC LIMIT 1",
            [run_date],
        ).fetchone()
    if row is None:
        return None
    return SuccessfulRun(
        run_id=row[0], report_path=Path(row[1]) if row[1] is not None else None
    )


def get_run_by_date(database: Database, run_date: date) -> UUID | None:
    """Return the most recently started run at `run_date`, or `None` (P2-11).

    Backs `pipeline/postmortem.py`'s "find the run N trading days back" step.

    Args:
        database: Shared DuckDB connection owner.
        run_date: Calendar date to match against `runs.run_date`.

    Returns:
        The matching `run_id`, most-recent `started_at` first if somehow
        more than one run shares a `run_date`, or `None` if no run exists on
        that date -- the caller skips that horizon entirely rather than
        raising (roadmap's NO_PRIOR_RUN fallback).
    """
    with database.connect() as conn:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE run_date = ? ORDER BY started_at DESC LIMIT 1",
            [run_date],
        ).fetchone()
    return row[0] if row is not None else None


def get_signal_outcomes(
    database: Database, start: date, end: date
) -> tuple[SignalOutcomeRow, ...]:
    """Return `signal_outcomes` rows observed within `[start, end]` (P2-11).

    Args:
        database: Shared DuckDB connection owner.
        start: Inclusive window start, matched against each row's `as_of`
            (the date the outcome was computed, not the historical run date).
        end: Inclusive window end.

    Returns:
        Rows for the "シグナル成績" markdown aggregation, unordered.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, symbol, horizon_days, as_of, signal_names,
                   forward_return_pct, classification
            FROM signal_outcomes
            WHERE as_of >= ? AND as_of <= ?
            """,
            [start, end],
        ).fetchall()
    return tuple(
        SignalOutcomeRow(
            run_id=row[0],
            symbol=row[1],
            horizon_days=row[2],
            as_of=row[3],
            signal_names=tuple(row[4]),
            forward_return_pct=row[5],
            classification=row[6],
        )
        for row in rows
    )


def get_run_detail(database: Database, run_id: UUID) -> RunDetail | None:
    """Return one run's candidates/risk/decisions, or `None` if unknown.

    Args:
        database: Shared DuckDB connection owner.
        run_id: The run to look up.

    Returns:
        `None` if `run_id` has no `runs` row (REQ-003 boundary: the caller
        renders a friendly "not found" message instead of an exception or a
        misleadingly-empty detail).
    """
    with database.connect() as conn:
        run_row = conn.execute(
            "SELECT run_date FROM runs WHERE run_id = ?", [str(run_id)]
        ).fetchone()
        if run_row is None:
            return None
        candidate_rows = conn.execute(
            """
            SELECT symbol, strategy_key, rank, metrics_json, signal_names
            FROM candidates
            WHERE run_id = ?
            ORDER BY strategy_key, rank
            """,
            [str(run_id)],
        ).fetchall()
        risk_rows = conn.execute(
            """
            SELECT symbol, status, max_shares, binding_constraint
            FROM risk_assessments
            WHERE run_id = ?
            ORDER BY symbol
            """,
            [str(run_id)],
        ).fetchall()

    candidates = tuple(
        RunCandidateRow(
            symbol=row[0],
            strategy_key=row[1],
            rank=row[2],
            score=_extract_score(row[3]),
            signal_names=tuple(row[4]),
        )
        for row in candidate_rows
    )
    risk_assessments = tuple(
        RunRiskRow(
            symbol=row[0], status=row[1], max_shares=row[2], binding_constraint=row[3]
        )
        for row in risk_rows
    )
    decisions = tuple(paper_records.get_trade_decisions(database, run_id))
    return RunDetail(
        run_id=run_id,
        run_date=run_row[0],
        candidates=candidates,
        risk_assessments=risk_assessments,
        decisions=decisions,
    )


def get_rejections(database: Database, run_id: UUID) -> list[RejectionRow]:
    """Return `screening_rejections` rows for one run (REQ-005, P1-02's table)."""
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT symbol, stage, reason_code, detail, as_of
            FROM screening_rejections
            WHERE run_id = ?
            ORDER BY symbol
            """,
            [str(run_id)],
        ).fetchall()
    return [
        RejectionRow(
            symbol=row[0],
            stage=row[1],
            reason_code=row[2],
            detail=_load_detail(row[3]),
            as_of=row[4],
        )
        for row in rows
    ]


def get_truncations(database: Database, run_id: UUID) -> list[TruncationRow]:
    """Return `screening_truncations` rows for one run (Issue #188's table).

    Args:
        database: Shared DuckDB connection owner.
        run_id: The run whose near-misses to read.

    Returns:
        Rows ordered by rank, closest to the cut first. Empty for a run that
        predates the table or whose ranking had no tail at all.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT symbol, strategy_key, rank, score, execution_state, as_of
            FROM screening_truncations
            WHERE run_id = ?
            ORDER BY rank, symbol
            """,
            [str(run_id)],
        ).fetchall()
    return [
        TruncationRow(
            symbol=row[0],
            strategy_key=row[1],
            rank=row[2],
            score=row[3],
            execution_state=row[4],
            as_of=row[5],
        )
        for row in rows
    ]


def get_symbol_timeline(database: Database, symbol: str) -> SymbolTimeline | None:
    """Return one symbol's cross-run candidacy/decision timeline (REQ-004).

    Args:
        database: Shared DuckDB connection owner.
        symbol: Ticker to look up (matched exactly; callers normalize case).

    Returns:
        `None` if `symbol` was never recorded as a candidate in any run
        (REQ-004 boundary: the caller renders "<SYM>の記録はありません").
        A symbol's decisions are still merged in even when they came from a
        strategy/run other than the one that most recently listed it as a
        candidate -- this is a cross-run, cross-strategy view by design.
    """
    with database.connect() as conn:
        candidacy_rows = conn.execute(
            """
            SELECT c.run_id, r.run_date, c.strategy_key, c.rank, c.metrics_json
            FROM candidates c
            JOIN runs r ON r.run_id = c.run_id
            WHERE c.symbol = ?
            ORDER BY r.run_date DESC, c.strategy_key
            """,
            [symbol],
        ).fetchall()
        if not candidacy_rows:
            return None
        decision_rows = conn.execute(
            """
            SELECT t.run_id, r.run_date, t.strategy_key, t.decision, t.reason_memo,
                   CASE
                     WHEN p.status = 'closed' AND p.entry_price > 0
                          AND p.close_price IS NOT NULL
                     THEN (p.close_price - p.entry_price) / p.entry_price
                     ELSE NULL
                   END AS realized_return_pct
            FROM trades_journal t
            JOIN runs r ON r.run_id = t.run_id
            LEFT JOIN positions p ON p.position_id = t.position_id
            WHERE t.symbol = ?
            ORDER BY r.run_date DESC, t.created_at DESC
            """,
            [symbol],
        ).fetchall()

    candidacies = tuple(
        SymbolCandidacyRow(
            run_id=row[0],
            run_date=row[1],
            strategy_key=row[2],
            rank=row[3],
            score=_extract_score(row[4]),
        )
        for row in candidacy_rows
    )
    decisions = tuple(
        SymbolDecisionRow(
            run_id=row[0],
            run_date=row[1],
            strategy_key=row[2],
            decision=row[3],
            reason_memo=row[4],
            realized_return_pct=row[5],
        )
        for row in decision_rows
    )
    return SymbolTimeline(symbol=symbol, candidacies=candidacies, decisions=decisions)
