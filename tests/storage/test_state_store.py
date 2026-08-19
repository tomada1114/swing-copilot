"""Tests for StateStore: runs/run_steps/positions/universe history (NFR-05)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

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
    ScreeningResult,
    SignalHit,
    TruncatedCandidate,
)
from swing_copilot.storage.audit_records import (
    ScreeningRunMeta,
    SignalOutcomeRecord,
    UniverseForwardReturnRecord,
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.base import EXHIBIT_TRUNCATION_MARKER, TextItem
from swing_copilot.universe import UniverseMember


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store


_PRE_P1_03_RISK_ASSESSMENTS_TABLE = """
    CREATE TABLE IF NOT EXISTS risk_assessments (
        run_id          UUID NOT NULL,
        symbol          VARCHAR NOT NULL,
        status          VARCHAR NOT NULL
            CHECK (status IN ('approved','rejected','not_calculable')),
        max_shares      BIGINT,
        entry_price     DOUBLE,
        stop_price      DOUBLE,
        reasons_json    JSON NOT NULL,
        warnings_json   JSON NOT NULL,
        PRIMARY KEY (run_id, symbol)
    )
"""

_PRE_P1_06_POSITIONS_TABLE = """
    CREATE TABLE IF NOT EXISTS positions (
        position_id   UUID PRIMARY KEY,
        symbol        VARCHAR NOT NULL,
        is_paper      BOOLEAN NOT NULL DEFAULT 1,
        entry_date    DATE NOT NULL,
        entry_price   DOUBLE NOT NULL,
        shares        BIGINT NOT NULL,
        stop_price    DOUBLE,
        status        VARCHAR NOT NULL CHECK(status IN ('open','closed')),
        close_date    DATE,
        close_price   DOUBLE,
        created_at    TIMESTAMPTZ NOT NULL
    )
"""

_PRE_NO_TRADE_VERDICT_POSITIONS_TABLE = """
    CREATE TABLE IF NOT EXISTS verdict_positions (
        run_id              UUID NOT NULL,
        symbol              VARCHAR NOT NULL,
        strategy_key        VARCHAR NOT NULL,
        entry_date          DATE NOT NULL,
        entry_price         DOUBLE NOT NULL,
        stop_price          DOUBLE,
        days_held           INTEGER NOT NULL,
        status              VARCHAR NOT NULL CHECK (status IN ('open', 'closed')),
        exit_date           DATE,
        exit_price          DOUBLE,
        exit_reason         VARCHAR
            CHECK (exit_reason IN ('stop', 'max_hold', 'manual')),
        realized_return_pct DOUBLE,
        last_marked_date    DATE,
        PRIMARY KEY (run_id, symbol)
    )
"""

_PRE_P8_123_TEXT_ITEMS_TABLE = """
    CREATE TABLE IF NOT EXISTS text_items (
        source_id      VARCHAR PRIMARY KEY,
        symbol         VARCHAR,
        source_type    VARCHAR NOT NULL,
        published_at   TIMESTAMPTZ NOT NULL,
        title          VARCHAR,
        source_url     VARCHAR NOT NULL,
        content_text   VARCHAR NOT NULL,
        fetched_at     TIMESTAMPTZ NOT NULL
    )
"""

_PRE_EXHIBIT_TRUNCATED_COVERAGE_TABLE = """
    CREATE TABLE IF NOT EXISTS analysis_source_coverage (
        run_id          UUID NOT NULL,
        symbol          VARCHAR NOT NULL,
        source_id       VARCHAR NOT NULL,
        original_chars  INTEGER NOT NULL,
        exported_chars  INTEGER NOT NULL,
        is_truncated    BOOLEAN NOT NULL,
        selection_mode  VARCHAR NOT NULL,
        sections_json   JSON NOT NULL,
        PRIMARY KEY (run_id, symbol, source_id)
    )
"""

_PRE_I57_RUNS_TABLE = """
    CREATE TABLE IF NOT EXISTS runs (
        run_id          UUID PRIMARY KEY,
        run_date        DATE NOT NULL,
        mode            VARCHAR NOT NULL,
        config_hash     VARCHAR NOT NULL,
        status          VARCHAR NOT NULL,
        started_at      TIMESTAMPTZ NOT NULL,
        completed_at    TIMESTAMPTZ,
        report_path     VARCHAR,
        error_summary   VARCHAR
    )
