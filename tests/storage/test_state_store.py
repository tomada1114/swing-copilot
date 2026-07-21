"""Tests for StateStore: runs/run_steps/positions/universe history (NFR-05)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from swing_copilot.models import Position, RunMode, RunStatus, StepStatus
from swing_copilot.storage.database import Database
from swing_copilot.storage.state_store import StateStore
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
        report_path = tmp_path / "reports" / "2026-07-20.html"

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
