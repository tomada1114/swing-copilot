"""History view model: faceting, `recommendation` stratification, ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swing_copilot.dashboard import queries
from swing_copilot.dashboard.viewmodels import history as history_vm

if TYPE_CHECKING:
    from swing_copilot.dashboard.models import ClassificationPanel, HistoryView
from tests.dashboard.conftest import PRIOR_RUN_DATE, PRIOR_RUN_ID, Builder, Fixture


def build(fixture: Fixture) -> HistoryView:
    return history_vm.build_history(
        history_vm.HistorySources(
            scorecard=queries.scorecard(fixture.db_path),
            regime=queries.regime_snapshots(fixture.db_path),
            positions=queries.tracked_positions(fixture.db_path),
        )
    )


def panel_for(
    view: HistoryView, recommendation: str, horizon_days: int
) -> ClassificationPanel:
    return next(
        panel
        for panel in view.panels
        if panel.recommendation == recommendation and panel.horizon_days == horizon_days
    )


class TestClassificationFacets:
    def test_proceed_and_skip_never_share_a_facet(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        # Issue #190 shadow-tracks skip as the counterfactual of proceed;
        # pooling them would average a decision with its own control group.
        builder.run()
        builder.candidate("AAPL", rank=1)
        builder.candidate("MSFT", rank=2)
        builder.verdict("AAPL", recommendation="proceed")
        builder.verdict("MSFT", recommendation="skip")
        builder.outcome(
            "AAPL", horizon_days=5, forward_return_pct=4.0, classification="HIT"
        )
        builder.outcome(
            "MSFT",
            horizon_days=5,
            forward_return_pct=-8.0,
            classification="MISS_SEVERE",
            recommendation="skip",
        )

        view = build(dashboard_db)

        assert {(p.recommendation, p.horizon_days) for p in view.panels} == {
            ("proceed", 5),
            ("skip", 5),
        }
        assert panel_for(view, "proceed", 5).bars[0].counts == (("HIT", 1),)
        assert panel_for(view, "skip", 5).bars[0].counts == (("MISS_SEVERE", 1),)

    def test_each_matured_horizon_gets_its_own_facet(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL")
        builder.outcome(
            "AAPL", horizon_days=5, forward_return_pct=1.0, classification="NEUTRAL"
        )
        builder.outcome(
            "AAPL", horizon_days=20, forward_return_pct=-3.0, classification="MISS_MILD"
        )

        view = build(dashboard_db)

        assert [panel.horizon_days for panel in view.panels] == [5, 20]

    def test_counts_are_stacked_in_severity_order(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        for index, (symbol, classification) in enumerate(
            (("A", "MISS_SEVERE"), ("B", "HIT"), ("C", "NEUTRAL"))
        ):
            builder.candidate(symbol, rank=index + 1)
            builder.verdict(symbol)
            builder.outcome(
                symbol,
                horizon_days=5,
                forward_return_pct=0.5,
                classification=classification,
            )

        bar = panel_for(build(dashboard_db), "proceed", 5).bars[0]

        assert bar.counts == (("HIT", 1), ("NEUTRAL", 1), ("MISS_SEVERE", 1))
        assert bar.total == 3

    def test_an_immature_verdict_contributes_no_bar(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL")

        assert build(dashboard_db).panels == ()


class TestRegimeTimeline:
    def test_points_are_ordered_oldest_first(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.regime(dd_level="HIGH", vix_close=22.0)
        prior = builder.for_run(PRIOR_RUN_ID)
        prior.run(run_date=PRIOR_RUN_DATE)
        prior.regime(dd_level="CALM", vix_close=13.0, as_of=PRIOR_RUN_DATE)

        points = build(dashboard_db).regime_points

        assert [point.run_date for point in points] == [
            PRIOR_RUN_DATE,
            points[1].run_date,
        ]
        assert [point.dd_level for point in points] == ["CALM", "HIGH"]
        assert points[0].vix_close == 13.0

    def test_a_run_without_a_vix_reading_keeps_its_point(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.regime(vix_close=None)

        points = build(dashboard_db).regime_points

        assert len(points) == 1
        assert points[0].vix_close is None


class TestLedger:
    def test_open_positions_list_proceed_before_skip(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.verdict("AAA", recommendation="skip")
        builder.verdict("ZZZ", recommendation="proceed")
        builder.position("AAA", recommendation="skip")
        builder.position("ZZZ", recommendation="proceed")

        rows = build(dashboard_db).open_positions

        assert [row.symbol for row in rows] == ["ZZZ", "AAA"]
        assert rows[0].entry_date.text == "2027-03-01", "a date column carries no time"
        assert rows[0].run_date.text == "2027-03-01"
        assert rows[0].entry_price.text == "101.25"
        assert rows[1].recommendation.text == "skip"

    def test_a_closed_position_is_not_listed_as_open(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.verdict("AAPL")
        builder.position(
            "AAPL", status="closed", exit_reason="stop", realized_return_pct=-2.0
        )

        assert build(dashboard_db).open_positions == ()

    def test_realized_results_are_summarized_per_side(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        for symbol, side, realized in (
            ("AAA", "proceed", 6.0),
            ("BBB", "proceed", -2.0),
            ("CCC", "skip", -10.0),
        ):
            builder.verdict(symbol, recommendation=side)
            builder.position(
                symbol,
                recommendation=side,
                status="closed",
                exit_reason="stop",
                realized_return_pct=realized,
            )

        summaries = {s.recommendation: s for s in build(dashboard_db).closed_summaries}

        assert (summaries["proceed"].closed, summaries["proceed"].wins) == (2, 1)
        assert summaries["proceed"].win_rate.text == "50.0%"
        assert summaries["proceed"].mean_return.text == "+2.00%"
        assert summaries["skip"].closed == 1
        assert summaries["skip"].median_return.text == "-10.00%"

    def test_a_side_with_no_closed_position_reports_absence_not_zero(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.verdict("AAPL")
        builder.position("AAPL", status="open")

        summaries = {s.recommendation: s for s in build(dashboard_db).closed_summaries}

        assert summaries["skip"].closed == 0
        assert summaries["skip"].win_rate.absence == "none"
        assert summaries["skip"].mean_return.absence == "none"
