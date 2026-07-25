"""Execution state and audit log repository (NFR-05).

`StateStore` owns every DuckDB table that is not bars/fundamentals (those
belong to `MarketStore`): universe history, run/step tracking, signals,
candidates, risk assessments, positions, the paper-trading journal, text
items, and LLM call audit records. `init_schema()` creates the full DDL from
`docs/04_detailed_design.md` 4.2 up front — later checklist items (screening,
risk, LLM, paper trading) add the write methods for their own tables as they
land, without re-touching schema creation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from swing_copilot.models import Position, RunStatus, StepStatus
from swing_copilot.storage import (
    audit_records,
    exposure_records,
    ftd_records,
    llm_records,
    paper_records,
    regime_records,
    text_records,
)
from swing_copilot.storage.schema import ALTER_SCHEMA_STATEMENTS, INIT_SCHEMA_STATEMENTS
from swing_copilot.universe import UniverseMember

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from pathlib import Path
    from uuid import UUID

    from swing_copilot.models import RunMode
    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.regime.ftd import FtdSnapshot
    from swing_copilot.regime.gate import RegimeSnapshot
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate, RejectionRecord, SignalHit
    from swing_copilot.storage.audit_records import (
        ScreeningRunMeta,
        SignalOutcomeRecord,
    )
    from swing_copilot.storage.database import Database
    from swing_copilot.storage.llm_records import LLMCallRecord
    from swing_copilot.storage.paper_records import TradeDecisionRecord
    from swing_copilot.text.base import TextItem

_UNIVERSE_SOURCE = "wikipedia"


class StateStore:
    """Execution state and audit log repository, backed by one `Database`."""

    def __init__(self, database: Database) -> None:
        """Create the store.

        Args:
            database: Shared DuckDB connection owner.
        """
        self._database = database

    @property
    def database(self) -> Database:
        """Expose the shared `Database` for read-only cross-module reuse.

        `pipeline/postmortem.py` needs direct `storage/history_queries.py`
        reads (`get_run_by_date`/`get_run_detail`/`get_signal_outcomes`)
        alongside this store's own write methods; those plain functions take
        `Database` directly by convention (see that module's docstring), so
        this is the intended seam rather than reaching into the private
        `_database` attribute from outside the class.
        """
        return self._database

    def init_schema(self) -> None:
        """Create every table this store owns (idempotent, additive only).

        Also applies `ALTER_SCHEMA_STATEMENTS` so an existing database from
        before an additive column change (e.g. P1-03's `risk_assessments`
        columns) picks them up; both statement sets are safe to re-run.
        """
        with self._database.connect() as conn:
            for statement in INIT_SCHEMA_STATEMENTS:
                conn.execute(statement)
            for statement in ALTER_SCHEMA_STATEMENTS:
                conn.execute(statement)

    def record_regime_snapshot(self, run_id: UUID, snapshot: RegimeSnapshot) -> None:
        """Persist the deterministic regime state for one run."""
        regime_records.record_regime_snapshot(self._database, run_id, snapshot)

    def record_exposure_decision(
        self, run_id: UUID, decision: ExposureDecision
    ) -> None:
        """Persist the Exposure Ceiling decision derived for one run."""
        exposure_records.record_exposure_decision(self._database, run_id, decision)

    def record_ftd_history(self, run_id: UUID, snapshot: FtdSnapshot) -> None:
        """Persist this run's deterministic SPY/QQQ FTD state changes."""
        ftd_records.record_ftd_history(self._database, run_id, snapshot)

    def start_run(self, run_date: date, mode: RunMode, config_hash: str) -> UUID:
        """Start a new run and record it as `running`.

        Args:
            run_date: Evaluation market date for this run.
            mode: Whether this run is `live` or `dry_run`.
            config_hash: Hash of the effective configuration, for audit.

        Returns:
            The newly generated `run_id`.
        """
        run_id = uuid4()
        with self._database.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, run_date, mode, config_hash, status, started_at
                ) VALUES (?, ?, ?, ?, 'running', now())
                """,
                [str(run_id), run_date, mode.value, config_hash],
            )
        return run_id

    def complete_run(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        report_path: Path | None = None,
        error_summary: str | None = None,
    ) -> None:
        """Mark a run finished.

        Args:
            run_id: The run to complete.
            status: Final run status.
            report_path: Path to the generated report, if any.
            error_summary: Short failure summary, if the run failed.
        """
        with self._database.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = now(), report_path = ?, error_summary = ?
                WHERE run_id = ?
                """,
                [
                    status.value,
                    str(report_path) if report_path is not None else None,
                    error_summary,
                    str(run_id),
                ],
            )

    def mark_stale_running_runs(self, cutoff: datetime, new_run_id: UUID) -> list[UUID]:
        """Mark abandoned `status='running'` runs `failed` (NFR-03 stuck-run detection).

        A run that crashed or was killed mid-execution (process kill,
        machine sleep, ...) never reaches `complete_run()` and would
        otherwise sit in `status='running'` forever. Any such row with
        `started_at < cutoff` is presumed abandoned and marked `failed` in
        one all-or-nothing transaction, so a mid-batch failure never leaves
        some rows marked stale and others silently untouched.

        Args:
            cutoff: Runs started strictly before this instant are stale
                (typically `deps.clock.now()` minus the NFR-03 timeout
                budget).
            new_run_id: The run performing this check, recorded in each
                stale run's `error_summary` for audit. This run is always
                excluded from marking, so a caller whose injected clock
                disagrees with the database wall clock can never mark
                itself stale.

        Returns:
            The `run_id`s marked stale, oldest `started_at` first.
        """
        with self._database.connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                stale_rows = conn.execute(
                    "SELECT run_id FROM runs WHERE status = 'running' "
                    "AND started_at < ? AND run_id != ? ORDER BY started_at",
                    [cutoff, str(new_run_id)],
                ).fetchall()
                for (run_id,) in stale_rows:
                    conn.execute(
                        """
                        UPDATE runs
                        SET status = 'failed', completed_at = now(),
                            error_summary = ?
                        WHERE run_id = ?
                        """,
                        [f"marked stale by run {new_run_id}", run_id],
                    )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        return [run_id for (run_id,) in stale_rows]

    def record_run_step(
        self,
        run_id: UUID,
        step: str,
        status: StepStatus,
        detail: str | None,
        duration_s: float,
    ) -> None:
        """Upsert one step's outcome for a run.

        Args:
            run_id: The run this step belongs to.
            step: Step identifier (e.g. `"1_prices"`).
            status: Step outcome.
            detail: Free-form detail (e.g. a degraded-mode reason).
            duration_s: Step duration in seconds.
        """
        with self._database.connect() as conn:
            conn.execute(
                """
                INSERT INTO run_steps (run_id, step, status, detail, duration_s)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (run_id, step) DO UPDATE SET
                    status = EXCLUDED.status,
                    detail = EXCLUDED.detail,
                    duration_s = EXCLUDED.duration_s
                """,
                [str(run_id), step, status.value, detail, duration_s],
            )

    def upsert_position(self, position: Position) -> None:
        """Insert or update a position, keyed by `position_id`.

        Args:
            position: The position to persist.
        """
        with self._database.connect() as conn:
            conn.execute(
                """
                INSERT INTO positions (
                    position_id, symbol, is_paper, entry_date, entry_price,
                    shares, stop_price, status, close_date, close_price,
                    exit_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
                ON CONFLICT (position_id) DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    is_paper = EXCLUDED.is_paper,
                    entry_date = EXCLUDED.entry_date,
                    entry_price = EXCLUDED.entry_price,
                    shares = EXCLUDED.shares,
                    stop_price = EXCLUDED.stop_price,
                    status = EXCLUDED.status,
                    close_date = EXCLUDED.close_date,
                    close_price = EXCLUDED.close_price,
                    exit_reason = EXCLUDED.exit_reason
                """,
                [
                    str(position.position_id),
                    position.symbol,
                    position.is_paper,
                    position.entry_date,
                    position.entry_price,
                    position.shares,
                    position.stop_price,
                    position.status,
                    position.close_date,
                    position.close_price,
                    position.exit_reason,
                ],
            )

    _POSITION_COLUMNS = (
        "position_id, symbol, is_paper, entry_date, entry_price, "
        "shares, stop_price, status, close_date, close_price, exit_reason"
    )

    @staticmethod
    def _position_from_row(row: tuple[Any, ...]) -> Position:  # Any: untyped DuckDB row
        return Position(
            position_id=row[0],
            symbol=row[1],
            is_paper=row[2],
            entry_date=row[3],
            entry_price=row[4],
            shares=row[5],
            stop_price=row[6],
            status=row[7],
            close_date=row[8],
            close_price=row[9],
            exit_reason=row[10],
        )

    def get_open_positions(self, is_paper: bool = True) -> list[Position]:
        """Return open positions matching `is_paper`.

        Args:
            is_paper: Whether to return paper or live positions.

        Returns:
            Open positions, unordered.
        """
        with self._database.connect() as conn:
            rows = conn.execute(
                f"SELECT {self._POSITION_COLUMNS} "  # noqa: S608 - constant column list
                "FROM positions WHERE status = 'open' AND is_paper = ?",
                [is_paper],
            ).fetchall()
        return [self._position_from_row(row) for row in rows]

    def get_position(self, position_id: UUID) -> Position | None:
        """Return one position by ID, regardless of status.

        Args:
            position_id: The position to look up.

        Returns:
            The matching position, or `None` if it doesn't exist.
        """
        with self._database.connect() as conn:
            row = conn.execute(
                f"SELECT {self._POSITION_COLUMNS} "  # noqa: S608 - constant column list
                "FROM positions WHERE position_id = ?",
                [str(position_id)],
            ).fetchone()
        return None if row is None else self._position_from_row(row)

    def get_closed_positions(
        self, is_paper: bool = True, as_of: date | None = None
    ) -> list[Position]:
        """Return closed positions matching `is_paper`.

        Args:
            is_paper: Whether to return paper or live positions.
            as_of: Optional point-in-time cutoff; when given, only positions
                with `close_date <= as_of` are returned (inclusive), so a
                position closed after `as_of` never leaks into a summary
                computed for that date. `None` returns every closed position
                regardless of `close_date`.

        Returns:
            Closed positions, unordered.
        """
        query = (
            f"SELECT {self._POSITION_COLUMNS} "  # noqa: S608 - constant column list
            "FROM positions WHERE status = 'closed' AND is_paper = ?"
        )
        params: list[Any] = [is_paper]
        if as_of is not None:
            query += " AND close_date <= ?"
            params.append(as_of)
        with self._database.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._position_from_row(row) for row in rows]

    def get_closed_positions_with_strategy(
        self, is_paper: bool = True, as_of: date | None = None
    ) -> list[tuple[Position, str | None]]:
        """Return closed positions paired with their originating strategy_key.

        A closed `Position` doesn't carry `strategy_key` directly — it lives
        on `trades_journal`, linked via `position_id` (P1-06, REQ-007's
        by-strategy breakdown).

        Args:
            is_paper: Whether to return paper or live positions.
            as_of: Optional point-in-time cutoff; same `close_date <= as_of`
                (inclusive) semantics as `get_closed_positions`.

        Returns:
            `(position, strategy_key)` pairs, unordered. `strategy_key` is
            `None` when the position was never linked to a `trades_journal`
            row (e.g. closed without ever recording a decision). Because
            `trades_journal.position_id` is not itself uniquely constrained
            (`UNIQUE (run_id, symbol, strategy_key)` is the real key), more
            than one row could in principle reference the same
            `position_id`; this picks the earliest-recorded row (by
            `created_at`, tie-broken by `strategy_key`) deterministically —
            a tie-break, not a new business rule.
        """
        qualified_columns = ", ".join(
            f"p.{column.strip()}" for column in self._POSITION_COLUMNS.split(",")
        )
        query = f"""
            SELECT {qualified_columns}, tj.strategy_key
            FROM positions p
            LEFT JOIN (
                SELECT position_id, strategy_key,
                       ROW_NUMBER() OVER (
                           PARTITION BY position_id
                           ORDER BY created_at ASC, strategy_key ASC
                       ) AS rn
                FROM trades_journal
                WHERE position_id IS NOT NULL
            ) tj ON tj.position_id = p.position_id AND tj.rn = 1
            WHERE p.status = 'closed' AND p.is_paper = ?
        """  # noqa: S608 - constant column list, no user input interpolated
        params: list[Any] = [is_paper]
        if as_of is not None:
            query += " AND p.close_date <= ?"
            params.append(as_of)
        with self._database.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [(self._position_from_row(row[:-1]), row[-1]) for row in rows]

    def record_trade_decision(self, record: TradeDecisionRecord) -> None:
        """Upsert a paper-trading decision, keyed by `(run_id, symbol, strategy_key)`.

        Args:
            record: The decision to persist.
        """
        paper_records.record_trade_decision(self._database, record)

    def get_decision_history(
        self, symbol: str, strategy_key: str, before_date: date, limit: int
    ) -> list[paper_records.DecisionHistoryEntry]:
        """Return bounded prior live decisions for point-in-time LLM context."""
        return paper_records.get_decision_history(
            self._database, symbol, strategy_key, before_date, limit
        )

    def get_candidate_strategy_keys(self, run_id: UUID, symbol: str) -> tuple[str, ...]:
        """Return strategies that produced a candidate in one run."""
        return paper_records.get_candidate_strategy_keys(self._database, run_id, symbol)

    def get_trade_decisions(
        self, run_id: UUID
    ) -> list[paper_records.TradeDecisionRecord]:
        """Return all human decisions recorded for one run."""
        return paper_records.get_trade_decisions(self._database, run_id)

    def get_run_report_path(self, run_id: UUID) -> Path | None:
        """Return the generated artifact path associated with a run."""
        return paper_records.get_run_report_path(self._database, run_id)

    def get_latest_run_report_path(self) -> Path | None:
        """Return the newest completed generated artifact path."""
        return paper_records.get_latest_run_report_path(self._database)

    def record_universe_membership(
        self, snapshot_date: date, members: Sequence[UniverseMember]
    ) -> None:
        """Persist a universe snapshot, satisfying `universe.UniverseStateStore`.

        Args:
            snapshot_date: The as-of date this snapshot represents.
            members: Universe membership to persist.
        """
        with self._database.connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(
                    "DELETE FROM universe_membership WHERE snapshot_date = ?",
                    [snapshot_date],
                )
                for member in members:
                    conn.execute(
                        """
                        INSERT INTO universe_membership (
                            snapshot_date, symbol, source_symbol, company_name,
                            gics_sector, source
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            snapshot_date,
                            member.symbol,
                            member.source_symbol,
                            member.company_name,
                            member.gics_sector,
                            _UNIVERSE_SOURCE,
                        ],
                    )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def get_latest_universe_membership(
        self, as_of: date | None = None
    ) -> tuple[date, tuple[UniverseMember, ...]] | None:
        """Return the latest universe snapshot not after `as_of`, if any.

        Args:
            as_of: Optional point-in-time cutoff. `None` returns the newest snapshot.

        Returns:
            `(snapshot_date, members)` for the latest snapshot, or `None`.
        """
        with self._database.connect() as conn:
            if as_of is None:
                latest = conn.execute(
                    "SELECT max(snapshot_date) FROM universe_membership"
                ).fetchone()
            else:
                latest = conn.execute(
                    "SELECT max(snapshot_date) FROM universe_membership "
                    "WHERE snapshot_date <= ?",
                    [as_of],
                ).fetchone()
            if latest is None or latest[0] is None:
                return None
            snapshot_date = latest[0]

            rows = conn.execute(
                """
                SELECT symbol, company_name, gics_sector, source_symbol
                FROM universe_membership
                WHERE snapshot_date = ?
                ORDER BY symbol
                """,
                [snapshot_date],
            ).fetchall()

        members = tuple(
            UniverseMember(
                symbol=row[0],
                company_name=row[1],
                gics_sector=row[2],
                source_symbol=row[3],
            )
            for row in rows
        )
        return (snapshot_date, members)

    def record_signals(
        self, signals: Sequence[SignalHit], run_date: date, strategy_key: str
    ) -> None:
        """Upsert signal hits by their business natural key.

        Args:
            signals: Signal hits to record.
            run_date: Evaluation market date the signals were computed for.
            strategy_key: Which `strategies.yaml` entry produced them.
        """
        audit_records.record_signals(self._database, signals, run_date, strategy_key)

    def record_candidates(
        self, candidates: Sequence[Candidate], run_id: UUID, strategy_key: str
    ) -> None:
        """Record one run's ranked candidates, keyed by `(run_id, symbol, strategy_key)`.

        Args:
            candidates: Ranked candidates to record.
            run_id: The run these candidates belong to.
            strategy_key: Which `strategies.yaml` entry produced them.
        """
        audit_records.record_candidates(
            self._database, candidates, run_id, strategy_key
        )

    def record_screening_results(
        self,
        candidates: Sequence[Candidate],
        rejections: Sequence[RejectionRecord],
        meta: ScreeningRunMeta,
    ) -> None:
        """Record one run's candidates and rejections in one transaction.

        REQ-004/REQ-020: both tables commit or roll back together.

        Args:
            candidates: Ranked candidates to record.
            rejections: Classified rejection records to record.
            meta: `(run_id, strategy_key, as_of)` shared by every row.
        """
        audit_records.record_screening_results(
            self._database, candidates, rejections, meta
        )

    def record_risk_assessments(
        self, assessments: Sequence[RiskAssessment], run_id: UUID
    ) -> None:
        """Record one run's risk assessments, keyed by `(run_id, symbol)`.

        Args:
            assessments: Risk assessments to record.
            run_id: The run these assessments belong to.
        """
        audit_records.record_risk_assessments(self._database, assessments, run_id)

    def record_signal_outcomes(self, outcomes: Sequence[SignalOutcomeRecord]) -> None:
        """Upsert one horizon's postmortem outcomes, keyed by `(run_id, symbol, horizon_days)`.

        Args:
            outcomes: Classified forward-return outcomes to persist (P2-11).
        """
        audit_records.record_signal_outcomes(self._database, outcomes)

    def record_llm_call(self, call: LLMCallRecord) -> None:
        """Append one LLM call's audit record.

        Args:
            call: The call to record.
        """
        llm_records.record_llm_call(self._database, call)

    def get_cached_llm_response(
        self, model: str, prompt_hash: str, schema_version: int
    ) -> str | None:
        """Return the most recent successful response for this natural key.

        Args:
            model: Model ID the original call used.
            prompt_hash: Hash of the original prompt text.
            schema_version: Schema version the original call used.

        Returns:
            The cached `response_json`, or `None` if no successful call matches.
        """
        return llm_records.get_cached_response(
            self._database, model, prompt_hash, schema_version
        )

    def record_text_items(self, items: Sequence[TextItem]) -> None:
        """Persist collected text items, upserted by `source_id`.

        Args:
            items: Text items collected this run (news, filings, calendar).
        """
        text_records.record_text_items(self._database, items)

    def get_source_urls(self, source_ids: Sequence[str]) -> dict[str, str]:
        """Resolve known `source_ids` to their `source_url`.

        Args:
            source_ids: Source IDs to resolve (e.g. an LLM fact's `source_ids`).

        Returns:
            A mapping for every `source_id` with a recorded text item.
        """
        return text_records.get_source_urls(self._database, source_ids)

    def get_monthly_llm_cost(self, as_of: date) -> float:
        """Return realized LLM cost for `as_of`'s calendar month.

        Args:
            as_of: Any date within the month to total.

        Returns:
            Total realized cost in USD for that calendar month.
        """
        return llm_records.get_monthly_cost(self._database, as_of)
