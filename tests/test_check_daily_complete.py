"""Tests for scripts/check_daily_complete.py (offline: an isolated DuckDB file)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from swing_copilot.storage.database import Database
from swing_copilot.storage.state_store import StateStore

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]

RUN_ID = "11111111-1111-4111-8111-111111111111"
OLDER_RUN_ID = "22222222-2222-4222-8222-222222222222"
RUN_DATE = date(2026, 8, 19)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_daily_complete", REPO_ROOT / "scripts" / "check_daily_complete.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_daily_complete = _load_module()


def _insert_run(
    db: Database, run_id: str, run_date: date, started_at: datetime
) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO runs (run_id, run_date, mode, config_hash, status, started_at)
            VALUES (?, ?, 'live', 'hash', 'success', ?)
            """,
            [run_id, run_date, started_at],
        )


def _insert_candidates(db: Database, run_id: str, count: int) -> None:
    with db.connect() as conn:
        for rank in range(1, count + 1):
            conn.execute(
                """
                INSERT INTO candidates
                    (run_id, symbol, strategy_key, rank, signal_names, metrics_json)
                VALUES (?, ?, 'default', ?, [], '{}')
                """,
                [run_id, f"SYM{rank}", rank],
            )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """An initialized, isolated DuckDB file holding one live run."""
    path = tmp_path / "copilot.duckdb"
    database = Database(path)
    StateStore(database).init_schema()
    _insert_run(database, RUN_ID, RUN_DATE, datetime(2026, 8, 19, 23, 50, tzinfo=UTC))
    return path


def _write_result(reports_dir: Path, run_id: str = RUN_ID) -> None:
    run_dir = reports_dir / RUN_DATE.isoformat() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "analysis_result.json").write_text("{}", encoding="utf-8")


