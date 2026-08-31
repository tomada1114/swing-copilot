"""The data-access layer: narrowing, ordering, and the read-only contract."""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

import swing_copilot.dashboard
from swing_copilot.dashboard import queries
from swing_copilot.research import ResearchError
from swing_copilot.storage.verdict_records import ACCOUNT_INDEPENDENT_EXPORT_SINCE
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


class TestReasonsForSymbol:
    """Issue #385: pre-#352 reason text may describe a reader's account."""

    @pytest.mark.parametrize(
        ("run_date", "is_visible"),
        [
            pytest.param(date(2026, 8, 20), False, id="day_before_cutoff"),
            pytest.param(date(2026, 8, 21), True, id="exactly_at_cutoff"),
            pytest.param(date(2026, 8, 22), True, id="day_after_cutoff"),
        ],
    )
    def test_the_account_dependent_cutoff_is_inclusive(
        self,
        builder: Builder,
        dashboard_db: Fixture,
        run_date: date,
        is_visible: bool,
    ) -> None:
        builder.run(run_date=run_date)
        builder.reason("AAPL", index=0, text="最終株数17株はこの制約下での結果である")

        frame = queries.reasons_for_symbol(dashboard_db.db_path, str(RUN_ID), "AAPL")

        assert frame.empty is not is_visible

    def test_an_as_of_replay_of_a_pre_cutoff_date_still_shows_its_reasons(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        """Issue #389: `run_date` is the replayed date, `started_at` is real wall time."""
        builder.run(
            run_date=date(2026, 5, 1),
            started_at=ACCOUNT_INDEPENDENT_EXPORT_SINCE + timedelta(days=10),
        )
        builder.reason("AAPL", index=0, text="最終株数17株はこの制約下での結果である")

        frame = queries.reasons_for_symbol(dashboard_db.db_path, str(RUN_ID), "AAPL")

        assert not frame.empty

    @pytest.mark.parametrize(
        ("started_at", "is_visible"),
        [
            pytest.param(
                ACCOUNT_INDEPENDENT_EXPORT_SINCE - timedelta(seconds=1),
                False,
                id="one_second_before_export_since",
            ),
            pytest.param(
                ACCOUNT_INDEPENDENT_EXPORT_SINCE, True, id="exactly_export_since"
            ),
            pytest.param(
                ACCOUNT_INDEPENDENT_EXPORT_SINCE + timedelta(seconds=1),
                True,
                id="one_second_after_export_since",
            ),
        ],
    )
    def test_the_started_at_boundary_is_inclusive(
        self,
        builder: Builder,
        dashboard_db: Fixture,
        started_at: datetime,
        is_visible: bool,
    ) -> None:
        """Isolates the `started_at` term of the predicate.

        `run_date` alone (2026-08-20) stays before the cutoff throughout, so
        only `started_at` can make a difference here.
        """
        builder.run(run_date=date(2026, 8, 20), started_at=started_at)
        builder.reason("AAPL", index=0, text="決算プロキシミティを理由に見送り")

        frame = queries.reasons_for_symbol(dashboard_db.db_path, str(RUN_ID), "AAPL")

        assert frame.empty is not is_visible


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

    def test_a_replay_stamped_run_is_not_reported_as_analysis_missing(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        # Issue #254: a `--as-of` replay stamps its own export. No skill
        # session owed it an answer, so a banner would be permanent noise --
        # and `copilot-history incomplete` and the daily preflight read the
        # same stamp, so all three stay consistent.
        builder.run()
        directory = write_run_archive(dashboard_db.reports_root, has_result=False)
        (directory / "historical_replay.json").write_text("{}", encoding="utf-8")

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
