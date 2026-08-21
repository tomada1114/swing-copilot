"""Run-overview view model: scorecard collapse, NULL meanings, grouping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swing_copilot.dashboard import queries
from swing_copilot.dashboard.viewmodels import common
from swing_copilot.dashboard.viewmodels import run as run_vm

if TYPE_CHECKING:
    from swing_copilot.dashboard.models import RunOverview, SymbolRow
from tests.dashboard.conftest import RUN_ID, Builder, Fixture


def build(fixture: Fixture, *, is_analysis_missing: bool = False) -> RunOverview:
    run_id = str(RUN_ID)
    runs = common.run_refs(queries.runs(fixture.db_path))
    return run_vm.build_run_overview(
        run_vm.RunSources(
            run=runs[0],
            regime=queries.regime_snapshots(fixture.db_path),
            candidates=queries.candidates_for_run(fixture.db_path, run_id),
            scorecard=queries.scorecard_for_run(fixture.db_path, run_id),
            rejections=queries.rejections_for_run(fixture.db_path, run_id),
            is_analysis_missing=is_analysis_missing,
        )
    )


def row_for(view: RunOverview, symbol: str) -> SymbolRow:
    return next(row for row in view.rows if row.symbol == symbol)


class TestVerdictAvailability:
    def test_a_candidate_without_a_verdict_reads_as_not_yet_archived(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        # The newest run always looks like this: `verdicts` is written by the
        # NEXT run's retro collect, so "no row" must never render as "skip".
        builder.run()
        builder.candidate("AAPL")

        row = row_for(build(dashboard_db), "AAPL")

        assert row.verdict.text == "verdict未取込"
        assert row.risk_status.text == "verdict未取込"
        assert row.binding_constraint.absence == "not_ingested"
        assert row.outcomes_fallback.absence == "not_ingested"

    def test_an_archived_verdict_renders_its_recommendation_and_risk(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL", recommendation="proceed")
        builder.risk("AAPL", status="rejected", binding_constraint="earnings")

        row = row_for(build(dashboard_db), "AAPL")

        assert (row.verdict.text, row.verdict.tone) == ("proceed", "good")
        assert (row.risk_status.text, row.risk_status.tone) == (
            "rejected",
            "critical",
        )
        assert row.binding_constraint.text == "earnings"

    def test_a_verdict_without_a_candidate_row_still_appears(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        # A verdict whose candidate row was written under a different
        # strategy key must not vanish from the overview.
        builder.run()
        builder.verdict("MSFT", recommendation="skip")

        view = build(dashboard_db)

        assert [row.symbol for row in view.rows] == ["MSFT"]
        assert row_for(view, "MSFT").is_candidate is False
        assert row_for(view, "MSFT").rank.absence == "absent"


class TestScorecardCollapse:
    def test_two_matured_horizons_collapse_into_one_row_with_two_outcomes(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL")
        builder.outcome(
            "AAPL", horizon_days=5, forward_return_pct=3.2, classification="HIT"
        )
        builder.outcome(
            "AAPL",
            horizon_days=20,
            forward_return_pct=-4.5,
            classification="MISS_SEVERE",
        )

        view = build(dashboard_db)
        row = row_for(view, "AAPL")

        assert len(view.rows) == 1, "the (verdict x horizon) grain must collapse"
        assert [outcome.horizon_days for outcome in row.outcomes] == [5, 20]
        assert row.outcomes[0].classification.tone == "good"
        assert row.outcomes[0].forward_return.text == "+3.20%"
        assert row.outcomes[1].classification.tone == "critical"
        assert row.outcomes[1].forward_return.text == "-4.50%"

    def test_an_immature_verdict_keeps_one_row_and_no_outcome(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL")

        row = row_for(build(dashboard_db), "AAPL")

        assert row.outcomes == ()
        assert row.outcomes_fallback.absence == "immature"


class TestNullMeanings:
    def test_a_pre_migration_candidate_reports_absent_score_components(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.legacy_candidate("AAPL")

        row = row_for(build(dashboard_db), "AAPL")

        assert row.score.absence == "absent"
        assert {stat.value.absence for stat in row.score_components} == {"absent"}

    def test_a_zero_score_component_is_a_value_not_an_absence(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL", score=0.0)

        row = row_for(build(dashboard_db), "AAPL")

        assert row.score.absence is None
        assert row.score.text == "0.000"


class TestRegimePanel:
    def test_reads_the_snapshot_of_this_run_only(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.regime(gate_verdict="BULL", dd_level="SEVERE", vix_close=21.5)

        panel = build(dashboard_db).regime

        assert panel is not None
        assert (panel.gate.text, panel.gate.tone) == ("BULL", "good")
        assert (panel.dd_level.text, panel.dd_level.tone) == ("SEVERE", "critical")
        labels = {stat.label: stat.value.text for stat in panel.stats}
        assert labels["VIX 終値"] == "21.50"
        assert labels["SPY 15日/5日DD"] == "1 / 0"
        assert labels["終値 vs EMA"] == "+2.67%"

    def test_a_snapshot_predating_the_price_columns_shows_absences(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.legacy_regime()

        panel = build(dashboard_db).regime

        assert panel is not None
        stats = {stat.label: stat.value for stat in panel.stats}
        assert stats["SPY 15日/5日DD"].absence == "no_snapshot"
        assert stats["終値 vs EMA"].absence == "no_snapshot"
        assert stats["VIX 終値"].absence == "no_snapshot"

    def test_a_run_without_a_snapshot_has_no_panel(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")

        assert build(dashboard_db).regime is None


class TestRejectionsAndCounts:
    def test_groups_by_stage_then_by_size(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.rejection(
            "AAA", stage="technical_signal", reason_code="SIGNAL_RSI_NOT_MET"
        )
        builder.rejection(
            "BBB", stage="technical_signal", reason_code="SIGNAL_TREND_NOT_MET"
        )
        builder.rejection(
            "CCC", stage="technical_signal", reason_code="SIGNAL_TREND_NOT_MET"
        )
        builder.rejection(
            "DDD", stage="data_quality", reason_code="DATA_INSUFFICIENT_HISTORY"
        )

        view = build(dashboard_db)

        assert view.rejection_total == 4
        assert [
            (group.stage, group.reason_code, group.count)
            for group in view.rejection_groups
        ] == [
            ("data_quality", "DATA_INSUFFICIENT_HISTORY", 1),
            ("technical_signal", "SIGNAL_TREND_NOT_MET", 2),
            ("technical_signal", "SIGNAL_RSI_NOT_MET", 1),
        ]
        assert view.rejection_groups[1].symbols == ("BBB", "CCC")

    def test_counts_and_no_trade_come_from_archived_verdicts(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL", rank=1)
        builder.candidate("MSFT", rank=2)
        builder.verdict("AAPL", recommendation="proceed")
        builder.verdict("MSFT", recommendation="skip", no_trade=True)

        view = build(dashboard_db)

        assert (view.proceed_count, view.skip_count) == (1, 1)
        assert view.no_trade is True

    def test_analysis_pending_note_is_only_set_when_asked(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()

        assert build(dashboard_db).analysis_pending_note is None
        assert build(dashboard_db, is_analysis_missing=True).analysis_pending_note


class TestStatusBadge:
    def test_degraded_run_is_flagged_as_a_warning(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run(status="degraded")

        view = build(dashboard_db)

        assert (view.status_badge.text, view.status_badge.tone) == (
            "degraded",
            "warning",
        )