"""


class TestInitSchema:
    def test_is_idempotent(self, state_store):
        state_store.init_schema()
        state_store.init_schema()

    def test_empty_positions_on_first_run(self, state_store):
        assert state_store.get_open_positions() == []

    def test_init_schema_upgrades_a_pre_p1_03_risk_assessments_table(self, tmp_path):
        # Simulate a database created before P1-03: risk_assessments exists
        # with the old 8-column shape (no sizing-breakdown columns).
        database = Database(tmp_path / "pre_p1_03.duckdb")
        with database.connect() as conn:
            conn.execute(_PRE_P1_03_RISK_ASSESSMENTS_TABLE)

        store = StateStore(database)
        store.init_schema()  # Must not raise against the old table shape.

        run_id = uuid4()
        with database.connect() as conn:
            conn.execute(
                """
                INSERT INTO risk_assessments (
                    run_id, symbol, status, max_shares, entry_price,
                    stop_price, reasons_json, warnings_json,
                    shares_by_risk, shares_by_position_cap,
                    binding_constraint, sizing_warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(run_id),
                    "AAPL",
                    "approved",
                    200,
                    50.0,
                    45.0,
                    "[]",
                    "[]",
                    200,
                    500,
                    "trade_risk",
                    "[]",
                ],
            )
            row = conn.execute(
                "SELECT binding_constraint, shares_by_risk, shares_by_position_cap "
                "FROM risk_assessments WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert row == ("trade_risk", 200, 500)

    def test_init_schema_adds_no_trade_to_a_pre_existing_verdict_positions_table(
        self, tmp_path
    ):
        # A database that already has `verdict_positions` from before this
        # change (no `no_trade` column at all): every row in it was
        # necessarily opened while `no_trade` verdicts were excluded, so the
        # migration both adds the column and backfills it to `FALSE` rather
        # than leaving it `NULL`.
        database = Database(tmp_path / "pre_no_trade.duckdb")
        run_id = uuid4()
        with database.connect() as conn:
            conn.execute(_PRE_NO_TRADE_VERDICT_POSITIONS_TABLE)
            conn.execute(
                """
                INSERT INTO verdict_positions (
                    run_id, symbol, strategy_key, entry_date, entry_price,
                    stop_price, days_held, status, last_marked_date
                ) VALUES (?, 'AAPL', 'default', ?, 100.0, 95.0, 0, 'open', ?)
                """,
                [str(run_id), date(2026, 7, 1), date(2026, 7, 1)],
            )

        store = StateStore(database)
        store.init_schema()  # Must not raise against the old table shape.

        position = store.get_verdict_position(run_id, "AAPL")
        assert position is not None
        assert position.no_trade is False

        # Idempotent: re-running must not disturb the backfilled row.
        store.init_schema()
        position_again = store.get_verdict_position(run_id, "AAPL")
        assert position_again is not None
        assert position_again.no_trade is False

    def test_init_schema_backfills_unknown_exit_reason_for_closed_rows_only(
        self, tmp_path
    ):
        # P1-06/REQ-002: a database created before this change has no
        # exit_reason column at all. Migration must backfill 'unknown' onto
        # already-closed rows, while a still-open row (which has no exit
        # reason at all) stays NULL rather than also getting stamped.
        database = Database(tmp_path / "pre_p1_06.duckdb")
        with database.connect() as conn:
            conn.execute(_PRE_P1_06_POSITIONS_TABLE)
            closed_id = str(uuid4())
            open_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO positions (
                    position_id, symbol, is_paper, entry_date, entry_price,
                    shares, stop_price, status, close_date, close_price,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, now())
                """,
                [
                    closed_id,
                    "AAPL",
                    True,
                    date(2026, 7, 1),
                    100.0,
                    10,
                    95.0,
                    date(2026, 7, 10),
                    110.0,
                ],
            )
            conn.execute(
                """
                INSERT INTO positions (
                    position_id, symbol, is_paper, entry_date, entry_price,
                    shares, stop_price, status, close_date, close_price,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, now())
                """,
                [open_id, "MSFT", True, date(2026, 7, 1), 200.0, 5, 190.0],
            )

        store = StateStore(database)
        store.init_schema()  # Must not raise against the old table shape.

        closed = store.get_position(UUID(closed_id))
        opened = store.get_position(UUID(open_id))
        assert closed is not None
        assert opened is not None
        assert closed.exit_reason == "unknown"
        assert opened.exit_reason is None

        # Idempotent: re-running must not disturb either row.
        store.init_schema()
        closed_again = store.get_position(UUID(closed_id))
        opened_again = store.get_position(UUID(open_id))
        assert closed_again is not None
        assert opened_again is not None
        assert closed_again.exit_reason == "unknown"
        assert opened_again.exit_reason is None

    def test_positions_check_constraint_rejects_invalid_exit_reason(self, state_store):
        # REQ-001/020: exit_reason is limited to the closed enum at the
        # schema level (fresh DB), independent of application validation.
        with (
            state_store._database.connect() as conn,  # noqa: SLF001
            pytest.raises(ConstraintException),
        ):
            conn.execute(
                """
                INSERT INTO positions (
                    position_id, symbol, is_paper, entry_date, entry_price,
                    shares, stop_price, status, close_date, close_price,
                    exit_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, now())
                """,
                [
                    str(uuid4()),
                    "AAPL",
                    True,
                    date(2026, 7, 1),
                    100.0,
                    10,
                    95.0,
                    date(2026, 7, 10),
                    110.0,
                    "not_a_real_reason",
                ],
            )

    def test_text_items_has_related_symbols_and_category_columns(self, state_store):
        with state_store._database.connect() as conn:  # noqa: SLF001
            columns = conn.execute("DESCRIBE text_items").fetchall()
        names = [row[0] for row in columns]
        assert len(names) == 10
        assert "related_symbols" in names
        assert "category" in names

    def test_init_schema_adds_related_symbols_and_category_to_a_pre_p8_123_table(
        self, tmp_path
    ):
        # A database created before this change: text_items has the old
        # 8-column shape with no related_symbols/category columns at all.
        database = Database(tmp_path / "pre_p8_123.duckdb")
        with database.connect() as conn:
            conn.execute(_PRE_P8_123_TEXT_ITEMS_TABLE)
            conn.execute(
                """
                INSERT INTO text_items (
                    source_id, symbol, source_type, published_at, title,
                    source_url, content_text, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    "finnhub-1",
                    "AAPL",
                    "news",
                    datetime(2026, 7, 19, tzinfo=UTC),
                    "Example headline",
                    "https://example.com/1",
                    "Example body text.",
                    datetime(2026, 7, 20, tzinfo=UTC),
                ],
            )

        store = StateStore(database)
        store.init_schema()  # Must not raise against the old table shape.

        with database.connect() as conn:
            row = conn.execute(
                "SELECT symbol, related_symbols, category FROM text_items "
                "WHERE source_id = 'finnhub-1'"
            ).fetchone()
            count = conn.execute("SELECT count(*) FROM text_items").fetchone()
        assert row == ("AAPL", None, None)
        assert count == (1,)

        # Idempotent: re-running the migration a second time must not raise
        # or disturb the already-migrated row.
        store.init_schema()
        with database.connect() as conn:
            row_again = conn.execute(
                "SELECT symbol, related_symbols, category FROM text_items "
                "WHERE source_id = 'finnhub-1'"
            ).fetchone()
        assert row_again == ("AAPL", None, None)

    def test_init_schema_leaves_pre_issue_157_coverage_rows_not_recorded(
        self, tmp_path
    ):
        # Issue #157: nothing in an existing row says whether its filing's
        # exhibits were cut off at collection, so the migration adds the
        # column without backfilling it. NULL keeps meaning "not recorded",
        # which readers must not take for "no exhibit was cut".
        database = Database(tmp_path / "pre_issue_157.duckdb")
        run_id = uuid4()
        with database.connect() as conn:
            conn.execute(_PRE_EXHIBIT_TRUNCATED_COVERAGE_TABLE)
            conn.execute(
                """
                INSERT INTO analysis_source_coverage (
                    run_id, symbol, source_id, original_chars, exported_chars,
                    is_truncated, selection_mode, sections_json
                ) VALUES (?, 'AAPL', 'edgar:1', 64841, 64841, FALSE, 'full', '[]')
                """,
                [str(run_id)],
            )

        store = StateStore(database)
        store.init_schema()  # Must not raise against the old table shape.

        rows = store.get_analysis_source_coverages(run_id, "AAPL")
        assert len(rows) == 1
        assert rows[0].exhibit_truncated is None

        # Idempotent: re-running must not disturb the un-backfilled row.
        store.init_schema()
        assert store.get_analysis_source_coverages(run_id, "AAPL")[0] == rows[0]

    def test_init_schema_adds_run_metadata_to_a_pre_i57_database(self, tmp_path):
        database = Database(tmp_path / "pre_i57.duckdb")
        with database.connect() as conn:
            conn.execute(_PRE_I57_RUNS_TABLE)

        store = StateStore(database)
        store.init_schema()
        run_id = store.start_run(
            date(2026, 7, 20),
            RunMode.LIVE,
            "a" * 64,
            metadata={"schema_version": "run-metadata-v1"},
        )

        with database.connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM runs WHERE run_id = ?", [str(run_id)]
            ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == {"schema_version": "run-metadata-v1"}


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

    def test_start_run_persists_canonical_reconstruction_metadata(self, state_store):
        metadata = {
            "provider": {"data_tier": "prototype", "name": "yfinance"},
            "schema_version": "run-metadata-v1",
        }
        run_id = state_store.start_run(
            date(2026, 7, 20), RunMode.DRY_RUN, "b" * 64, metadata=metadata
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT config_hash, metadata_json FROM runs WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert row[0] == "b" * 64
        assert json.loads(row[1]) == metadata

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


def _insert_trade_decision(  # noqa: PLR0913 - test helper, keyword-only for clarity
    state_store: StateStore,
    *,
    run_id: UUID,
    symbol: str,
    strategy_key: str,
    position_id: UUID,
    created_at: datetime,
) -> None:
    with state_store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO trades_journal (
                journal_id, run_id, symbol, strategy_key, position_id,
                decision, reason_memo, virtual_fill_price, created_at
            ) VALUES (?, ?, ?, ?, ?, 'followed', NULL, NULL, ?)
            """,
            [
                str(uuid4()),
                str(run_id),
                symbol,
                strategy_key,
                str(position_id),
                created_at,
            ],
        )


class TestClosedPositionsWithStrategy:
    def test_pairs_a_closed_position_with_its_linked_strategy_key(self, state_store):
        position = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=date(2026, 7, 1),
            entry_price=100.0,
            shares=10,
            status="closed",
            close_date=date(2026, 7, 10),
            close_price=110.0,
            exit_reason="target",
        )
        state_store.upsert_position(position)
        _insert_trade_decision(
            state_store,
            run_id=uuid4(),
            symbol="AAPL",
            strategy_key="trend_follow",
            position_id=position.position_id,
            created_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        )

        result = state_store.get_closed_positions_with_strategy(is_paper=True)

        assert result == [(position, "trend_follow")]

    def test_strategy_key_is_none_when_no_trades_journal_row_links_the_position(
        self, state_store
    ):
        position = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=date(2026, 7, 1),
            entry_price=100.0,
            shares=10,
            status="closed",
            close_date=date(2026, 7, 10),
            close_price=110.0,
            exit_reason="target",
        )
        state_store.upsert_position(position)

        result = state_store.get_closed_positions_with_strategy(is_paper=True)

        assert result == [(position, None)]

    def test_multiple_trades_journal_rows_for_one_position_pick_earliest_created_at(
        self, state_store
    ):
        # trades_journal.position_id is not itself uniquely constrained
        # (UNIQUE (run_id, symbol, strategy_key) is the real key), so more
        # than one row can reference the same position_id. The tie-break is
        # earliest created_at, tie-broken by strategy_key — deliberately
        # NOT alphabetical-first, to prove created_at (not strategy_key) is
        # the primary ordering.
        position = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=date(2026, 7, 1),
            entry_price=100.0,
            shares=10,
            status="closed",
            close_date=date(2026, 7, 10),
            close_price=110.0,
            exit_reason="target",
        )
        state_store.upsert_position(position)
        _insert_trade_decision(
            state_store,
            run_id=uuid4(),
            symbol="AAPL",
            strategy_key="zzz_earlier",
            position_id=position.position_id,
            created_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        )
        _insert_trade_decision(
            state_store,
            run_id=uuid4(),
            symbol="AAPL",
            strategy_key="aaa_later",
            position_id=position.position_id,
            created_at=datetime(2026, 7, 1, 11, 0, tzinfo=UTC),
        )

        result = state_store.get_closed_positions_with_strategy(is_paper=True)

        assert result == [(position, "zzz_earlier")]

    def test_as_of_boundary_immediately_before_at_and_after_cutoff(self, state_store):
        as_of = date(2026, 7, 20)

        def _closed(symbol: str, close_date: date) -> Position:
            return Position(
                position_id=uuid4(),
                symbol=symbol,
                is_paper=True,
                entry_date=date(2026, 7, 1),
                entry_price=100.0,
                shares=10,
                status="closed",
                close_date=close_date,
                close_price=110.0,
                exit_reason="target",
            )

        before = _closed("BEFORE", date(2026, 7, 19))
        at_cutoff = _closed("AT", as_of)
        after = _closed("AFTER", date(2026, 7, 21))
        for position in (before, at_cutoff, after):
            state_store.upsert_position(position)

        result = state_store.get_closed_positions_with_strategy(
            is_paper=True, as_of=as_of
        )

        symbols = {position.symbol for position, _ in result}
        assert symbols == {"BEFORE", "AT"}


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

    def test_rolls_back_entirely_when_a_later_hit_has_a_non_finite_metric(
        self, state_store
    ):
        # P1-04 (Issue #13): dumps_safe raises before the guarded row's
        # INSERT runs. That ValueError must still trigger the existing
        # transaction's ROLLBACK -- an earlier, otherwise-valid hit in the
        # same batch must not be left committed.
        run_date = date(2026, 7, 20)
        valid_hit = SignalHit(
            symbol="AAPL",
            signal_name="trend_sma",
            direction="long",
            strength=1.0,
            metrics={"sma_long": 100.0},
        )
        nan_hit = SignalHit(
            symbol="MSFT",
            signal_name="trend_sma",
            direction="long",
            strength=1.0,
            metrics={"sma_long": float("nan")},
        )

        with pytest.raises(ValueError, match="non-finite"):
            state_store.record_signals([valid_hit, nan_hit], run_date, "default")

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute("SELECT count(*) FROM signals").fetchone()
        assert count == (0,)


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
            ScreeningResult(
                candidates=candidates,
                rejections=rejections,
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
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
            ScreeningResult(
                candidates=[],
                rejections=[rejection],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
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
                ScreeningResult(
                    candidates=candidates,
                    rejections=rejections,
                    truncated=[],
                ),
                ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
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
                ScreeningResult(
                    candidates=candidates,
                    rejections=rejections,
                    truncated=[],
                ),
                ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
            )

        monkeypatch.setattr(state_store._database, "connect", real_connect)  # noqa: SLF001
        retry_run_id = uuid4()
        state_store.record_screening_results(
            ScreeningResult(
                candidates=candidates,
                rejections=rejections,
                truncated=[],
            ),
            ScreeningRunMeta(retry_run_id, "default", date(2026, 7, 20), 5),
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
            ScreeningResult(
                candidates=candidates,
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute(
                "SELECT count(*) FROM screening_rejections WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert count == (0,)


def _truncated(symbol: str, rank: int, score: float = 0.5) -> TruncatedCandidate:
    return TruncatedCandidate(
        symbol=symbol,
        rank=rank,
        score=score,
        score_breakdown={
            "score_rsi_pullback": 0.1,
            "score_trend_quality": 0.2,
            "score_liquidity": 0.3,
            "score_atr_pct": 0.4,
        },
        execution_state="READY",
        execution_distance=0.02,
    )


def _truncated_with_strategy_components(symbol: str, rank: int) -> TruncatedCandidate:
    """Issue #251: a near-miss whose breakdown carries all seven components."""
    return TruncatedCandidate(
        symbol=symbol,
        rank=rank,
        score=0.5,
        score_breakdown={
            "score_rsi_pullback": 0.1,
            "score_trend_quality": 0.2,
            "score_liquidity": 0.3,
            "score_atr_pct": 0.4,
            "score_pivot_proximity": 0.5,
            "score_rs_percentile": 0.6,
            "score_criteria_met": 0.7,
        },
        execution_state="READY",
        execution_distance=0.02,
    )


class _FlakyTruncationConnection:
    """Wraps a real connection; raises on the Nth `INSERT INTO screening_truncations`.

    Targets the third table of `record_screening_results`' single transaction,
    so the rollback assertion covers a failure that lands *after* candidate
    rows, rejection rows, and an earlier truncation row all succeeded.
    """

    def __init__(self, real_conn: duckdb.DuckDBPyConnection, fail_on_call: int):
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._insert_calls = 0

    def execute(self, sql, parameters=None):
        if sql.lstrip().startswith("INSERT INTO screening_truncations"):
            self._insert_calls += 1
            if self._insert_calls == self._fail_on_call:
                msg = "simulated failure on a later truncation insert"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


class TestRecordScreeningTruncations:
    """Issue #188: `screening_truncations` and its share of the one transaction."""

    def test_strategy_specific_components_reach_their_own_columns(self, state_store):
        # Issue #251: the truncated tail has no `metrics_json` fallback, so a
        # component the breakdown carries but no column holds would be lost.
        run_id = uuid4()

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[_truncated_with_strategy_components("NEAR", rank=6)],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT score_pivot_proximity, score_rs_percentile, "
                "score_criteria_met FROM screening_truncations WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert row == (0.5, 0.6, 0.7)

    def test_records_rank_score_and_breakdown_columns(self, state_store):
        run_id = uuid4()

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[_truncated("NEAR", rank=6, score=0.42)],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT symbol, strategy_key, rank, score, score_rsi_pullback,
                       score_trend_quality, score_liquidity, score_atr_pct,
                       execution_state, execution_distance, as_of
                FROM screening_truncations WHERE run_id = ?
                """,
                [str(run_id)],
            ).fetchone()
        assert row == (
            "NEAR",
            "default",
            6,
            0.42,
            0.1,
            0.2,
            0.3,
            0.4,
            "READY",
            0.02,
            date(2026, 7, 20),
        )

    def test_retains_only_three_pages_below_the_cut_closest_first(self, state_store):
        # The retention rule is `candidate_limit * 3`, applied by rank, and it
        # must not depend on the order the sequence arrived in.
        run_id = uuid4()
        truncations = [_truncated(f"T{rank}", rank) for rank in range(9, 2, -1)]

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=truncations,
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 2),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            ranks = conn.execute(
                "SELECT rank FROM screening_truncations WHERE run_id = ? ORDER BY rank",
                [str(run_id)],
            ).fetchall()
        assert ranks == [(3,), (4,), (5,), (6,), (7,), (8,)]

    def test_zero_candidate_limit_retains_nothing(self, state_store):
        # Boundary: with no candidates there is no "just below the cut" to
        # compare a near-miss against, so nothing is worth storing.
        run_id = uuid4()

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[_truncated("NEAR", rank=1)],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 0),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute(
                "SELECT count(*) FROM screening_truncations WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert count == (0,)

    def test_rerun_replaces_the_tail_instead_of_leaving_phantom_near_misses(
        self, state_store
    ):
        # Snapshot-replacement semantics: a symbol that the corrected ranking
        # moved above the cut must not survive as a near-miss.
        run_id = uuid4()
        meta = ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5)
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[_truncated("GONE", 6), _truncated("STAY", 7)],
            ),
            meta,
        )

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[_truncated("STAY", rank=6, score=0.9)],
            ),
            meta,
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol, rank, score FROM screening_truncations "
                "WHERE run_id = ? ORDER BY symbol",
                [str(run_id)],
            ).fetchall()
        assert rows == [("STAY", 6, 0.9)]

    def test_replacement_is_scoped_to_the_written_strategy(self, state_store):
        # Two strategies screened in one run are two independent rankings;
        # writing one must not delete the other's tail.
        run_id = uuid4()
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[_truncated("OTHER", 6)],
            ),
            ScreeningRunMeta(run_id, "vcp", date(2026, 7, 20), 5),
        )

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[_truncated("NEAR", 6)],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT strategy_key, symbol FROM screening_truncations "
                "WHERE run_id = ? ORDER BY strategy_key",
                [str(run_id)],
            ).fetchall()
        assert rows == [("default", "NEAR"), ("vcp", "OTHER")]

    def test_rolls_back_all_three_tables_when_a_later_truncation_insert_fails(
        self, state_store, monkeypatch
    ):
        # Failure injection after >=1 candidate, >=1 rejection, and >=1
        # truncation row already succeeded inside the same transaction.
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
            )
        ]
        truncations = [_truncated(f"T{rank}", rank) for rank in (6, 7, 8)]

        real_connect = state_store._database.connect  # noqa: SLF001
        monkeypatch.setattr(
            state_store._database,  # noqa: SLF001
            "connect",
            lambda: _FlakyTruncationConnection(real_connect(), fail_on_call=3),
        )

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.record_screening_results(
                ScreeningResult(
                    candidates=candidates,
                    rejections=rejections,
                    truncated=truncations,
                ),
                ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
            )

        monkeypatch.setattr(state_store._database, "connect", real_connect)  # noqa: SLF001
        with state_store._database.connect() as conn:  # noqa: SLF001
            counts = conn.execute(
                "SELECT "
                "(SELECT count(*) FROM candidates WHERE run_id = ?), "
                "(SELECT count(*) FROM screening_rejections WHERE run_id = ?), "
                "(SELECT count(*) FROM screening_truncations WHERE run_id = ?)",
                [str(run_id)] * 3,
            ).fetchone()
        assert counts == (0, 0, 0)

    def test_rerun_after_a_rolled_back_truncation_failure_succeeds(
        self, state_store, monkeypatch
    ):
        run_id = uuid4()
        meta = ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5)
        truncations = [_truncated(f"T{rank}", rank) for rank in (6, 7)]
        real_connect = state_store._database.connect  # noqa: SLF001
        monkeypatch.setattr(
            state_store._database,  # noqa: SLF001
            "connect",
            lambda: _FlakyTruncationConnection(real_connect(), fail_on_call=2),
        )
        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.record_screening_results(
                ScreeningResult(
                    candidates=[],
                    rejections=[],
                    truncated=truncations,
                ),
                meta,
            )

        monkeypatch.setattr(state_store._database, "connect", real_connect)  # noqa: SLF001
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=truncations,
            ),
            meta,
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute(
                "SELECT count(*) FROM screening_truncations WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert count == (2,)


def _hit(
    symbol: str, signal_name: str = "trend_sma", strength: float = 1.0
) -> SignalHit:
    return SignalHit(
        symbol=symbol,
        signal_name=signal_name,
        direction="long",
        strength=strength,
        metrics={"rsi14": 40.0},
    )


class _FlakySignalHitConnection:
    """Wraps a real connection; raises on the Nth `INSERT INTO signal_hits`.

    Targets the fourth (last) table of `record_screening_results`' single
    transaction, so the rollback assertion covers a failure landing after
    candidate, rejection, truncation, and an earlier signal-hit row have all
    succeeded.
    """

    def __init__(self, real_conn: duckdb.DuckDBPyConnection, fail_on_call: int):
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._insert_calls = 0

    def execute(self, sql, parameters=None):
        if sql.lstrip().startswith("INSERT INTO signal_hits"):
            self._insert_calls += 1
            if self._insert_calls == self._fail_on_call:
                msg = "simulated failure on a later signal hit insert"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


class TestRecordSignalHits:
    """Issue #192: `signal_hits`, the `run_id`-keyed successor to `signals`."""

    def test_records_hits_against_the_run_not_the_run_date(self, state_store):
        run_id = uuid4()

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[],
                signal_hits=[_hit("AAPL"), _hit("AAPL", "rsi_pullback", 0.5)],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol, strategy_key, signal_name, strength, metrics_json "
                "FROM signal_hits WHERE run_id = ? ORDER BY signal_name",
                [str(run_id)],
            ).fetchall()
        assert rows == [
            ("AAPL", "default", "rsi_pullback", 0.5, '{"rsi14": 40.0}'),
            ("AAPL", "default", "trend_sma", 1.0, '{"rsi14": 40.0}'),
        ]

    def test_same_date_dry_run_and_live_runs_no_longer_collide(self, state_store):
        """The defect that made the legacy `signals` table unusable."""
        live = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
        dry_run = state_store.start_run(date(2026, 7, 20), RunMode.DRY_RUN, "cfg")
        for run_id, strength in ((live, 1.0), (dry_run, 0.25)):
            state_store.record_screening_results(
                ScreeningResult(
                    candidates=[],
                    rejections=[],
                    truncated=[],
                    signal_hits=[_hit("AAPL", strength=strength)],
                ),
                ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
            )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT run_id, strength FROM signal_hits ORDER BY strength"
            ).fetchall()
        assert {(str(row[0]), row[1]) for row in rows} == {
            (str(live), 1.0),
            (str(dry_run), 0.25),
        }

    def test_a_rerun_drops_a_hit_the_new_ranking_no_longer_has(self, state_store):
        # Replacement, not upsert: a signal that stopped firing on corrected
        # bars must not survive as a phantom hit.
        run_id = uuid4()
        meta = ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5)
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[],
                signal_hits=[_hit("AAPL"), _hit("GONE")],
            ),
            meta,
        )

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[],
                signal_hits=[_hit("AAPL", strength=0.75)],
            ),
            meta,
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol, strength FROM signal_hits WHERE run_id = ?",
                [str(run_id)],
            ).fetchall()
        assert rows == [("AAPL", 0.75)]

    def test_replacement_is_scoped_to_the_strategy_being_rewritten(self, state_store):
        run_id = uuid4()
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[], rejections=[], truncated=[], signal_hits=[_hit("OTHER")]
            ),
            ScreeningRunMeta(run_id, "vcp", date(2026, 7, 20), 5),
        )

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[], rejections=[], truncated=[], signal_hits=[_hit("AAPL")]
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT strategy_key, symbol FROM signal_hits WHERE run_id = ? "
                "ORDER BY strategy_key",
                [str(run_id)],
            ).fetchall()
        assert rows == [("default", "AAPL"), ("vcp", "OTHER")]

    def test_rolls_back_all_four_tables_when_a_later_hit_insert_fails(
        self, state_store, monkeypatch
    ):
        run_id = uuid4()
        candidates = [
            Candidate(
                symbol="AAPL",
                as_of=date(2026, 7, 20),
                signal_names=(),
                metrics={"score": 0.5},
                rank=1,
            )
        ]
        rejections = [
            RejectionRecord(
                symbol="R1",
                stage=RejectionStage.DATA_QUALITY,
                reason_code=RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
                detail={"available_quarters": 0, "required_quarters": 4},
            )
        ]
        hits = [_hit(f"H{index}") for index in range(3)]

        real_connect = state_store._database.connect  # noqa: SLF001
        monkeypatch.setattr(
            state_store._database,  # noqa: SLF001
            "connect",
            lambda: _FlakySignalHitConnection(real_connect(), fail_on_call=3),
        )

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.record_screening_results(
                ScreeningResult(
                    candidates=candidates,
                    rejections=rejections,
                    truncated=[_truncated("NEAR", 6)],
                    signal_hits=hits,
                ),
                ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
            )

        monkeypatch.setattr(state_store._database, "connect", real_connect)  # noqa: SLF001
        with state_store._database.connect() as conn:  # noqa: SLF001
            counts = conn.execute(
                "SELECT "
                "(SELECT count(*) FROM candidates WHERE run_id = ?), "
                "(SELECT count(*) FROM screening_rejections WHERE run_id = ?), "
                "(SELECT count(*) FROM screening_truncations WHERE run_id = ?), "
                "(SELECT count(*) FROM signal_hits WHERE run_id = ?)",
                [str(run_id)] * 4,
            ).fetchone()
        assert counts == (0, 0, 0, 0)