class TestCheck:
    def test_passes_when_the_analysis_result_is_there(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        _insert_candidates(Database(db_path), RUN_ID, 10)
        reports_dir = tmp_path / "reports"
        _write_result(reports_dir)

        check_daily_complete.check(reports_dir, db_path)

    def test_fails_when_candidates_exist_but_no_analysis_result(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        _insert_candidates(Database(db_path), RUN_ID, 10)
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        with pytest.raises(check_daily_complete.IncompleteRunError, match="10 件"):
            check_daily_complete.check(reports_dir, db_path)

    def test_passes_when_the_run_produced_no_candidates(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        """No candidates means nothing was owed to the analysis."""
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        check_daily_complete.check(reports_dir, db_path)

    def test_looks_at_the_most_recently_started_run(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        """A completed earlier run must not vouch for the newest one."""
        database = Database(db_path)
        _insert_run(
            database, OLDER_RUN_ID, RUN_DATE, datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
        )
        _insert_candidates(database, OLDER_RUN_ID, 10)
        _insert_candidates(database, RUN_ID, 10)
        reports_dir = tmp_path / "reports"
        _write_result(reports_dir, OLDER_RUN_ID)

        with pytest.raises(check_daily_complete.IncompleteRunError, match=RUN_ID):
            check_daily_complete.check(reports_dir, db_path)


class TestStartedAfter:
    """`--started-after` scopes the check to this job's own run (Issue #370)."""

    def test_passes_when_the_latest_run_started_at_or_after_it(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        _insert_candidates(Database(db_path), RUN_ID, 10)
        reports_dir = tmp_path / "reports"
        _write_result(reports_dir)

        # Exactly equal counts as "at or after" -- not strictly before.
        check_daily_complete.check(
            reports_dir,
            db_path,
            started_after=datetime(2026, 8, 19, 23, 50, tzinfo=UTC),
        )

    def test_raises_when_the_latest_run_started_before_it(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        """A previous day's run must not vouch for a day this job never ran.

        Visible now that reports/ is pulled from R2 at job start.
        """
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        with pytest.raises(
            check_daily_complete.IncompleteRunError,
            match="このジョブ自身の run が無い",
        ):
            check_daily_complete.check(
                reports_dir,
                db_path,
                started_after=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
            )

    def test_omitting_it_preserves_the_candidates_zero_path(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        check_daily_complete.check(reports_dir, db_path)

    def test_omitting_it_preserves_the_analysis_result_present_path(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        _insert_candidates(Database(db_path), RUN_ID, 10)
        reports_dir = tmp_path / "reports"
        _write_result(reports_dir)

        check_daily_complete.check(reports_dir, db_path)


class TestMain:
    def test_returns_one_and_reports_on_stderr_when_incomplete(
        self, db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _insert_candidates(Database(db_path), RUN_ID, 3)
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        exit_code = check_daily_complete.main(
            ["--reports-dir", str(reports_dir), "--db", str(db_path)]
        )

        assert exit_code == 1
        assert "error:" in capsys.readouterr().err

    def test_returns_zero_when_complete(self, db_path: Path, tmp_path: Path) -> None:
        _insert_candidates(Database(db_path), RUN_ID, 3)
        reports_dir = tmp_path / "reports"
        _write_result(reports_dir)

        assert (
            check_daily_complete.main(
                ["--reports-dir", str(reports_dir), "--db", str(db_path)]
            )
            == 0
        )

    def test_returns_zero_when_started_after_precedes_the_latest_run(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        _insert_candidates(Database(db_path), RUN_ID, 3)
        reports_dir = tmp_path / "reports"
        _write_result(reports_dir)

        exit_code = check_daily_complete.main(
            [
                "--reports-dir",
                str(reports_dir),
                "--db",
                str(db_path),
                "--started-after",
                "2026-08-19T00:00:00Z",
            ]
        )

        assert exit_code == 0

    def test_returns_one_when_started_after_excludes_the_latest_run(
        self, db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        exit_code = check_daily_complete.main(
            [
                "--reports-dir",
                str(reports_dir),
                "--db",
                str(db_path),
                "--started-after",
                "2026-08-20T00:00:00Z",
            ]
        )

        assert exit_code == 1
        assert "このジョブ自身の run が無い" in capsys.readouterr().err


class TestOutcomeFile:
    """Issue #372: `--outcome-file` tells "never started" from "legit abort"."""

    def test_missing_outcome_file_fails_even_with_no_started_after(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        """Absent file fails immediately -- independent of what the DB holds."""
        _insert_candidates(Database(db_path), RUN_ID, 3)
        reports_dir = tmp_path / "reports"
        _write_result(reports_dir)
        outcome_file = tmp_path / "outcome.json"

        with pytest.raises(
            check_daily_complete.IncompleteRunError,
            match="copilot-daily が一度も起動していない",
        ):
            check_daily_complete.check(reports_dir, db_path, outcome_file=outcome_file)

    def test_preflight_abort_outcome_passes_without_consulting_the_db(
        self, tmp_path: Path
    ) -> None:
        """No DB at all is fine -- the outcome file alone settles this."""
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        outcome_file = tmp_path / "outcome.json"
        outcome_file.write_text(
            '{"outcome": "preflight_abort", "reason": "no_trading_day"}',
            encoding="utf-8",
        )

        check_daily_complete.check(reports_dir, outcome_file=outcome_file)

    def test_non_abort_outcome_falls_through_to_the_existing_checks(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        _insert_candidates(Database(db_path), RUN_ID, 3)
        reports_dir = tmp_path / "reports"
        _write_result(reports_dir)
        outcome_file = tmp_path / "outcome.json"
        outcome_file.write_text('{"outcome": "success"}', encoding="utf-8")

        check_daily_complete.check(reports_dir, db_path, outcome_file=outcome_file)

    def test_non_abort_outcome_still_fails_when_the_analysis_is_missing(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        _insert_candidates(Database(db_path), RUN_ID, 3)
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        outcome_file = tmp_path / "outcome.json"
        outcome_file.write_text('{"outcome": "degraded"}', encoding="utf-8")

        with pytest.raises(check_daily_complete.IncompleteRunError, match="3 件"):
            check_daily_complete.check(reports_dir, db_path, outcome_file=outcome_file)

    def test_malformed_outcome_file_raises(self, db_path: Path, tmp_path: Path) -> None:
        outcome_file = tmp_path / "outcome.json"
        outcome_file.write_text("not json", encoding="utf-8")

        with pytest.raises(
            check_daily_complete.IncompleteRunError, match="読み込めない"
        ):
            check_daily_complete.check(
                tmp_path / "reports", db_path, outcome_file=outcome_file
            )

    def test_omitting_it_preserves_existing_behavior(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        """The default (`outcome_file=None`) must not change prior behavior."""
        _insert_candidates(Database(db_path), RUN_ID, 3)
        reports_dir = tmp_path / "reports"
        _write_result(reports_dir)

        check_daily_complete.check(reports_dir, db_path)


class TestMainOutcomeFile:
    def test_outcome_file_flag_reaches_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        outcome_file = tmp_path / "outcome.json"
        outcome_file.write_text(
            '{"outcome": "preflight_abort", "reason": "no_trading_day"}',
            encoding="utf-8",
        )

        exit_code = check_daily_complete.main(
            [
                "--reports-dir",
                str(reports_dir),
                "--outcome-file",
                str(outcome_file),
            ]
        )

        assert exit_code == 0
        assert "preflight abort" in capsys.readouterr().out

    def test_missing_outcome_file_flag_target_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        exit_code = check_daily_complete.main(
            [
                "--reports-dir",
                str(reports_dir),
                "--outcome-file",
                str(tmp_path / "missing-outcome.json"),
            ]
        )

        assert exit_code == 1
        assert "一度も起動していない" in capsys.readouterr().err
