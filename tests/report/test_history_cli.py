"""Read-only history CLI contracts: `copilot-history` (P1-05).

Covers each subcommand against a populated fixture DB and an empty DB,
Example 3's traceback-free non-zero exit for an unknown `run_id`, and
REQ-007's read-only guarantee (no subcommand mutates any table).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.models import RunMode
from swing_copilot.report.history_cli import main
from swing_copilot.report.incomplete_runs import ANALYSIS_INCOMPLETE_EXIT_CODE
from swing_copilot.risk.checks import RiskAssessment
from swing_copilot.screening.base import (
    Candidate,
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
    ScreeningResult,
)
from swing_copilot.storage.audit_records import ScreeningRunMeta
from tests.support.runs import seed_run

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.storage.database import Database
    from swing_copilot.storage.state_store import StateStore

_TABLES = (
    "universe_membership",
    "runs",
    "run_steps",
    "signals",
    "candidates",
    "screening_rejections",
    "risk_assessments",
    "text_items",
)


def _db_path(state_store: StateStore) -> str:
    return str(state_store._database.db_path)  # noqa: SLF001


def _write_run_archive(tmp_path: Path, run_date: date, *, has_result: bool) -> UUID:
    """Build one `reports/<date>/<run_id>/` archive under an isolated root.

    Detection is an existence check, so the documents' contents are
    irrelevant here; what matters is which of the two files is present.
    """
    run_id = uuid4()
    directory = tmp_path / "reports" / run_date.isoformat() / str(run_id)
    directory.mkdir(parents=True)
    (directory / ANALYSIS_INPUT_FILENAME).write_text("{}", encoding="utf-8")
    if has_result:
        (directory / ANALYSIS_RESULT_FILENAME).write_text("{}", encoding="utf-8")
    return run_id


def _insert_run_row(state_store: StateStore, run_id: UUID, run_date: date) -> None:
    """Record the archive's run as a finished deterministic pipeline."""
    seed_run(
        state_store,
        run_id,
        run_date,
        started_at=datetime(2026, 8, 10, 18, 30, tzinfo=UTC),
    )


def _candidate(symbol: str = "AAPL", rank: int = 1) -> Candidate:
    return Candidate(
        symbol,
        date(2026, 7, 20),
        ("trend_sma",),
        {"close": 100.0, "score": 0.5},
        rank,
    )


def _insert_legacy_risk_assessment(
    state_store: StateStore, run_id: UUID, symbol: str, max_shares: int
) -> None:
    """Insert a pre-#348 style `risk_assessments` row with a real share count.

    `record_risk_assessments` always writes `NULL` for the sizing columns
    now (Issue #348), so a row carrying a non-NULL `max_shares` can only be
    reproduced by writing straight to the table -- exactly what an
    un-rewritten pre-#348 archive still looks like (Issue #385).
    """
    with state_store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO risk_assessments (run_id, symbol, status, max_shares, "
            "reasons_json, warnings_json) VALUES (?, ?, 'approved', ?, '[]', '[]')",
            [str(run_id), symbol, max_shares],
        )


def _populate(state_store: StateStore) -> UUID:
    """Example 1's shape: 1 run with 2 candidates and 1 rejection."""
    run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
    state_store.record_screening_results(
        ScreeningResult(
            candidates=[_candidate("AAPL", 1), _candidate("MSFT", 2)],
            rejections=[
                RejectionRecord(
                    symbol="JPM",
                    stage=RejectionStage.TECHNICAL_SIGNAL,
                    reason_code=RejectionReasonCode.SIGNAL_TREND_NOT_MET,
                    detail={"rsi14": 70.0},
                )
            ],
            truncated=[],
        ),
        ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
    )
    state_store.record_risk_assessments(
        [
            RiskAssessment(
                symbol="AAPL",
                status="approved",
                entry_price=100.0,
                limit_price=101.0,
                stop_price=95.0,
                atr14=2.0,
                stop_distance_pct=(101.0 - 95.0) / 101.0,
                reasons=(),
            )
        ],
        run_id,
    )
    return run_id