class TestPromotedCandidateColumns:
    """Issue #192: the ranking key as columns, written straight from `Candidate`."""

    def _candidate(
        self,
        metrics: dict[str, float] | None = None,
        execution_state: str = "READY",
        execution_distance: float | None = 0.35,
    ) -> Candidate:
        return Candidate(
            symbol="AAPL",
            as_of=date(2026, 7, 20),
            signal_names=("trend_sma",),
            metrics=(
                {
                    "score": 0.62,
                    "score_rsi_pullback": 0.30,
                    "score_trend_quality": 0.20,
                    "score_liquidity": 0.10,
                    "score_atr_pct": 0.02,
                }
                if metrics is None
                else metrics
            ),
            rank=1,
            execution_state=execution_state,
            execution_distance=execution_distance,
        )

    def _row(self, state_store, run_id):
        with state_store._database.connect() as conn:  # noqa: SLF001
            return conn.execute(
                "SELECT score, score_rsi_pullback, score_trend_quality, "
                "score_liquidity, score_atr_pct, execution_state, "
                "execution_distance FROM candidates WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()

    def test_records_score_components_and_execution_state(self, state_store):
        run_id = uuid4()

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[self._candidate()], rejections=[], truncated=[]
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        assert self._row(state_store, run_id) == (
            0.62,
            0.30,
            0.20,
            0.10,
            0.02,
            "READY",
            0.35,
        )

    def test_strategy_specific_components_reach_their_own_columns(self, state_store):
        # Issue #251: the three components added to `ScoreWeights` are
        # promoted like the first four, so `score` still equals the sum of
        # the component columns when a strategy weights one of them.
        run_id = uuid4()

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[
                    self._candidate(
                        metrics={
                            "score": 0.62,
                            "score_pivot_proximity": 0.07,
                            "score_rs_percentile": 0.05,
                            "score_criteria_met": 0.03,
                        }
                    )
                ],
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT score_pivot_proximity, score_rs_percentile, "
                "score_criteria_met FROM candidates WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert row == (0.07, 0.05, 0.03)

    def test_a_rerun_corrects_the_promoted_columns_too(self, state_store):
        # Correction upsert: a stale score beside a corrected `metrics_json`
        # would be the worst of both shapes.
        run_id = uuid4()
        meta = ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5)
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[self._candidate()], rejections=[], truncated=[]
            ),
            meta,
        )

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[
                    self._candidate(
                        metrics={"score": 0.11, "score_rsi_pullback": 0.05},
                        execution_state="EXTENDED",
                        execution_distance=2.5,
                    )
                ],
                rejections=[],
                truncated=[],
            ),
            meta,
        )

        assert self._row(state_store, run_id) == (
            0.11,
            0.05,
            None,
            None,
            None,
            "EXTENDED",
            2.5,
        )

    def test_a_candidate_without_score_metrics_records_null_not_zero(self, state_store):
        """An uncomputed component and a component computed as 0.0 differ."""
        run_id = uuid4()

        state_store.record_screening_results(
            ScreeningResult(
                candidates=[self._candidate(metrics={})], rejections=[], truncated=[]
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        assert self._row(state_store, run_id) == (
            None,
            None,
            None,
            None,
            None,
            "READY",
            0.35,
        )


class _FlakyUniverseReturnConnection:
    """Wraps a real connection; raises on the Nth `INSERT INTO universe_forward_returns`."""

    def __init__(self, real_conn: duckdb.DuckDBPyConnection, fail_on_call: int):
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._insert_calls = 0

    def execute(self, sql, parameters=None):
        if sql.lstrip().startswith("INSERT INTO universe_forward_returns"):
            self._insert_calls += 1
            if self._insert_calls == self._fail_on_call:
                msg = "simulated failure on a later universe return insert"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


def _universe_return(  # noqa: PLR0913 - a record factory mirroring the row's own columns
    run_id: UUID,
    symbol: str,
    outcome_class: str = "rejected",
    reason_code: str | None = "FILTER_NEGATIVE_FCF",
    forward_return_pct: float = -1.5,
    horizon_days: int = 5,
) -> UniverseForwardReturnRecord:
    return UniverseForwardReturnRecord(
        run_id=run_id,
        symbol=symbol,
        horizon_days=horizon_days,
        as_of=date(2026, 7, 27),
        outcome_class=outcome_class,
        reason_code=reason_code,
        forward_return_pct=forward_return_pct,
    )


class TestReplaceUniverseForwardReturns:
    """Issue #188: the control-group forward returns and their replacement."""

    def test_persists_each_outcome_class_with_its_reason_code(self, state_store):
        run_id = uuid4()

        state_store.replace_universe_forward_returns(
            run_id,
            5,
            [
                _universe_return(run_id, "CAND", "candidate", None, 3.0),
                _universe_return(run_id, "NEAR", "truncated", None, 1.0),
                _universe_return(run_id, "GONE", "rejected", "FILTER_NEGATIVE_FCF"),
            ],
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol, outcome_class, reason_code, forward_return_pct "
                "FROM universe_forward_returns WHERE run_id = ? ORDER BY symbol",
                [str(run_id)],
            ).fetchall()
        assert rows == [
            ("CAND", "candidate", None, 3.0),
            ("GONE", "rejected", "FILTER_NEGATIVE_FCF", -1.5),
            ("NEAR", "truncated", None, 1.0),
        ]

    def test_invalid_outcome_class_violates_check_constraint(self, state_store):
        with (
            state_store._database.connect() as conn,  # noqa: SLF001
            pytest.raises(ConstraintException),
        ):
            conn.execute(
                """
                INSERT INTO universe_forward_returns (
                    run_id, symbol, horizon_days, as_of, outcome_class,
                    reason_code, forward_return_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [str(uuid4()), "XYZ", 5, date(2026, 7, 27), "shortlisted", None, 1.0],
            )

    def test_replacement_drops_symbols_absent_from_the_recomputed_set(
        self, state_store
    ):
        run_id = uuid4()
        state_store.replace_universe_forward_returns(
            run_id, 5, [_universe_return(run_id, "GONE"), _universe_return(run_id, "A")]
        )

        state_store.replace_universe_forward_returns(
            run_id, 5, [_universe_return(run_id, "A", forward_return_pct=2.25)]
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol, forward_return_pct FROM universe_forward_returns "
                "WHERE run_id = ?",
                [str(run_id)],
            ).fetchall()
        assert rows == [("A", 2.25)]

    def test_replacement_leaves_the_other_horizon_untouched(self, state_store):
        run_id = uuid4()
        state_store.replace_universe_forward_returns(
            run_id, 20, [_universe_return(run_id, "A", horizon_days=20)]
        )

        state_store.replace_universe_forward_returns(
            run_id, 5, [_universe_return(run_id, "B")]
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT horizon_days, symbol FROM universe_forward_returns "
                "WHERE run_id = ? ORDER BY horizon_days",
                [str(run_id)],
            ).fetchall()
        assert rows == [(5, "B"), (20, "A")]

    @pytest.mark.parametrize(
        ("run_id_matches", "horizon_days"),
        [
            pytest.param(False, 5, id="foreign-run-id"),
            pytest.param(True, 20, id="foreign-horizon"),
        ],
    )
    def test_records_outside_the_replaced_slice_raise(
        self, state_store, run_id_matches, horizon_days
    ):
        run_id = uuid4()
        record_run_id = run_id if run_id_matches else uuid4()

        with pytest.raises(ValueError, match="must match the replacement"):
            state_store.replace_universe_forward_returns(
                run_id,
                5,
                [_universe_return(record_run_id, "A", horizon_days=horizon_days)],
            )

    def test_rolls_back_when_a_later_insert_fails(self, state_store, monkeypatch):
        run_id = uuid4()
        state_store.replace_universe_forward_returns(
            run_id, 5, [_universe_return(run_id, "KEEP")]
        )
        real_connect = state_store._database.connect  # noqa: SLF001
        monkeypatch.setattr(
            state_store._database,  # noqa: SLF001
            "connect",
            lambda: _FlakyUniverseReturnConnection(real_connect(), fail_on_call=2),
        )

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_universe_forward_returns(
                run_id,
                5,
                [_universe_return(run_id, "A"), _universe_return(run_id, "B")],
            )

        monkeypatch.setattr(state_store._database, "connect", real_connect)  # noqa: SLF001
        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol FROM universe_forward_returns WHERE run_id = ?",
                [str(run_id)],
            ).fetchall()
        # The DELETE that opened the failed replacement rolled back too, so the
        # previously committed slice is still readable in full.
        assert rows == [("KEEP",)]


class TestRecordRiskAssessments:
    def test_rolls_back_entirely_when_a_later_row_fails(self, state_store):
        # One run's assessments are one logical write (AGENTS.md): inject a
        # failure after the first row has been inserted — the CHECK
        # constraint rejects the second row's status — and assert the first
        # row did not survive on its own.
        run_id = uuid4()
        valid = RiskAssessment(
            symbol="AAPL",
            status="approved",
            max_shares=10,
            entry_price=100.0,
            stop_price=95.0,
            reasons=(),
        )
        invalid = RiskAssessment(
            symbol="MSFT",
            status="bogus_status",
            max_shares=10,
            entry_price=100.0,
            stop_price=95.0,
            reasons=(),
        )

        with pytest.raises(duckdb.Error):
            state_store.record_risk_assessments([valid, invalid], run_id)

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute("SELECT count(*) FROM risk_assessments").fetchone()
        assert count == (0,)

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

    def test_data_quality_correlation_warnings_nan_sentinel_persists_as_json_null(
        self, state_store
    ):
        # P1-04 (Issue #13): risk/checks.py::check_correlation intentionally
        # uses NaN as CorrelationWarning.correlation's "not computable"
        # sentinel for warning_type="data_quality". dumps_safe must not
        # reject the whole row for this legitimate value -- it is persisted
        # as JSON null (the spec-compliant representation) instead.
        run_id = uuid4()
        assessment = RiskAssessment(
            symbol="AAPL",
            status="approved",
            max_shares=10,
            entry_price=100.0,
            stop_price=95.0,
            reasons=(),
            warnings=(CorrelationWarning("MSFT", float("nan"), "data_quality"),),
        )

        state_store.record_risk_assessments([assessment], run_id)

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT warnings_json FROM risk_assessments WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        parsed = json.loads(row[0])
        assert parsed == [
            {
                "warning_type": "data_quality",
                "correlated_symbol": "MSFT",
                "correlation": None,
            }
        ]

    def test_records_sizing_breakdown(self, state_store):
        # REQ-005: shares_by_risk/shares_by_position_cap/binding_constraint/
        # sizing_warnings all persist alongside the existing columns.
        run_id = uuid4()
        assessment = RiskAssessment(
            symbol="AAPL",
            status="approved",
            max_shares=200,
            entry_price=50.0,
            stop_price=45.0,
            reasons=(),
            warnings=(),
            shares_by_risk=200,
            shares_by_position_cap=500,
            binding_constraint="trade_risk",
            sizing_warnings=("WIDE_STOP",),
        )

        state_store.record_risk_assessments([assessment], run_id)

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT shares_by_risk, shares_by_position_cap,
                       binding_constraint, sizing_warnings_json
                FROM risk_assessments WHERE run_id = ?
                """,
                [str(run_id)],
            ).fetchone()
        assert row[0] == 200
        assert row[1] == 500
        assert row[2] == "trade_risk"
        assert "WIDE_STOP" in row[3]

    def test_rerun_correction_upserts_sizing_breakdown(self, state_store):
        # Natural-key rerun (same run_id, symbol) must overwrite the sizing
        # breakdown, not silently keep the stale first-write values.
        run_id = uuid4()
        first = RiskAssessment(
            symbol="AAPL",
            status="approved",
            max_shares=200,
            entry_price=50.0,
            stop_price=45.0,
            reasons=(),
            warnings=(),
            shares_by_risk=200,
            shares_by_position_cap=500,
            binding_constraint="trade_risk",
        )
        second = RiskAssessment(
            symbol="AAPL",
            status="approved",
            max_shares=40,
            entry_price=50.0,
            stop_price=45.0,
            reasons=(),
            warnings=(),
            shares_by_risk=200,
            shares_by_position_cap=40,
            binding_constraint="position_cap",
        )

        state_store.record_risk_assessments([first], run_id)
        state_store.record_risk_assessments([second], run_id)

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT max_shares, shares_by_position_cap, binding_constraint "
                "FROM risk_assessments WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert row == (40, 40, "position_cap")


class TestRecordSignalOutcomes:
    """P2-11: `signal_outcomes` writes, keyed by `(run_id, symbol, horizon_days)`."""

    def test_records_one_row(self, state_store):
        run_id = uuid4()
        outcome = SignalOutcomeRecord(
            run_id=run_id,
            symbol="AAPL",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=1.5,
            classification="TRUE_POSITIVE",
        )

        state_store.record_signal_outcomes([outcome])

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT symbol, horizon_days, forward_return_pct, classification "
                "FROM signal_outcomes WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert row == ("AAPL", 5, 1.5, "TRUE_POSITIVE")

    def test_empty_sequence_is_a_no_op(self, state_store):
        state_store.record_signal_outcomes([])

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute("SELECT count(*) FROM signal_outcomes").fetchone()
        assert count == (0,)

    def test_replacement_with_empty_sequence_removes_stale_rows(self, state_store):
        run_id = uuid4()
        outcome = SignalOutcomeRecord(
            run_id=run_id,
            symbol="AAPL",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=1.5,
            classification="TRUE_POSITIVE",
        )
        state_store.record_signal_outcomes([outcome])

        state_store.replace_signal_outcomes(run_id, 5, [])

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute(
                "SELECT count(*) FROM signal_outcomes WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert count == (0,)

    def test_replacement_rejects_mixed_natural_keys_before_delete(self, state_store):
        run_id = uuid4()
        existing = SignalOutcomeRecord(
            run_id=run_id,
            symbol="AAPL",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=1.5,
            classification="TRUE_POSITIVE",
        )
        mismatched = SignalOutcomeRecord(
            run_id=uuid4(),
            symbol="MSFT",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=1.0,
            classification="TRUE_POSITIVE",
        )
        state_store.record_signal_outcomes([existing])

        with pytest.raises(ValueError, match="must match"):
            state_store.replace_signal_outcomes(run_id, 5, [mismatched])

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute(
                "SELECT count(*) FROM signal_outcomes WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert count == (1,)

    def test_replacement_rolls_back_delete_when_insert_fails(self, state_store):
        run_id = uuid4()
        existing = SignalOutcomeRecord(
            run_id=run_id,
            symbol="AAPL",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=1.5,
            classification="TRUE_POSITIVE",
        )
        invalid = SignalOutcomeRecord(
            run_id=run_id,
            symbol="MSFT",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=-1.0,
            classification="NOT_A_CLASSIFICATION",
        )
        state_store.record_signal_outcomes([existing])

        with pytest.raises(ConstraintException):
            state_store.replace_signal_outcomes(run_id, 5, [invalid])

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol, classification FROM signal_outcomes "
                "WHERE run_id = ? AND horizon_days = 5",
                [str(run_id)],
            ).fetchall()
        assert rows == [("AAPL", "TRUE_POSITIVE")]

    def test_rerun_with_corrected_values_updates_the_existing_row(self, state_store):
        run_id = uuid4()
        first = SignalOutcomeRecord(
            run_id=run_id,
            symbol="AAPL",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=1.5,
            classification="TRUE_POSITIVE",
        )
        corrected = SignalOutcomeRecord(
            run_id=run_id,
            symbol="AAPL",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=-5.0,
            classification="FALSE_POSITIVE_SEVERE",
        )

        state_store.record_signal_outcomes([first])
        state_store.record_signal_outcomes([corrected])

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT forward_return_pct, classification FROM signal_outcomes "
                "WHERE run_id = ? AND symbol = ? AND horizon_days = ?",
                [str(run_id), "AAPL", 5],
            ).fetchall()
        assert rows == [(-5.0, "FALSE_POSITIVE_SEVERE")]

    def test_rolls_back_entirely_when_a_later_row_violates_a_check_constraint(
        self, state_store
    ):
        # An earlier, otherwise-valid row in the same batch must not be left
        # committed when a later row in the same call fails (AGENTS.md: one
        # logical multi-row write is one transaction).
        run_id = uuid4()
        valid = SignalOutcomeRecord(
            run_id=run_id,
            symbol="AAPL",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=1.5,
            classification="TRUE_POSITIVE",
        )
        invalid = SignalOutcomeRecord(
            run_id=run_id,
            symbol="MSFT",
            horizon_days=5,
            as_of=date(2026, 7, 24),
            signal_names=("trend_sma",),
            forward_return_pct=1.5,
            classification="NOT_A_REAL_CLASSIFICATION",
        )

        with pytest.raises(duckdb.ConstraintException):
            state_store.record_signal_outcomes([valid, invalid])

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute("SELECT count(*) FROM signal_outcomes").fetchone()
        assert count == (0,)


def _text_item(
    source_id: str,
    source_url: str,
    *,
    source_type: str = "news",
    related_symbols: tuple[str, ...] = (),
    category: str | None = None,
) -> TextItem:
    return TextItem(
        source_id=source_id,
        symbol="AAPL",
        source_type=source_type,
        published_at=datetime(2026, 7, 19, tzinfo=UTC),
        title="Example headline",
        source_url=source_url,
        content_text="Example body text.",
        fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
        related_symbols=related_symbols,
        category=category,
    )


def _filing_text_item(source_id: str, content_text: str) -> TextItem:
    """A collected filing, whose `content_text` is its whole audit copy."""
    return replace(
        _text_item(source_id, "https://example.com/8-K", source_type="filing"),
        content_text=content_text,
    )


def _text_item_content(state_store: StateStore, source_id: str) -> str:
    with state_store._database.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT content_text FROM text_items WHERE source_id = ?",
            [source_id],
        ).fetchone()
    assert row is not None
    return cast("str", row[0])


def _text_item_related_symbols_and_category(
    state_store: StateStore, source_id: str
) -> tuple[str | None, str | None]:
    with state_store._database.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT related_symbols, category FROM text_items WHERE source_id = ?",
            [source_id],
        ).fetchone()
    assert row is not None
    return (row[0], row[1])


