"""The data-access layer: narrowing, ordering, and the read-only contract."""

from __future__ import annotations

import ast
from pathlib import Path

import duckdb
import pytest

import swing_copilot.dashboard
from swing_copilot.dashboard import queries
from swing_copilot.research import ResearchError
from tests.dashboard.conftest import (
    PRIOR_RUN_DATE,
    PRIOR_RUN_ID,
    RUN_ID,
    Builder,
    Fixture,
    write_run_archive,
)

#: Anchored to the installed package rather than to the working
#: directory, so the read-only contract is checked wherever pytest runs.
DASHBOARD_SOURCE = Path(swing_copilot.dashboard.__file__).parent


class TestNarrowing:
    def test_runs_are_returned_newest_first(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.for_run(PRIOR_RUN_ID).run(run_date=PRIOR_RUN_DATE)

        frame = queries.runs(dashboard_db.db_path)

        assert [str(value) for value in frame["run_id"]] == [
            str(RUN_ID),
            str(PRIOR_RUN_ID),
        ]

    def test_candidates_are_limited_to_the_requested_run(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        prior = builder.for_run(PRIOR_RUN_ID)
        prior.run(run_date=PRIOR_RUN_DATE)
        prior.candidate("MSFT")

        frame = queries.candidates_for_run(dashboard_db.db_path, str(RUN_ID))

        assert list(frame["symbol"]) == ["AAPL"]

    def test_rejections_are_limited_to_the_requested_run(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.rejection(
            "AAA", stage="data_quality", reason_code="DATA_MISSING_NET_INCOME"
        )
        prior = builder.for_run(PRIOR_RUN_ID)
        prior.run(run_date=PRIOR_RUN_DATE)
        prior.rejection(
            "BBB", stage="data_quality", reason_code="DATA_MISSING_NET_INCOME"
        )

        frame = queries.rejections_for_run(dashboard_db.db_path, str(RUN_ID))

        assert list(frame["symbol"]) == ["AAA"]

    def test_an_empty_database_yields_empty_frames(self, dashboard_db: Fixture) -> None:
        assert queries.runs(dashboard_db.db_path).empty
        assert queries.rejections_for_run(dashboard_db.db_path, str(RUN_ID)).empty


class TestIncompleteRuns:
    def test_reports_only_the_run_whose_analysis_never_finished(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.for_run(PRIOR_RUN_ID).run(run_date=PRIOR_RUN_DATE)
        write_run_archive(dashboard_db.reports_root, has_result=False)
        write_run_archive(
            dashboard_db.reports_root,
            run_id=PRIOR_RUN_ID,
            run_date=PRIOR_RUN_DATE,
            has_result=True,
        )

        missing = queries.analysis_missing_run_ids(
            dashboard_db.db_path, dashboard_db.reports_root
        )

        assert missing == frozenset({str(RUN_ID)})

    def test_a_failed_run_is_not_reported_as_analysis_missing(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        # `runs.status` already says the pipeline stopped early; a second
        # banner saying "analysis missing" would only be noise.
        builder.run(status="failed")
        write_run_archive(dashboard_db.reports_root, has_result=False)

        missing = queries.analysis_missing_run_ids(
            dashboard_db.db_path, dashboard_db.reports_root
        )

        assert missing == frozenset()

    def test_a_missing_reports_tree_yields_nothing(self, dashboard_db: Fixture) -> None:
        assert (
            queries.analysis_missing_run_ids(
                dashboard_db.db_path, dashboard_db.reports_root / "absent"
            )
            == frozenset()
        )

    def test_an_absent_database_yields_nothing_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        reports_root = tmp_path / "reports"
        write_run_archive(reports_root, has_result=False)

        assert (
            queries.analysis_missing_run_ids(tmp_path / "absent.duckdb", reports_root)
            == frozenset()
        )

    def test_an_unreadable_database_yields_nothing_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        # A file that exists but has no `runs` table: the banner is optional
        # information, so it degrades instead of taking the page down.
        broken = tmp_path / "broken.duckdb"
        with duckdb.connect(str(broken)) as connection:
            connection.execute("CREATE TABLE placeholder (x INTEGER)")
        reports_root = tmp_path / "reports"
        write_run_archive(reports_root, has_result=False)

        assert queries.analysis_missing_run_ids(broken, reports_root) == frozenset()


class TestReadOnlyContract:
    def test_a_missing_database_raises_a_research_error(self, tmp_path: Path) -> None:
        with pytest.raises(ResearchError, match="database file not found"):
            queries.runs(tmp_path / "absent.duckdb")

    def test_no_dashboard_module_calls_ensure_views_or_opens_duckdb(self) -> None:
        """The invariant the whole design rests on, checked structurally.

        `ensure_views()` opens a read-write connection to run DDL, and a raw
        `duckdb.connect` bypasses `research`'s open-query-close discipline.
        Either one can take DuckDB's exclusive file lock and fail the
        unattended 18:30 run, so neither may appear anywhere under
        `dashboard/`.
        """
        offenders: list[str] = []
        for source_path in DASHBOARD_SOURCE.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                if name.endswith(("ensure_views", "duckdb.connect")):
                    offenders.append(f"{source_path}:{node.lineno} {name}")
        assert offenders == [], "read-only violation: " + ", ".join(offenders)

    def test_only_read_only_database_handles_are_constructed(self) -> None:
        """Any `Database(...)` the dashboard builds must pass `read_only=True`."""
        offenders: list[str] = []
        for source_path in DASHBOARD_SOURCE.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                is_database = (
                    isinstance(node.func, ast.Name) and node.func.id == "Database"
                )
                if is_database and not any(
                    keyword.arg == "read_only" for keyword in node.keywords
                ):
                    offenders.append(f"{source_path}:{node.lineno}")
        assert offenders == [], "read-write Database in the dashboard: " + ", ".join(
            offenders
        )
