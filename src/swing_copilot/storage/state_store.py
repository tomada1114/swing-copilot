"""Execution state and audit log repository (NFR-05).

`StateStore` owns every DuckDB table that is not bars/fundamentals (those
belong to `MarketStore`): universe history, run/step tracking, signals,
candidates, risk assessments, positions, the paper-trading journal, text
items. `init_schema()` creates the full DDL from `docs/04_detailed_design.md`
4.2 up front — later checklist items (screening, risk, paper trading) add the
write methods for their own tables as they land, without re-touching schema
creation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import duckdb

from swing_copilot.exceptions import StorageSchemaError
from swing_copilot.storage import (
    audit_records,
    config_records,
    earnings_records,
    exposure_records,
    ftd_records,
    history_queries,
    regime_records,
    retro_records,
    text_records,
    tracking_records,
    verdict_records,
)
from swing_copilot.storage.json_guard import dumps_safe
from swing_copilot.storage.schema import (
    ALTER_SCHEMA_STATEMENTS,
    ANALYSIS_VIEW_STATEMENTS,
    DROP_SCHEMA_STATEMENTS,
    INIT_SCHEMA_STATEMENTS,
)
from swing_copilot.universe import UniverseMember

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date, datetime
    from pathlib import Path
    from uuid import UUID

    from swing_copilot.data.earnings import EarningsEvent
    from swing_copilot.models import RunMode, RunStatus, StepStatus
    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.regime.ftd import FtdSnapshot
    from swing_copilot.regime.gate import RegimeSnapshot
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import (
        Candidate,
        ScreeningResult,
        SignalHit,
    )
    from swing_copilot.storage.audit_records import (
        ScreeningRunMeta,
        SignalOutcomeRecord,
        UniverseForwardReturnRecord,
    )
    from swing_copilot.storage.config_records import ConfigVersionRecord
    from swing_copilot.storage.database import Database
    from swing_copilot.storage.retro_records import (
        FailureClassHistory,
        RetroNarrationRecord,
        RetroSessionRecord,
    )
    from swing_copilot.storage.tracking_records import (
        TrackableVerdict,
        VerdictPosition,
        VerdictPositionMark,
    )
    from swing_copilot.storage.verdict_records import (
        AnalysisSourceCoverageRecord,
        CollectedRunRecords,
        PriorVerdictRecord,
        VerdictCitationRow,
        VerdictOutcomeRecord,
        VerdictReasonBasisRow,
        VerdictRecord,
        VerdictRow,
        VerdictSourceRecord,
    )
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

    def validate_read_only_schema(self) -> None:
        """Verify the state table needed by read-only universe selection.

        Raises:
            StorageSchemaError: The database does not contain the state schema.
        """
        with self._database.connect() as conn:
            try:
                conn.execute("SELECT 1 FROM universe_membership LIMIT 0")
            except duckdb.CatalogException as exc:
                msg = "required table 'universe_membership' is missing"
                raise StorageSchemaError(msg) from exc

    def init_schema(self) -> None:
        """Create every table this store owns (idempotent).

        `DROP_SCHEMA_STATEMENTS` runs first, so a table this project no longer
        owns is gone before anything reads or writes -- that is the migration
        path for the 2026-08 real-trade record removal, and it is deliberately
        the first thing an existing database sees on open.

        Then `ALTER_SCHEMA_STATEMENTS` so an existing database from before an
        additive column change (e.g. P1-03's `risk_assessments` columns) picks
        them up, then `ANALYSIS_VIEW_STATEMENTS` (CREATE OR REPLACE, so edited
        view definitions self-migrate); every statement set is safe to re-run.

        Issue #192's `verdict_reasons` backfill runs last, after the tables
        it reads and writes both exist. It is the one migration step that
        cannot be a SQL string (it re-uses `verdict_records`' own
        `reasons_json` parsing instead of duplicating it as nested JSON SQL),
        and like every statement above it is idempotent.
        """
        with self._database.connect() as conn:
            for statement in DROP_SCHEMA_STATEMENTS:
                conn.execute(statement)
            for statement in INIT_SCHEMA_STATEMENTS:
                conn.execute(statement)
            for statement in ALTER_SCHEMA_STATEMENTS:
                conn.execute(statement)
            for statement in ANALYSIS_VIEW_STATEMENTS:
                conn.execute(statement)
            verdict_records.backfill_verdict_reasons(conn)

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

    def start_run(
        self,
        run_date: date,
        mode: RunMode,
        config_hash: str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> UUID:
        """Start a new run and record it as `running`.

        Args:
            run_date: Evaluation market date for this run.
            mode: Whether this run is `live` or `dry_run`.
            config_hash: Full SHA-256 fingerprint of the effective configuration.
            metadata: Canonical, non-secret run metadata needed to reconstruct
                the provider, data tier, universe snapshot, and schema/app
                versions. `None` exists only for legacy callers/tests.

        Returns:
            The newly generated `run_id`.
        """
        run_id = uuid4()
        with self._database.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, run_date, mode, config_hash, metadata_json, status, started_at
                ) VALUES (?, ?, ?, ?, ?, 'running', now())
                """,
                [
                    str(run_id),
                    run_date,
                    mode.value,
                    config_hash,
                    # Issue #192: every JSON column under `storage/` goes
                    # through the shared NaN/Inf guard (P1-04's contract),
                    # this one included -- run metadata is caller-supplied,
                    # so a non-finite value here would otherwise be the one
                    # write that reached DuckDB as a `NaN` literal.
                    dumps_safe(metadata if metadata is not None else {}),
                ],
            )
        return run_id

    # PLR0913: one field per `runs` column this write path lets a caller
    # specify explicitly (that is the whole point -- unlike `start_run`,
    # nothing here is fixed), all but the first four keyword-only.
    def insert_run(  # noqa: PLR0913
        self,
        run_id: UUID,
        run_date: date,
        mode: RunMode,
        config_hash: str,
        *,
        status: RunStatus,
        started_at: datetime,
        finished_at: datetime | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Register a fully specified `runs` row (historical seed / backfill).

        Unlike `start_run`, every lifecycle field is caller-supplied instead
        of `start_run`'s fixed `status='running'` / `started_at=now()` --
        `start_run` itself is unchanged, since altering how it derives those
        would change production behavior. This is a second, independent
        write path onto the same table, for callers that already know a
        run's full historical state: a test seeding a `runs` row at an
        arbitrary point in its lifecycle without reaching into the private
        DuckDB connection (Issue #395's motivation; #398 migrates the
        existing hand-written `INSERT INTO runs` tests onto this), and a
        future backfill that replays a run whose real timestamps are already
        known.

        Args:
            run_id: The run's identity.
            run_date: Evaluation market date for this run.
            mode: Whether this run is `live` or `dry_run`.
            config_hash: Full SHA-256 fingerprint of the effective configuration.
            status: The run's lifecycle status.
            started_at: When the run started.
            finished_at: When the run finished, or `None` for a run still
                `running`.
            metadata: Canonical, non-secret run metadata; `None` writes `{}`.
        """
        with self._database.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, run_date, mode, config_hash, metadata_json, status,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(run_id),
                    run_date,
                    mode.value,
                    config_hash,
                    dumps_safe(metadata if metadata is not None else {}),
                    status.value,
                    started_at,
                    finished_at,
                ],
            )

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
        with self._database.transaction() as conn:
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

    def upsert_earnings_calendar(self, events: Sequence[EarningsEvent]) -> None:
        """Correction-upsert earnings events as one transaction."""
        earnings_records.upsert_earnings_calendar(self._database, events)

    def get_earnings_event(self, symbol: str) -> EarningsEvent | None:
        """Return the latest corrected earnings event for one symbol."""
        return earnings_records.get_earnings_event(self._database, symbol)

    def get_run_started_at(self, run_id: UUID) -> datetime | None:
        """Return one run's `started_at`, or `None` if it has no `runs` row."""
        return history_queries.get_run_started_at(self._database, run_id)

    def get_successful_run(
        self, run_date: date
    ) -> history_queries.SuccessfulRun | None:
        """Return the most recently started `status='success'` run on `run_date`."""
        return history_queries.get_successful_run(self._database, run_date)

    def record_universe_membership(
        self, snapshot_date: date, members: Sequence[UniverseMember]
    ) -> None:
        """Persist a universe snapshot, satisfying `universe.UniverseStateStore`.

        Args:
            snapshot_date: The as-of date this snapshot represents.
            members: Universe membership to persist.
        """
        with self._database.transaction() as conn:
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
        self, result: ScreeningResult, meta: ScreeningRunMeta
    ) -> None:
        """Record one screening run's four outcomes in one transaction.

        REQ-004/REQ-020 plus Issues #188/#192: candidates, rejections,
        truncations, and signal hits commit or roll back together, because
        they are one screening run's outcomes.

        Args:
            result: The run's candidates, rejections, truncated tail, and
                signal hits. The truncated tail is retained down to
                `audit_records.PERSISTED_TRUNCATION_MULTIPLIER` pages.
            meta: `(run_id, strategy_key, as_of, candidate_limit)` shared by
                every row.
        """
        audit_records.record_screening_results(self._database, result, meta)

    def replace_universe_forward_returns(
        self,
        run_id: UUID,
        horizon_days: int,
        returns: Sequence[UniverseForwardReturnRecord],
    ) -> None:
        """Replace one historical run/horizon's control-group forward returns.

        Args:
            run_id: The historical run being evaluated.
            horizon_days: The horizon being replaced.
            returns: The complete recomputed set for that slice (Issue #188).
        """
        audit_records.replace_universe_forward_returns(
            self._database, run_id, horizon_days, returns
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

    def replace_signal_outcomes(
        self,
        run_id: UUID,
        horizon_days: int,
        outcomes: Sequence[SignalOutcomeRecord],
    ) -> None:
        """Replace one historical run/horizon's complete outcome set atomically."""
        audit_records.replace_signal_outcomes(
            self._database, run_id, horizon_days, outcomes
        )

    def replace_run_verdicts(
        self,
        run_id: UUID,
        verdicts: Sequence[VerdictRecord],
        sources: Sequence[VerdictSourceRecord],
        coverages: Sequence[AnalysisSourceCoverageRecord] = (),
    ) -> None:
        """Replace one run's complete verdict and citation set atomically (P8-30).

        Args:
            run_id: The run whose `verdicts`/`verdict_sources` rows are being
                replaced wholesale, so a corrected re-ingest drops symbols the
                new answer no longer covers.
            verdicts: The run's collected verdicts.
            sources: The `source_id`s those analyses cited.
            coverages: Code-owned filing coverage archived from the analysis
                input for later retrospective comparison.
        """
        verdict_records.replace_run_verdicts(
            self._database, run_id, verdicts, sources, coverages
        )

    def replace_collected_run(self, records: CollectedRunRecords) -> None:
        """Replace one collected run's rows and its document fingerprint (#209).

        Args:
            records: The run's verdicts, citations, coverage rows, and the
                digest of the `analysis_input.json`/`analysis_result.json`
                pair they were built from. All of it commits or none does, so
                a stored digest always describes rows that really exist.
        """
        verdict_records.replace_collected_run(self._database, records)

    def get_verdict_collection_digests(self) -> dict[UUID, str]:
        """Return the document fingerprint of every already-collected run."""
        return verdict_records.get_verdict_collection_digests(self._database)

    def get_recorded_outcome_slices(
        self, run_ids: Sequence[UUID]
    ) -> dict[tuple[UUID, int], frozenset[tuple[str, str]]]:
        """Return each recorded `(run, horizon)` slice's symbol/verdict set."""
        return verdict_records.get_recorded_outcome_slices(self._database, run_ids)

    def get_prior_verdicts(
        self, symbol: str, strategy_key: str, before_date: date, limit: int
    ) -> tuple[PriorVerdictRecord, ...]:
        """Return a symbol's earlier verdicts and matured outcomes (Issue #191).

        Args:
            symbol: The candidate's ticker.
            strategy_key: Only the same strategy's verdicts are comparable.
            before_date: Exclusive point-in-time cutoff on the verdict date.
            limit: Maximum verdicts to return, newest first.
        """
        return verdict_records.get_prior_verdicts(
            self._database, symbol, strategy_key, before_date, limit
        )

    def get_verdict_reason_bases_in_window(
        self, window_start: date, as_of: date
    ) -> tuple[VerdictReasonBasisRow, ...]:
        """Return the evidence kinds behind verdicts maturing in a window."""
        return verdict_records.get_verdict_reason_bases_in_window(
            self._database, window_start, as_of
        )

    def get_analysis_source_coverages(
        self, run_id: UUID, symbol: str
    ) -> tuple[AnalysisSourceCoverageRecord, ...]:
        """Return archived filing-input coverage for one run and symbol."""
        return verdict_records.get_analysis_source_coverages(
            self._database, run_id, symbol
        )

    def get_analysis_source_coverages_in_window(
        self, window_start: date, as_of: date
    ) -> tuple[AnalysisSourceCoverageRecord, ...]:
        """Return filing-input coverage for outcomes maturing in a window."""
        return verdict_records.get_analysis_source_coverages_in_window(
            self._database, window_start, as_of
        )

    def replace_verdict_outcomes(
        self,
        run_id: UUID,
        horizon_days: int,
        outcomes: Sequence[VerdictOutcomeRecord],
    ) -> None:
        """Replace one run/horizon's complete verdict classification set (P8-30).

        Args:
            run_id: The evaluated run.
            horizon_days: The evaluated horizon (5 or 20).
            outcomes: Classified forward-return outcomes to persist.
        """
        verdict_records.replace_verdict_outcomes(
            self._database, run_id, horizon_days, outcomes
        )

    def get_verdicts_in_window(
        self, window_start: date, as_of: date
    ) -> tuple[VerdictRow, ...]:
        """Return collected verdicts with a run date in `[window_start, as_of]`.

        Args:
            window_start: Inclusive earliest run date to evaluate.
            as_of: Inclusive latest run date to evaluate.

        Returns:
            Rows in a deterministic `(as_of, run_id, symbol)` order.
        """
        return verdict_records.get_verdicts_in_window(
            self._database, window_start, as_of
        )

    def get_verdict_outcomes_in_window(
        self, window_start: date, as_of: date
    ) -> tuple[VerdictOutcomeRecord, ...]:
        """Return classifications that matured in `[window_start, as_of]` (P8-31).

        Args:
            window_start: Inclusive earliest maturity date.
            as_of: Inclusive latest maturity date.

        Returns:
            Rows in a deterministic `(as_of, run_id, symbol, horizon_days)`
            order, the input of every retrospective aggregate.
        """
        return verdict_records.get_verdict_outcomes_in_window(
            self._database, window_start, as_of
        )

    def get_run_verdicts(self, run_id: UUID) -> tuple[VerdictRecord, ...]:
        """Return one run's collected verdicts with their reasons (P8-31).

        Args:
            run_id: The archived run to read back.
        """
        return verdict_records.get_run_verdicts(self._database, run_id)

    def get_verdict_citations_in_window(
        self, window_start: date, as_of: date
    ) -> tuple[VerdictCitationRow, ...]:
        """Return sources cited by verdicts matured in the window (P8-31).

        Args:
            window_start: Inclusive earliest maturity date.
            as_of: Inclusive latest maturity date.
        """
        return verdict_records.get_verdict_citations_in_window(
            self._database, window_start, as_of
        )

    def get_untracked_verdicts(
        self,
        as_of: date,
        recommendations: Sequence[str] = tracking_records.TRACKED_RECOMMENDATIONS,
    ) -> tuple[TrackableVerdict, ...]:
        """Return verdicts dated `<= as_of` that have no shadow position yet.

        Args:
            as_of: Inclusive point-in-time cutoff on the verdict's run date.
            recommendations: Verdict sides to open positions for.
        """
        return tracking_records.get_untracked_verdicts(
            self._database, as_of, recommendations
        )

    def get_untracked_truncations(self, as_of: date) -> tuple[TrackableVerdict, ...]:
        """Return truncated candidates dated `<= as_of` with no shadow position yet.

        Issue #188's tracking extension point: nothing in the daily loop
        calls this yet: see `tracking_records.get_untracked_truncations`.

        Args:
            as_of: Inclusive point-in-time cutoff on the truncation's `as_of`.
        """
        return tracking_records.get_untracked_truncations(self._database, as_of)

    def sync_verdict_position_recommendations(
        self,
    ) -> tuple[tuple[UUID, str, str], ...]:
        """Realign tracked positions with their verdict's current side.

        Returns:
            `(run_id, symbol, new_recommendation)` per realigned position.
        """
        return tracking_records.sync_verdict_position_recommendations(self._database)

    def delete_orphaned_verdict_positions(self) -> tuple[tuple[UUID, str], ...]:
        """Drop tracked positions whose verdict row no longer exists.

        Returns:
            The deleted positions' `(run_id, symbol)` identities.
        """
        return tracking_records.delete_orphaned_verdict_positions(self._database)

    def delete_verdict_positions(self, keys: Sequence[tuple[UUID, str]]) -> None:
        """Delete the named tracked positions and their marks in one transaction.

        Args:
            keys: `(run_id, symbol)` identities to remove. Empty is a no-op.
        """
        tracking_records.delete_verdict_positions(self._database, keys)

    def get_verdict_positions(
        self,
        status: str | None = None,
        recommendations: Sequence[str] | None = None,
    ) -> tuple[VerdictPosition, ...]:
        """Return tracked virtual positions, optionally narrowed.

        Args:
            status: `"open"`, `"closed"`, or `None` for both.
            recommendations: Verdict sides to include, or `None` for all.
        """
        return tracking_records.get_verdict_positions(
            self._database, status, recommendations
        )

    def get_verdict_position(self, run_id: UUID, symbol: str) -> VerdictPosition | None:
        """Return one tracked position, or `None` when it was never opened."""
        return tracking_records.get_verdict_position(self._database, run_id, symbol)

    def upsert_verdict_position(
        self,
        position: VerdictPosition,
        marks: Sequence[VerdictPositionMark] = (),
    ) -> None:
        """Persist one tracked position's advance and its marks atomically.

        Args:
            position: The position's state after the advance.
            marks: Marks produced by the same advance.
        """
        tracking_records.upsert_verdict_position(self._database, position, marks)

    def get_verdict_position_marks(
        self, run_id: UUID, symbol: str
    ) -> tuple[VerdictPositionMark, ...]:
        """Return one tracked position's daily marks in trading-date order."""
        return tracking_records.get_verdict_position_marks(
            self._database, run_id, symbol
        )

    def get_latest_verdict_position_marks(
        self,
    ) -> dict[tuple[UUID, str], VerdictPositionMark]:
        """Return each tracked position's most recent mark, keyed by identity."""
        return tracking_records.get_latest_verdict_position_marks(self._database)

    def get_earliest_verdict_position_marks(
        self,
    ) -> dict[tuple[UUID, str], VerdictPositionMark]:
        """Return each tracked position's first (entry-session) mark."""
        return tracking_records.get_earliest_verdict_position_marks(self._database)

    def get_verdict_reasons_json(self, run_id: UUID, symbol: str) -> str | None:
        """Return the raw `verdicts.reasons_json` behind a tracked position."""
        return tracking_records.get_verdict_reasons_json(self._database, run_id, symbol)

    def replace_retro_session(
        self,
        session: RetroSessionRecord,
        narrations: Sequence[RetroNarrationRecord],
    ) -> None:
        """Record one ingested retrospective and its verified narrations (#189).

        Args:
            session: The session row, correction-upserted by `retro_as_of`.
            narrations: That session's complete narration set; a re-ingest
                replaces the date's previous set rather than merging into it.
        """
        retro_records.replace_retro_session(self._database, session, narrations)

    def get_failure_class_history(
        self, as_of: date, session_limit: int
    ) -> FailureClassHistory:
        """Cross-tab the trailing retrospectives' `failure_class` counts (#189).

        Args:
            as_of: Point-in-time cutoff; only sessions ingested at or before
                it are counted.
            session_limit: How many trailing sessions to count.

        Returns:
            The counted sessions and one row per observed class.
        """
        return retro_records.get_failure_class_history(
            self._database, as_of, session_limit
        )

    def get_retro_narrations(
        self, retro_as_of: date
    ) -> tuple[RetroNarrationRecord, ...]:
        """Return one retrospective's persisted narrations (#189)."""
        return retro_records.get_retro_narrations(self._database, retro_as_of)

    def upsert_config_version(self, record: ConfigVersionRecord) -> None:
        """Record what a run's `config_hash` stood for (#189).

        Args:
            record: The observed configuration, keyed by `runs.config_hash`.
        """
        config_records.upsert_config_version(self._database, record)

    def get_config_versions(self) -> tuple[ConfigVersionRecord, ...]:
        """Return the whole configuration ledger, oldest first (#189)."""
        return config_records.get_config_versions(self._database)

    def get_run_config_hashes(self, as_of: date) -> dict[UUID, str]:
        """Map every run visible at `as_of` to its `config_hash` (#189)."""
        return config_records.get_run_config_hashes(self._database, as_of)

    def record_text_items(self, items: Sequence[TextItem]) -> None:
        """Persist collected text items, upserted by `source_id`.

        Args:
            items: Text items collected this run (news, filings, calendar).
        """
        text_records.record_text_items(self._database, items)

    def latest_filing_dates(
        self, symbols: Sequence[str], *, as_of: date
    ) -> dict[str, date]:
        """Read each symbol's latest collected filing date, visible at `as_of`.

        Args:
            symbols: Tickers to look up.
            as_of: Inclusive point-in-time cutoff on the filing date.

        Returns:
            `{symbol: latest visible filing date}`, omitting symbols with no
            collected filing.
        """
        return text_records.latest_filing_dates(self._database, symbols, as_of=as_of)

    def get_source_urls(self, source_ids: Sequence[str]) -> dict[str, str]:
        """Resolve known `source_ids` to their `source_url`.

        Args:
            source_ids: Source IDs to resolve (e.g. an analysis fact's IDs).

        Returns:
            A mapping for every `source_id` with a recorded text item.
        """
        return text_records.get_source_urls(self._database, source_ids)
