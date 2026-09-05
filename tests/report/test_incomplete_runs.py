"""Incomplete-analysis detection contracts (Issue #129).

The regression these tests exist for is not "a missing file is found" but the
two ways a naive predicate gets it wrong: flagging a finished newest run
because `verdicts` rows are always written one run late, and flagging the
leftover of a same-day double start that Issue #118 already handles.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
    HISTORICAL_REPLAY_FILENAME,
)
from swing_copilot.report.incomplete_runs import (
    IncompleteRunKind,
    find_incomplete_runs,
)
from tests.support.runs import seed_run

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.storage.database import Database
    from swing_copilot.storage.state_store import StateStore

#: The two 2026-08-10 / 2026-08-03 shapes the issue reported, plus the
#: 2026-08-11 run that finished normally.
_UNFINISHED = UUID("9455e10b-b1f9-46d9-b45f-2dfdf31bac8e")
_FINISHED = UUID("7afee0e5-cdbb-4aa8-9c1e-7a03d7b0374c")
_SAME_DAY_FIRST = UUID("41ea5618-9c58-46d7-b966-ce5c1b2f467c")
_SAME_DAY_SECOND = UUID("743af6df-2aa5-4190-aa26-fc9768da6de2")


@pytest.fixture
def reports_root(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    return root


def _state_database(state_store: StateStore) -> Database:
    return state_store.database


def _write_run(
    reports_root: Path,
    run_date: date,
    run_id: UUID,
    *,
    has_input: bool = True,
    has_result: bool = False,
) -> Path:
    """Create one `reports/<date>/<run_id>/` archive with the chosen documents.

    The documents' *contents* are deliberately irrelevant: detection is an
    existence check, so that a preflight never has to parse the strict
    analysis schemas to answer "did the analysis phase run at all".
    """
    directory = reports_root / run_date.isoformat() / str(run_id)
    directory.mkdir(parents=True)
    if has_input:
        (directory / ANALYSIS_INPUT_FILENAME).write_text("{}", encoding="utf-8")
    if has_result:
        (directory / ANALYSIS_RESULT_FILENAME).write_text("{}", encoding="utf-8")
    return directory


def _insert_run(
    state_store: StateStore,
    run_id: UUID,
    run_date: date,
    *,
    status: str = "success",
    started_at: datetime | None = None,
) -> None:
    """Insert a minimal `runs` row with an explicitly chosen status."""
    seed_run(
        state_store,
        run_id,
        run_date,
        status=status,
        started_at=started_at or datetime(2026, 8, 10, 18, 30, tzinfo=UTC),
    )


class TestAnalysisMissing:
    def test_successful_run_without_result_is_actionable_analysis_missing(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        # The reported 2026-08-10 shape: copilot-daily succeeded, the skill
        # session died before writing analysis_result.json.
        _write_run(reports_root, date(2026, 8, 10), _UNFINISHED)
        _insert_run(state_store, _UNFINISHED, date(2026, 8, 10))

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert [(run.run_id, run.kind) for run in found] == [
            (_UNFINISHED, IncompleteRunKind.ANALYSIS_MISSING)
        ]
        assert found[0].is_actionable
        assert found[0].run_status == "success"
        assert found[0].path == reports_root / "2026-08-10" / str(_UNFINISHED)

    def test_degraded_run_without_result_is_still_analysis_missing(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        # A degraded run still produced a usable analysis_input.json, so its
        # missing result is the same gap as a successful run's.
        _write_run(reports_root, date(2026, 8, 10), _UNFINISHED)
        _insert_run(state_store, _UNFINISHED, date(2026, 8, 10), status="degraded")

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert found[0].kind is IncompleteRunKind.ANALYSIS_MISSING
        assert found[0].is_actionable


class TestFinishedRunsAreNeverFlagged:
    def test_finished_run_with_zero_verdict_rows_is_not_flagged(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        """The newest finished run must not be a false positive (#129).

        `verdicts` rows are written by `copilot-retro collect` during the
        *next* run, so a run that finished today always has zero of them.
        A row-count predicate would flag this run; the filesystem does not.
        """
        _write_run(reports_root, date(2026, 8, 11), _FINISHED, has_result=True)
        _insert_run(state_store, _FINISHED, date(2026, 8, 11))
        with _state_database(state_store).connect() as conn:
            verdict_count = conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert verdict_count == (0,)
        assert found == ()

    def test_directory_without_analysis_input_is_ignored(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        # The run never reached the analysis phase at all; that failure is
        # `runs.status`'s job to report, not this scan's.
        _write_run(reports_root, date(2026, 8, 10), _UNFINISHED, has_input=False)
        _insert_run(state_store, _UNFINISHED, date(2026, 8, 10), status="failed")

        assert find_incomplete_runs(_state_database(state_store), reports_root) == ()

    def test_missing_reports_root_returns_empty_without_raising(
        self, state_store: StateStore, tmp_path: Path
    ) -> None:
        assert (
            find_incomplete_runs(_state_database(state_store), tmp_path / "absent")
            == ()
        )


class TestNonActionableKinds:
    def test_same_day_completed_sibling_marks_the_run_superseded(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        """The reported 2026-08-03 shape, which Issue #118 now blocks."""
        _write_run(reports_root, date(2026, 8, 3), _SAME_DAY_FIRST, has_result=True)
        _write_run(reports_root, date(2026, 8, 3), _SAME_DAY_SECOND)
        _insert_run(state_store, _SAME_DAY_FIRST, date(2026, 8, 3))
        _insert_run(state_store, _SAME_DAY_SECOND, date(2026, 8, 3))

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert [run.run_id for run in found] == [_SAME_DAY_SECOND]
        assert found[0].kind is IncompleteRunKind.SAME_DAY_SUPERSEDED
        assert found[0].completed_sibling_run_id == _SAME_DAY_FIRST
        assert not found[0].is_actionable

    @pytest.mark.parametrize("status", ["failed", "running"])
    def test_unfinished_pipeline_is_listed_but_not_actionable(
        self, state_store: StateStore, reports_root: Path, status: str
    ) -> None:
        _write_run(reports_root, date(2026, 8, 10), _UNFINISHED)
        _insert_run(state_store, _UNFINISHED, date(2026, 8, 10), status=status)

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert found[0].kind is IncompleteRunKind.PIPELINE_UNFINISHED
        assert found[0].run_status == status
        assert not found[0].is_actionable

    def test_completed_sibling_wins_over_a_failed_status(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        # Precedence: whatever this run's own status says, that date's
        # analysis is not missing, so it must not raise the alarm.
        _write_run(reports_root, date(2026, 8, 3), _SAME_DAY_FIRST, has_result=True)
        _write_run(reports_root, date(2026, 8, 3), _SAME_DAY_SECOND)
        _insert_run(state_store, _SAME_DAY_SECOND, date(2026, 8, 3), status="failed")

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert found[0].kind is IncompleteRunKind.SAME_DAY_SUPERSEDED
        assert not found[0].is_actionable

    def test_a_replay_stamped_directory_is_never_a_gap(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        # Issue #254: a `--as-of` replay stamps the export it writes. Nobody
        # was going to answer it, so the missing result is the expected state
        # -- and the daily preflight, this CLI, and the dashboard banner all
        # have to agree about that, which is why the stamp is read here.
        directory = _write_run(reports_root, date(2026, 8, 14), _UNFINISHED)
        (directory / HISTORICAL_REPLAY_FILENAME).write_text("{}", encoding="utf-8")
        _insert_run(state_store, _UNFINISHED, date(2026, 8, 14))

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert found[0].kind is IncompleteRunKind.HISTORICAL_REPLAY
        assert not found[0].is_actionable

    def test_a_replay_stamp_wins_over_a_completed_sibling(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        # Both reasons are non-actionable, but the stamp is a fact about this
        # directory rather than about the date, so it is the precise one.
        _write_run(reports_root, date(2026, 8, 3), _SAME_DAY_FIRST, has_result=True)
        directory = _write_run(reports_root, date(2026, 8, 3), _SAME_DAY_SECOND)
        (directory / HISTORICAL_REPLAY_FILENAME).write_text("{}", encoding="utf-8")
        _insert_run(state_store, _SAME_DAY_FIRST, date(2026, 8, 3))
        _insert_run(state_store, _SAME_DAY_SECOND, date(2026, 8, 3))

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert [run.run_id for run in found] == [_SAME_DAY_SECOND]
        assert found[0].kind is IncompleteRunKind.HISTORICAL_REPLAY
        assert not found[0].is_actionable


class TestDatabaseDivergence:
    def test_run_directory_without_a_runs_row_is_reported_as_divergence(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        _write_run(reports_root, date(2026, 8, 10), _UNFINISHED)

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert found[0].kind is IncompleteRunKind.RUN_ROW_MISSING
        assert found[0].run_status is None
        assert found[0].started_at is None
        assert found[0].is_actionable


class TestSinceBoundary:
    """`--since` is inclusive, matching the repo's `as_of` cutoff discipline."""

    @pytest.mark.parametrize(
        ("run_date", "is_included"),
        [
            pytest.param(date(2026, 8, 9), False, id="before-cutoff"),
            pytest.param(date(2026, 8, 10), True, id="exactly-at-cutoff"),
            pytest.param(date(2026, 8, 11), True, id="after-cutoff"),
        ],
    )
    def test_cutoff_boundary_is_inclusive(
        self,
        state_store: StateStore,
        reports_root: Path,
        run_date: date,
        is_included: bool,
    ) -> None:
        _write_run(reports_root, run_date, _UNFINISHED)
        _insert_run(state_store, _UNFINISHED, run_date)

        found = find_incomplete_runs(
            _state_database(state_store), reports_root, since=date(2026, 8, 10)
        )

        assert bool(found) is is_included

    def test_excluded_older_run_does_not_hide_a_newer_one(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        _write_run(reports_root, date(2026, 8, 3), _SAME_DAY_SECOND)
        _write_run(reports_root, date(2026, 8, 10), _UNFINISHED)
        _insert_run(state_store, _SAME_DAY_SECOND, date(2026, 8, 3))
        _insert_run(state_store, _UNFINISHED, date(2026, 8, 10))

        found = find_incomplete_runs(
            _state_database(state_store), reports_root, since=date(2026, 8, 10)
        )

        assert [run.run_id for run in found] == [_UNFINISHED]


class TestOrdering:
    def test_newest_run_date_first_then_latest_started_at(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        earlier = UUID("00000000-0000-4000-8000-000000000001")
        later = UUID("00000000-0000-4000-8000-000000000002")
        _write_run(reports_root, date(2026, 8, 3), _SAME_DAY_SECOND)
        _write_run(reports_root, date(2026, 8, 10), earlier)
        _write_run(reports_root, date(2026, 8, 10), later)
        _insert_run(state_store, _SAME_DAY_SECOND, date(2026, 8, 3))
        _insert_run(
            state_store,
            earlier,
            date(2026, 8, 10),
            started_at=datetime(2026, 8, 10, 15, 5, tzinfo=UTC),
        )
        _insert_run(
            state_store,
            later,
            date(2026, 8, 10),
            started_at=datetime(2026, 8, 10, 18, 30, tzinfo=UTC),
        )

        found = find_incomplete_runs(_state_database(state_store), reports_root)

        assert [run.run_id for run in found] == [later, earlier, _SAME_DAY_SECOND]