def _snapshot(database: Database) -> dict[str, list[tuple[str, ...]]]:
    with database.connect() as conn:
        return {
            table: sorted(
                tuple(str(value) for value in row)
                for row in conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            )
            for table in _TABLES
        }


class TestRuns:
    def test_populated_db_shows_run_summary_row(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)

        main(["runs", "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        assert str(run_id) in output
        assert "2026-07-20" in output
        # Example 1: 2 candidates, 1 rejection.
        assert "2" in output
        assert "1" in output

    def test_empty_db_shows_no_record_message_without_exception(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["runs", "--db", _db_path(state_store)])

        assert "記録なし" in capsys.readouterr().out

    def test_limit_greater_than_actual_rows_shows_all_without_error(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)

        main(["runs", "--limit", "50", "--db", _db_path(state_store)])

        assert "記録なし" not in capsys.readouterr().out


class TestRunDetail:
    def test_known_run_id_shows_candidates_and_risk(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)

        main(["run", "--run-id", str(run_id), "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        assert "AAPL" in output
        assert "approved" in output
        assert "trade_risk" not in output

    def test_a_pre_348_rows_share_count_never_appears_in_the_risk_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Issue #385: `max_shares` is a compat column with zero readers now."""
        run_id = _populate(state_store)
        _insert_legacy_risk_assessment(state_store, run_id, "MSFT", 17)

        main(["run", "--run-id", str(run_id), "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        assert "Shares" not in output
        # Scoped to the row itself: the header prints a random run UUID, whose
        # hex happens to contain "17" about 9% of the time.
        risk_rows = [line for line in output.splitlines() if "MSFT" in line]
        assert risk_rows
        assert all("17" not in line for line in risk_rows)

    def test_unknown_run_id_exits_nonzero_without_traceback(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "run",
                    "--run-id",
                    "nonexistent-id",
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err

    def test_syntactically_valid_but_unrecorded_run_id_exits_nonzero(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "run",
                    "--run-id",
                    str(uuid4()),
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err


class TestSymbol:
    def test_known_symbol_shows_its_candidacy(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)

        main(["symbol", "AAPL", "--db", _db_path(state_store)])

        assert "AAPL" in capsys.readouterr().out

    def test_symbol_never_a_candidate_shows_no_record_message_exit_zero(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)

        main(["symbol", "ZZZZ", "--db", _db_path(state_store)])

        assert "ZZZZの記録はありません" in capsys.readouterr().out

    def test_empty_db_shows_no_record_message_without_exception(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["symbol", "AAPL", "--db", _db_path(state_store)])

        assert "AAPLの記録はありません" in capsys.readouterr().out

    def test_symbol_is_case_normalized(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)

        main(["symbol", "aapl", "--db", _db_path(state_store)])

        assert "AAPLの記録はありません" not in capsys.readouterr().out


class TestRejections:
    def test_known_run_id_shows_rejections_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)

        main(["rejections", "--run-id", str(run_id), "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        assert "JPM" in output
        assert "SIGNAL_TREND_NOT_MET" in output

    def test_unknown_run_id_exits_nonzero_without_traceback(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "rejections",
                    "--run-id",
                    str(uuid4()),
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err

    def test_run_with_zero_rejections_shows_no_record_message(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")

        main(["rejections", "--run-id", str(run_id), "--db", _db_path(state_store)])

        assert "記録なし" in capsys.readouterr().out


class TestIncomplete:
    """Issue #129: surface runs whose analysis phase never finished."""

    def test_actionable_gap_is_listed_and_exits_with_the_agreed_code(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_id = _write_run_archive(tmp_path, date(2026, 8, 10), has_result=False)
        _insert_run_row(state_store, run_id, date(2026, 8, 10))

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "incomplete",
                    "--reports-dir",
                    str(tmp_path / "reports"),
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert exc_info.value.code == ANALYSIS_INCOMPLETE_EXIT_CODE
        output = capsys.readouterr().out
        assert str(run_id) in output
        assert "分析未完" in output

    def test_all_runs_finished_prints_the_clear_message_and_exits_zero(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_id = _write_run_archive(tmp_path, date(2026, 8, 11), has_result=True)
        _insert_run_row(state_store, run_id, date(2026, 8, 11))

        main(
            [
                "incomplete",
                "--reports-dir",
                str(tmp_path / "reports"),
                "--db",
                _db_path(state_store),
            ]
        )

        assert "分析フェーズ未完のrunはありません" in capsys.readouterr().out

    def test_same_day_duplicate_is_listed_without_raising_the_exit_code(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        finished = _write_run_archive(tmp_path, date(2026, 8, 3), has_result=True)
        leftover = _write_run_archive(tmp_path, date(2026, 8, 3), has_result=False)
        _insert_run_row(state_store, finished, date(2026, 8, 3))
        _insert_run_row(state_store, leftover, date(2026, 8, 3))

        main(
            [
                "incomplete",
                "--reports-dir",
                str(tmp_path / "reports"),
                "--db",
                _db_path(state_store),
            ]
        )

        output = capsys.readouterr().out
        assert str(leftover) in output
        assert "同日重複" in output
        assert "対処が必要な未完runはありません" in output

    def test_a_replay_stamped_run_is_listed_without_raising_the_exit_code(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Issue #254: regenerating a report with `copilot-daily --as-of`
        # leaves an export nobody owed an answer to. Without reading the
        # stamp this command would keep returning exit 3 for that directory
        # forever, while the daily preflight stayed silent about it.
        replayed = _write_run_archive(tmp_path, date(2026, 8, 14), has_result=False)
        directory = tmp_path / "reports" / "2026-08-14" / str(replayed)
        (directory / "historical_replay.json").write_text("{}", encoding="utf-8")
        _insert_run_row(state_store, replayed, date(2026, 8, 14))

        main(
            [
                "incomplete",
                "--reports-dir",
                str(tmp_path / "reports"),
                "--db",
                _db_path(state_store),
            ]
        )

        output = capsys.readouterr().out
        assert str(replayed) in output
        assert "リプレイ" in output
        assert "対処が必要な未完runはありません" in output

    def test_since_narrows_the_window_to_the_dates_that_still_matter(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stale = _write_run_archive(tmp_path, date(2026, 8, 3), has_result=False)
        _insert_run_row(state_store, stale, date(2026, 8, 3))

        main(
            [
                "incomplete",
                "--since",
                "2026-08-10",
                "--reports-dir",
                str(tmp_path / "reports"),
                "--db",
                _db_path(state_store),
            ]
        )

        assert "分析フェーズ未完のrunはありません" in capsys.readouterr().out


class TestReadOnly:
    """REQ-007: no `copilot-history` subcommand may mutate any table."""

    def _assert_no_mutation(self, state_store: StateStore, argv: list[str]) -> None:
        before = _snapshot(state_store._database)  # noqa: SLF001
        main([*argv, "--db", _db_path(state_store)])
        after = _snapshot(state_store._database)  # noqa: SLF001
        assert before == after

    def test_runs_does_not_mutate_any_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)
        self._assert_no_mutation(state_store, ["runs"])
        capsys.readouterr()  # drain, keep output out of the test log

    def test_run_detail_does_not_mutate_any_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)
        self._assert_no_mutation(state_store, ["run", "--run-id", str(run_id)])
        capsys.readouterr()

    def test_symbol_does_not_mutate_any_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)
        self._assert_no_mutation(state_store, ["symbol", "AAPL"])
        capsys.readouterr()

    def test_rejections_does_not_mutate_any_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)
        self._assert_no_mutation(state_store, ["rejections", "--run-id", str(run_id)])
        capsys.readouterr()

    def test_incomplete_does_not_mutate_any_table(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Exercised through the exit-3 path, where the scan does the most
        # work, to prove even that branch stays `SELECT`-only (REQ-007).
        run_id = _write_run_archive(tmp_path, date(2026, 8, 10), has_result=False)
        _insert_run_row(state_store, run_id, date(2026, 8, 10))
        before = _snapshot(state_store._database)  # noqa: SLF001

        with pytest.raises(SystemExit):
            main(
                [
                    "incomplete",
                    "--reports-dir",
                    str(tmp_path / "reports"),
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert _snapshot(state_store._database) == before  # noqa: SLF001
        capsys.readouterr()