def _dated_filing(source_id: str, symbol: str, filed_on: date) -> TextItem:
    """A collected filing for `symbol`, filed on `filed_on`.

    `published_at` is midnight UTC of the SEC filing date, exactly as
    `data/edgar.py::_filing_text_item` stores it.
    """
    return replace(
        _text_item(source_id, "https://example.com/10-Q", source_type="filing"),
        symbol=symbol,
        published_at=datetime.combine(filed_on, datetime.min.time(), tzinfo=UTC),
    )


class TestLatestFilingDates:
    """Issue #258: the zero-extra-request "was anything filed lately?" source.

    Feeds the fundamentals step's incremental refresh, so it must respect
    the same `as_of` visibility boundary as every other filing read.
    """

    def test_returns_the_most_recent_filing_per_symbol(self, state_store):
        state_store.record_text_items(
            [
                _dated_filing("edgar:1", "AAPL", date(2026, 7, 10)),
                _dated_filing("edgar:2", "AAPL", date(2026, 7, 18)),
                _dated_filing("edgar:3", "MSFT", date(2026, 7, 15)),
            ]
        )

        assert state_store.latest_filing_dates(
            ["AAPL", "MSFT"], as_of=date(2026, 7, 20)
        ) == {"AAPL": date(2026, 7, 18), "MSFT": date(2026, 7, 15)}

    def test_a_filing_accepted_exactly_on_as_of_is_visible(self, state_store):
        state_store.record_text_items(
            [_dated_filing("edgar:1", "AAPL", date(2026, 7, 20))]
        )

        assert state_store.latest_filing_dates(["AAPL"], as_of=date(2026, 7, 20)) == {
            "AAPL": date(2026, 7, 20)
        }

    def test_a_filing_accepted_the_day_before_as_of_is_visible(self, state_store):
        state_store.record_text_items(
            [_dated_filing("edgar:1", "AAPL", date(2026, 7, 19))]
        )

        assert state_store.latest_filing_dates(["AAPL"], as_of=date(2026, 7, 20)) == {
            "AAPL": date(2026, 7, 19)
        }

    def test_a_filing_accepted_the_day_after_as_of_is_invisible(self, state_store):
        state_store.record_text_items(
            [_dated_filing("edgar:1", "AAPL", date(2026, 7, 21))]
        )

        assert state_store.latest_filing_dates(["AAPL"], as_of=date(2026, 7, 20)) == {}

    def test_the_cutoff_hides_only_the_filings_past_it(self, state_store):
        state_store.record_text_items(
            [
                _dated_filing("edgar:1", "AAPL", date(2026, 7, 18)),
                _dated_filing("edgar:2", "AAPL", date(2026, 7, 21)),
            ]
        )

        assert state_store.latest_filing_dates(["AAPL"], as_of=date(2026, 7, 20)) == {
            "AAPL": date(2026, 7, 18)
        }

    def test_news_items_are_not_filings(self, state_store):
        state_store.record_text_items(
            [_text_item("finnhub-1", "https://example.com/1")]
        )

        assert state_store.latest_filing_dates(["AAPL"], as_of=date(2026, 7, 20)) == {}

    def test_a_symbol_with_no_collected_filing_is_omitted(self, state_store):
        state_store.record_text_items(
            [_dated_filing("edgar:1", "AAPL", date(2026, 7, 18))]
        )

        assert state_store.latest_filing_dates(
            ["AAPL", "MSFT"], as_of=date(2026, 7, 20)
        ) == {"AAPL": date(2026, 7, 18)}

    def test_empty_symbols_returns_empty_mapping(self, state_store):
        assert state_store.latest_filing_dates([], as_of=date(2026, 7, 20)) == {}


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

    def test_rerecording_corrects_a_body_stored_short_by_the_collection_stage(
        self, state_store
    ):
        # Issue #180: an exhibit cut at collection time is persisted as the
        # filing's whole audit copy, so the same-key rerun is the only path
        # back to the full text. `ON CONFLICT DO NOTHING` here would freeze
        # every filing collected under the old 60,000-character ceiling.
        stored_short = "a" * 60_000 + EXHIBIT_TRUNCATION_MARKER
        full_text = "a" * 375_403
        state_store.record_text_items(
            [_filing_text_item("edgar:0001-26-000009", stored_short)]
        )

        state_store.record_text_items(
            [_filing_text_item("edgar:0001-26-000009", full_text)]
        )

        assert _text_item_content(state_store, "edgar:0001-26-000009") == full_text

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

    def test_related_symbols_are_joined_by_comma_in_provider_order(self, state_store):
        state_store.record_text_items(
            [
                _text_item(
                    "finnhub-1",
                    "https://example.com/1",
                    related_symbols=("ADM", "AAPL"),
                )
            ]
        )

        related, _category = _text_item_related_symbols_and_category(
            state_store, "finnhub-1"
        )
        assert related == "ADM,AAPL"

    def test_single_related_symbol_is_stored_without_a_delimiter(self, state_store):
        state_store.record_text_items(
            [_text_item("finnhub-1", "https://example.com/1", related_symbols=("ADM",))]
        )

        related, _category = _text_item_related_symbols_and_category(
            state_store, "finnhub-1"
        )
        assert related == "ADM"

    def test_empty_related_symbols_are_stored_as_null(self, state_store):
        state_store.record_text_items(
            [_text_item("finnhub-1", "https://example.com/1", related_symbols=())]
        )

        related, _category = _text_item_related_symbols_and_category(
            state_store, "finnhub-1"
        )
        assert related is None

    def test_category_is_persisted(self, state_store):
        state_store.record_text_items(
            [_text_item("finnhub-1", "https://example.com/1", category="company")]
        )

        _related, category = _text_item_related_symbols_and_category(
            state_store, "finnhub-1"
        )
        assert category == "company"

    def test_none_category_is_stored_as_null(self, state_store):
        state_store.record_text_items(
            [_text_item("finnhub-1", "https://example.com/1", category=None)]
        )

        _related, category = _text_item_related_symbols_and_category(
            state_store, "finnhub-1"
        )
        assert category is None

    @pytest.mark.parametrize("source_type", ["filing", "calendar"])
    def test_filing_and_calendar_items_default_both_columns_to_null(
        self, state_store, source_type
    ):
        state_store.record_text_items(
            [_text_item("finnhub-1", "https://example.com/1", source_type=source_type)]
        )

        related, category = _text_item_related_symbols_and_category(
            state_store, "finnhub-1"
        )
        assert related is None
        assert category is None

    def test_rerecording_corrects_related_symbols_and_category(self, state_store):
        state_store.record_text_items(
            [
                _text_item(
                    "finnhub-1",
                    "https://example.com/1",
                    related_symbols=("ADM",),
                    category="company",
                )
            ]
        )
        state_store.record_text_items(
            [
                _text_item(
                    "finnhub-1",
                    "https://example.com/1",
                    related_symbols=("ADM", "AAPL"),
                    category="press-release",
                )
            ]
        )

        related, category = _text_item_related_symbols_and_category(
            state_store, "finnhub-1"
        )
        assert related == "ADM,AAPL"
        assert category == "press-release"
