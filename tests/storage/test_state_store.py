"""Tests for StateStore: runs/run_steps/positions/universe history (NFR-05)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import uuid4

import duckdb
import pytest
from duckdb import ConstraintException

from swing_copilot.models import Position, RunMode, RunStatus, StepStatus
from swing_copilot.risk.checks import CorrelationWarning, RiskAssessment
from swing_copilot.screening.base import (
    Candidate,
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
    SignalHit,
)
from swing_copilot.storage.audit_records import ScreeningRunMeta
from swing_copilot.storage.database import Database
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.base import TextItem
from swing_copilot.universe import UniverseMember


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store


class TestInitSchema:
    def test_is_idempotent(self, state_store):
        state_store.init_schema()
        state_store.init_schema()

    def test_empty_positions_on_first_run(self, state_store):
        assert state_store.get_open_positions() == []


class TestRunLifecycle:
    def test_start_run_returns_unique_ids_for_same_run_date(self, state_store):
        first = state_store.start_run(date(2026, 7, 20), RunMode.DRY_RUN, "hash-a")
        second = state_store.start_run(date(2026, 7, 20), RunMode.DRY_RUN, "hash-a")

        assert first != second

    def test_complete_run_updates_status_and_report_path(self, state_store, tmp_path):
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.DRY_RUN, "hash-a")
        report_path = tmp_path / "reports" / "2026-07-20.md"

        state_store.complete_run(run_id, RunStatus.SUCCESS, report_path=report_path)

        with state_store._database.connect() as conn:  # noqa: SLF001 - verifying persisted state
            row = conn.execute(
                "SELECT status, report_path, completed_at FROM runs WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert row[0] == "success"
        assert row[1] == str(report_path)
        assert row[2] is not None

    def test_failed_run_can_be_recovered_by_a_new_run(self, state_store):
        failed_run = state_store.start_run(date(2026, 7, 20), RunMode.DRY_RUN, "hash-a")
        state_store.complete_run(failed_run, RunStatus.FAILED, error_summary="boom")

        retry_run = state_store.start_run(date(2026, 7, 20), RunMode.DRY_RUN, "hash-a")
        state_store.complete_run(retry_run, RunStatus.SUCCESS)

        assert retry_run != failed_run


class _FlakyConnection:
    """Wraps a real DuckDB connection; raises on the Nth `UPDATE runs` call."""

    def __init__(self, real_conn: duckdb.DuckDBPyConnection, fail_on_call: int):
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._update_calls = 0

    def execute(self, sql, parameters=None):
        if sql.lstrip().startswith("UPDATE runs"):
            self._update_calls += 1
            if self._update_calls == self._fail_on_call:
                msg = "simulated failure on a later UPDATE"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


class TestMarkStaleRunningRuns:
    @staticmethod
    def _backdate(state_store, run_id, started_at):
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE runs SET started_at = ? WHERE run_id = ?",
                [started_at, str(run_id)],
            )

    def test_never_marks_the_run_performing_the_check_itself(self, state_store):
        # A caller whose injected clock disagrees with the DB wall clock
        # (e.g. tests with a future FakeClock) must not self-mark stale.
        own_run = state_store.start_run(date(2026, 7, 19), RunMode.LIVE, "hash-a")
        self._backdate(state_store, own_run, datetime(2026, 7, 19, 8, tzinfo=UTC))

        marked = state_store.mark_stale_running_runs(
            datetime(2026, 7, 20, tzinfo=UTC), own_run
        )

        assert marked == []

    def test_marks_a_running_run_older_than_cutoff_as_failed(self, state_store):
        stale_run = state_store.start_run(date(2026, 7, 19), RunMode.LIVE, "hash-a")
        self._backdate(state_store, stale_run, datetime(2026, 7, 19, 8, tzinfo=UTC))
        new_run_id = uuid4()

        marked = state_store.mark_stale_running_runs(
            datetime(2026, 7, 20, tzinfo=UTC), new_run_id
        )

        assert marked == [stale_run]
        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, error_summary, completed_at FROM runs WHERE run_id = ?",
                [str(stale_run)],
            ).fetchone()
        assert row[0] == "failed"
        assert str(new_run_id) in row[1]
        assert row[2] is not None

    def test_leaves_a_run_started_exactly_at_cutoff_alone(self, state_store):
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "hash-a")
        cutoff = datetime(2026, 7, 20, 12, tzinfo=UTC)
        self._backdate(state_store, run_id, cutoff)

        marked = state_store.mark_stale_running_runs(cutoff, uuid4())

        assert marked == []
        with state_store._database.connect() as conn:  # noqa: SLF001
            status = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", [str(run_id)]
            ).fetchone()
        assert status == ("running",)

    def test_leaves_a_run_started_just_after_cutoff_alone(self, state_store):
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "hash-a")
        cutoff = datetime(2026, 7, 20, 12, tzinfo=UTC)
        self._backdate(state_store, run_id, cutoff + timedelta(seconds=1))

        marked = state_store.mark_stale_running_runs(cutoff, uuid4())

        assert marked == []

    def test_does_not_touch_an_already_completed_run(self, state_store):
        run_id = state_store.start_run(date(2026, 7, 19), RunMode.LIVE, "hash-a")
        self._backdate(state_store, run_id, datetime(2026, 7, 19, 8, tzinfo=UTC))
        state_store.complete_run(run_id, RunStatus.SUCCESS)

        marked = state_store.mark_stale_running_runs(
            datetime(2026, 7, 20, tzinfo=UTC), uuid4()
        )

        assert marked == []

    def test_marks_multiple_stale_runs_oldest_started_at_first(self, state_store):
        older = state_store.start_run(date(2026, 7, 18), RunMode.LIVE, "hash-a")
        newer = state_store.start_run(date(2026, 7, 19), RunMode.LIVE, "hash-a")
        self._backdate(state_store, older, datetime(2026, 7, 18, 8, tzinfo=UTC))
        self._backdate(state_store, newer, datetime(2026, 7, 19, 8, tzinfo=UTC))

        marked = state_store.mark_stale_running_runs(
            datetime(2026, 7, 20, tzinfo=UTC), uuid4()
        )

        assert marked == [older, newer]

    def test_rolls_back_entirely_when_a_later_update_fails(
        self, state_store, monkeypatch
    ):
        first = state_store.start_run(date(2026, 7, 18), RunMode.LIVE, "hash-a")
        second = state_store.start_run(date(2026, 7, 19), RunMode.LIVE, "hash-a")
        self._backdate(state_store, first, datetime(2026, 7, 18, 8, tzinfo=UTC))
        self._backdate(state_store, second, datetime(2026, 7, 19, 8, tzinfo=UTC))

        real_connect = state_store._database.connect  # noqa: SLF001
        monkeypatch.setattr(
            state_store._database,  # noqa: SLF001
            "connect",
            lambda: _FlakyConnection(real_connect(), fail_on_call=2),
        )

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.mark_stale_running_runs(
                datetime(2026, 7, 20, tzinfo=UTC), uuid4()
            )

        # Rolled back entirely: the first UPDATE succeeded before the second
        # one raised, but neither run's status was left changed.
        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT run_id, status FROM runs ORDER BY run_id"
            ).fetchall()
        statuses = {str(run_id): status for run_id, status in rows}
        assert statuses[str(first)] == "running"
        assert statuses[str(second)] == "running"


class TestRecordRunStep:
    def test_upserts_on_same_run_id_and_step(self, state_store):
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.DRY_RUN, "hash-a")

        state_store.record_run_step(
            run_id, "1_prices", StepStatus.FAILED, "timeout", 1.5
        )
        state_store.record_run_step(run_id, "1_prices", StepStatus.SUCCESS, None, 2.5)

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT status, detail, duration_s FROM run_steps "
                "WHERE run_id = ? AND step = ?",
                [str(run_id), "1_prices"],
            ).fetchall()
        assert rows == [("success", None, 2.5)]

    def test_two_runs_keep_independent_step_history(self, state_store):
        run_a = state_store.start_run(date(2026, 7, 20), RunMode.DRY_RUN, "hash-a")
        run_b = state_store.start_run(date(2026, 7, 20), RunMode.DRY_RUN, "hash-a")

        state_store.record_run_step(run_a, "1_prices", StepStatus.SUCCESS, None, 1.0)
        state_store.record_run_step(run_b, "1_prices", StepStatus.FAILED, "boom", 0.5)

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT run_id, status FROM run_steps ORDER BY run_id"
            ).fetchall()
        assert len(rows) == 2
        statuses = {str(run_id): status for run_id, status in rows}
        assert statuses[str(run_a)] == "success"
        assert statuses[str(run_b)] == "failed"


class TestOpenPositions:
    def test_returns_only_open_positions_matching_is_paper(self, state_store):
        open_paper = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=date(2026, 7, 15),
            entry_price=100.0,
            shares=10,
            status="open",
            stop_price=90.0,
        )
        closed_paper = Position(
            position_id=uuid4(),
            symbol="MSFT",
            is_paper=True,
            entry_date=date(2026, 7, 10),
            entry_price=200.0,
            shares=5,
            status="closed",
            close_date=date(2026, 7, 18),
            close_price=210.0,
        )
        open_live = Position(
            position_id=uuid4(),
            symbol="JPM",
            is_paper=False,
            entry_date=date(2026, 7, 12),
            entry_price=150.0,
            shares=3,
            status="open",
        )
        state_store.upsert_position(open_paper)
        state_store.upsert_position(closed_paper)
        state_store.upsert_position(open_live)

        result = state_store.get_open_positions(is_paper=True)

        assert [position.symbol for position in result] == ["AAPL"]


class TestUniverseMembership:
    def test_returns_none_when_nothing_recorded_yet(self, state_store):
        assert state_store.get_latest_universe_membership() is None

    def test_records_and_retrieves_latest_snapshot(self, state_store):
        members = [
            UniverseMember(
                symbol="AAPL",
                company_name="Apple Inc.",
                gics_sector="Information Technology",
                source_symbol="AAPL",
            )
        ]

        state_store.record_universe_membership(date(2026, 7, 20), members)
        result = state_store.get_latest_universe_membership()

        assert result == (date(2026, 7, 20), tuple(members))

    def test_second_snapshot_becomes_the_latest(self, state_store):
        first = [
            UniverseMember(
                symbol="AAPL",
                company_name="Apple Inc.",
                gics_sector="Information Technology",
                source_symbol="AAPL",
            )
        ]
        second = [
            UniverseMember(
                symbol="MSFT",
                company_name="Microsoft Corp.",
                gics_sector="Information Technology",
                source_symbol="MSFT",
            )
        ]

        state_store.record_universe_membership(date(2026, 7, 13), first)
        state_store.record_universe_membership(date(2026, 7, 20), second)

        assert state_store.get_latest_universe_membership() == (
            date(2026, 7, 20),
            tuple(second),
        )

    def test_as_of_returns_latest_snapshot_not_after_cutoff(self, state_store):
        first = [UniverseMember("AAPL", "Apple Inc.", "Technology", "AAPL")]
        future = [UniverseMember("MSFT", "Microsoft Corp.", "Technology", "MSFT")]
        state_store.record_universe_membership(date(2026, 7, 13), first)
        state_store.record_universe_membership(date(2026, 7, 20), future)

        assert state_store.get_latest_universe_membership(date(2026, 7, 15)) == (
            date(2026, 7, 13),
            tuple(first),
        )

    def test_rerecording_same_date_replaces_removed_members(self, state_store):
        snapshot_date = date(2026, 7, 20)
        first = [UniverseMember("AAPL", "Apple", "Technology", "AAPL")]
        corrected = [UniverseMember("MSFT", "Microsoft", "Technology", "MSFT")]
        state_store.record_universe_membership(snapshot_date, first)

        state_store.record_universe_membership(snapshot_date, corrected)

        assert state_store.get_latest_universe_membership() == (
            snapshot_date,
            tuple(corrected),
        )


class TestRecordSignals:
    def test_duplicate_natural_key_is_updated_for_corrected_input(self, state_store):
        run_date = date(2026, 7, 20)
        hit = SignalHit(
            symbol="AAPL",
            signal_name="trend_sma",
            direction="long",
            strength=1.0,
            metrics={"sma_long": 100.0},
        )
        updated_hit = SignalHit(
            symbol="AAPL",
            signal_name="trend_sma",
            direction="long",
            strength=1.0,
            metrics={"sma_long": 999.0},
        )

        state_store.record_signals([hit], run_date, "default")
        state_store.record_signals([updated_hit], run_date, "default")

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT metrics_json FROM signals WHERE symbol = 'AAPL'"
            ).fetchall()
        assert len(rows) == 1
        assert "999.0" in rows[0][0]


class TestRecordCandidates:
    def test_records_one_row_per_candidate(self, state_store):
        run_id = uuid4()
        candidates = [
            Candidate(
                symbol="AAPL",
                as_of=date(2026, 7, 20),
                signal_names=("trend_sma",),
                metrics={"rsi14": 40.0},
                rank=1,
            )
        ]

        state_store.record_candidates(candidates, run_id, "default")

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol, rank FROM candidates WHERE run_id = ?", [str(run_id)]
            ).fetchall()
        assert rows == [("AAPL", 1)]

    def test_different_run_ids_do_not_collide(self, state_store):
        candidate = Candidate(
            symbol="AAPL", as_of=date(2026, 7, 20), signal_names=(), metrics={}, rank=1
        )
        run_a, run_b = uuid4(), uuid4()

        state_store.record_candidates([candidate], run_a, "default")
        state_store.record_candidates([candidate], run_b, "default")

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute("SELECT count(*) FROM candidates").fetchone()
        assert count == (2,)


class _FlakyRejectionConnection:
    """Wraps a real connection; raises on the Nth `INSERT INTO screening_rejections`.

    Mirrors `_FlakyConnection` above but targets the rejections table so the
    rollback test can inject a failure *after* at least one candidate row
    and one rejection row have already been inserted in the same
    transaction (REQ-004/REQ-020).
    """

    def __init__(self, real_conn: duckdb.DuckDBPyConnection, fail_on_call: int):
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._insert_calls = 0

    def execute(self, sql, parameters=None):
        if sql.lstrip().startswith("INSERT INTO screening_rejections"):
            self._insert_calls += 1
            if self._insert_calls == self._fail_on_call:
                msg = "simulated failure on a later rejection insert"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


class TestRecordScreeningResults:
    """P1-02: `screening_rejections` table and its atomic write (REQ-001..REQ-040)."""

    def test_records_one_row_per_candidate_and_rejection(self, state_store):
        # REQ-001: each stage/reason_code combination reaches the DB.
        run_id = uuid4()
        candidates = [
            Candidate(
                symbol="AAPL",
                as_of=date(2026, 7, 20),
                signal_names=("trend_sma",),
                metrics={"rsi14": 40.0},
                rank=1,
            )
        ]
        rejections = [
            RejectionRecord(
                symbol="LOWQ",
                stage=RejectionStage.DATA_QUALITY,
                reason_code=RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
                detail={"available_quarters": 1, "required_quarters": 4},
            ),
            RejectionRecord(
                symbol="NETINC",
                stage=RejectionStage.FUNDAMENTAL_FILTER,
                reason_code=RejectionReasonCode.FILTER_NEGATIVE_NET_INCOME,
                detail={"net_income": -500000.0, "threshold": 0},
            ),
            RejectionRecord(
                symbol="LOWVOL",
                stage=RejectionStage.FUNDAMENTAL_FILTER,
                reason_code=RejectionReasonCode.FILTER_LOW_LIQUIDITY,
                detail={"avg_volume": 100.0, "threshold": 1_000_000},
            ),
            RejectionRecord(
                symbol="NOTREND",
                stage=RejectionStage.TECHNICAL_SIGNAL,
                reason_code=RejectionReasonCode.SIGNAL_TREND_NOT_MET,
                detail={"close": 99.0, "sma_long": 101.0},
            ),
        ]

        state_store.record_screening_results(
            candidates,
            rejections,
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20)),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            candidate_rows = conn.execute(
                "SELECT symbol, rank FROM candidates WHERE run_id = ?", [str(run_id)]
            ).fetchall()
            rejection_rows = conn.execute(
                "SELECT symbol, stage, reason_code FROM screening_rejections "
                "WHERE run_id = ? ORDER BY symbol",
                [str(run_id)],
            ).fetchall()
        assert candidate_rows == [("AAPL", 1)]
        assert rejection_rows == [
            ("LOWQ", "data_quality", "DATA_INSUFFICIENT_HISTORY"),
            ("LOWVOL", "fundamental_filter", "FILTER_LOW_LIQUIDITY"),
            ("NETINC", "fundamental_filter", "FILTER_NEGATIVE_NET_INCOME"),
            ("NOTREND", "technical_signal", "SIGNAL_TREND_NOT_MET"),
        ]

    def test_detail_json_round_trips_observed_value_and_threshold(self, state_store):
        # REQ-003: detail JSON preserves the exact observed value/threshold.
        run_id = uuid4()
        rejection = RejectionRecord(
            symbol="XYZ",
            stage=RejectionStage.FUNDAMENTAL_FILTER,
            reason_code=RejectionReasonCode.FILTER_LOW_EQUITY_RATIO,
            detail={"equity_ratio": 0.24, "threshold": 0.30},
        )

        state_store.record_screening_results(
            [], [rejection], ScreeningRunMeta(run_id, "default", date(2026, 7, 20))
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT detail FROM screening_rejections WHERE run_id = ? AND symbol = ?",
                [str(run_id), "XYZ"],
            ).fetchone()
        assert json.loads(row[0]) == {"equity_ratio": 0.24, "threshold": 0.30}

    def test_invalid_reason_code_violates_check_constraint(self, state_store):
        # REQ-002: reason_code is limited to the closed enum at the schema
        # level, independent of application-layer validation.
        with (
            state_store._database.connect() as conn,  # noqa: SLF001
            pytest.raises(ConstraintException),
        ):
            conn.execute(
                """
                INSERT INTO screening_rejections (
                    run_id, symbol, stage, reason_code, detail, as_of
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    str(uuid4()),
                    "XYZ",
                    "fundamental_filter",
                    "NOT_A_REAL_REASON_CODE",
                    "{}",
                    date(2026, 7, 20),
                ],
            )

    def test_invalid_stage_violates_check_constraint(self, state_store):
        with (
            state_store._database.connect() as conn,  # noqa: SLF001
            pytest.raises(ConstraintException),
        ):
            conn.execute(
                """
                INSERT INTO screening_rejections (
                    run_id, symbol, stage, reason_code, detail, as_of
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    str(uuid4()),
                    "XYZ",
                    "not_a_real_stage",
                    "DATA_INSUFFICIENT_HISTORY",
                    "{}",
                    date(2026, 7, 20),
                ],
            )

    def test_rolls_back_both_tables_when_a_later_rejection_insert_fails(
        self, state_store, monkeypatch
    ):
        # REQ-004/REQ-020, Example 3: candidates=5 rejections=3, fails on the
        # 3rd rejection insert (after >=1 candidate and >=1 rejection already
        # succeeded in the same transaction) -> zero rows from this run.
        run_id = uuid4()
        candidates = [
            Candidate(
                symbol=f"C{i}",
                as_of=date(2026, 7, 20),
                signal_names=(),
                metrics={},
                rank=i,
            )
            for i in range(1, 6)
        ]
        rejections = [
            RejectionRecord(
                symbol=f"R{i}",
                stage=RejectionStage.DATA_QUALITY,
                reason_code=RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
                detail={"available_quarters": 0, "required_quarters": 4},
            )
            for i in range(1, 4)
        ]

        real_connect = state_store._database.connect  # noqa: SLF001
        monkeypatch.setattr(
            state_store._database,  # noqa: SLF001
            "connect",
            lambda: _FlakyRejectionConnection(real_connect(), fail_on_call=3),
        )

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.record_screening_results(
                candidates,
                rejections,
                ScreeningRunMeta(run_id, "default", date(2026, 7, 20)),
            )

        with state_store._database.connect() as conn:  # noqa: SLF001
            candidate_count = conn.execute(
                "SELECT count(*) FROM candidates WHERE run_id = ?", [str(run_id)]
            ).fetchone()
            rejection_count = conn.execute(
                "SELECT count(*) FROM screening_rejections WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert candidate_count == (0,)
        assert rejection_count == (0,)

    def test_rerun_after_failure_succeeds(self, state_store, monkeypatch):
        # A rerun with a fresh run_id after a rolled-back failure must
        # succeed cleanly (no partial state left behind to conflict with).
        run_id = uuid4()
        candidates = [
            Candidate(
                symbol="AAPL",
                as_of=date(2026, 7, 20),
                signal_names=(),
                metrics={},
                rank=1,
            )
        ]
        rejections = [
            RejectionRecord(
                symbol="R1",
                stage=RejectionStage.DATA_QUALITY,
                reason_code=RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
                detail={"available_quarters": 0, "required_quarters": 4},
            ),
            RejectionRecord(
                symbol="R2",
                stage=RejectionStage.DATA_QUALITY,
                reason_code=RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
                detail={"available_quarters": 0, "required_quarters": 4},
            ),
        ]
        real_connect = state_store._database.connect  # noqa: SLF001
        monkeypatch.setattr(
            state_store._database,  # noqa: SLF001
            "connect",
            lambda: _FlakyRejectionConnection(real_connect(), fail_on_call=2),
        )
        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.record_screening_results(
                candidates,
                rejections,
                ScreeningRunMeta(run_id, "default", date(2026, 7, 20)),
            )

        monkeypatch.setattr(state_store._database, "connect", real_connect)  # noqa: SLF001
        retry_run_id = uuid4()
        state_store.record_screening_results(
            candidates,
            rejections,
            ScreeningRunMeta(retry_run_id, "default", date(2026, 7, 20)),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            counts = conn.execute(
                "SELECT "
                "(SELECT count(*) FROM candidates WHERE run_id = ?), "
                "(SELECT count(*) FROM screening_rejections WHERE run_id = ?)",
                [str(retry_run_id), str(retry_run_id)],
            ).fetchone()
        assert counts == (1, 2)

    def test_all_pass_fixture_writes_zero_rejection_rows(self, state_store):
        # REQ-010 boundary at the storage level: an empty rejections list is
        # a legitimate, error-free write.
        run_id = uuid4()
        candidates = [
            Candidate(
                symbol="AAPL",
                as_of=date(2026, 7, 20),
                signal_names=(),
                metrics={},
                rank=1,
            )
        ]

        state_store.record_screening_results(
            candidates, [], ScreeningRunMeta(run_id, "default", date(2026, 7, 20))
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute(
                "SELECT count(*) FROM screening_rejections WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert count == (0,)


class TestRecordRiskAssessments:
    def test_records_status_and_warnings(self, state_store):
        run_id = uuid4()
        assessment = RiskAssessment(
            symbol="AAPL",
            status="approved",
            max_shares=10,
            entry_price=100.0,
            stop_price=95.0,
            reasons=(),
            warnings=(CorrelationWarning("MSFT", 0.8, "high_correlation"),),
        )

        state_store.record_risk_assessments([assessment], run_id)

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT status, warnings_json FROM risk_assessments WHERE run_id = ?",
                [str(run_id)],
            ).fetchall()
        assert rows[0][0] == "approved"
        assert "MSFT" in rows[0][1]


def _text_item(source_id: str, source_url: str) -> TextItem:
    return TextItem(
        source_id=source_id,
        symbol="AAPL",
        source_type="news",
        published_at=datetime(2026, 7, 19, tzinfo=UTC),
        title="Example headline",
        source_url=source_url,
        content_text="Example body text.",
        fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


class TestTextItems:
    def test_record_then_resolve_source_urls(self, state_store):
        state_store.record_text_items(
            [
                _text_item("finnhub-1", "https://example.com/1"),
                _text_item("finnhub-2", "https://example.com/2"),
            ]
        )

        result = state_store.get_source_urls(["finnhub-1", "finnhub-2"])

        assert result == {
            "finnhub-1": "https://example.com/1",
            "finnhub-2": "https://example.com/2",
        }

    def test_unknown_source_ids_are_silently_omitted(self, state_store):
        state_store.record_text_items(
            [_text_item("finnhub-1", "https://example.com/1")]
        )

        result = state_store.get_source_urls(["finnhub-1", "unknown-id"])

        assert result == {"finnhub-1": "https://example.com/1"}

    def test_empty_source_ids_returns_empty_mapping(self, state_store):
        assert state_store.get_source_urls([]) == {}

    def test_rerecording_same_source_id_corrects_the_row(self, state_store):
        state_store.record_text_items(
            [_text_item("finnhub-1", "https://example.com/old")]
        )
        state_store.record_text_items(
            [_text_item("finnhub-1", "https://example.com/new")]
        )

        result = state_store.get_source_urls(["finnhub-1"])

        assert result == {"finnhub-1": "https://example.com/new"}

    def test_batch_rolls_back_entirely_when_a_later_item_is_invalid(self, state_store):
        valid = _text_item("finnhub-1", "https://example.com/1")
        invalid = _text_item("finnhub-2", "https://example.com/2")
        # content_text is NOT NULL; force a constraint violation on the
        # second row so at least one row would have succeeded in isolation.
        invalid = TextItem(
            source_id=invalid.source_id,
            symbol=invalid.symbol,
            source_type=invalid.source_type,
            published_at=invalid.published_at,
            title=invalid.title,
            source_url=invalid.source_url,
            content_text=cast("str", None),
            fetched_at=invalid.fetched_at,
        )

        with pytest.raises(ConstraintException):
            state_store.record_text_items([valid, invalid])

        assert state_store.get_source_urls(["finnhub-1", "finnhub-2"]) == {}
