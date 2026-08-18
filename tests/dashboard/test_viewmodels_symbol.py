"""Symbol-detail view model: reasons, technicals, tracking, NULL meanings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swing_copilot.dashboard import queries
from swing_copilot.dashboard.viewmodels import common
from swing_copilot.dashboard.viewmodels import symbol as symbol_vm

if TYPE_CHECKING:
    from swing_copilot.dashboard.formatting import Cell
    from swing_copilot.dashboard.models import SymbolDetail
from tests.dashboard.conftest import RUN_ID, Builder, Fixture


def build_optional(fixture: Fixture, symbol: str) -> SymbolDetail | None:
    run_id = str(RUN_ID)
    runs = common.run_refs(queries.runs(fixture.db_path))
    return symbol_vm.build_symbol_detail(
        symbol_vm.SymbolSources(
            run=runs[0],
            symbol=symbol,
            candidates=queries.candidates_for_run(fixture.db_path, run_id),
            scorecard=queries.scorecard_for_run(fixture.db_path, run_id),
            reasons=queries.reasons_for_symbol(fixture.db_path, run_id, symbol),
            positions=queries.tracked_positions(fixture.db_path),
        )
    )


def build(fixture: Fixture, symbol: str) -> SymbolDetail:
    view = build_optional(fixture, symbol)
    assert view is not None
    return view


def stat(view: SymbolDetail, label: str) -> Cell:
    for group in (view.score_components, view.technicals, view.execution, view.risk):
        for item in group:
            if item.label == label:
                return item.value
    message = f"no stat labelled {label!r}"
    raise AssertionError(message)


class TestPresence:
    def test_a_symbol_this_run_never_saw_has_no_page(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")

        assert build_optional(dashboard_db, "ZZZZ") is None

    def test_a_verdict_without_a_candidate_row_still_has_a_page(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.verdict("MSFT", recommendation="skip")

        view = build(dashboard_db, "MSFT")

        assert view.verdict.text == "skip"
        assert view.strategy_key == "default"


class TestReasons:
    def test_reasons_are_ordered_and_carry_their_basis_and_citation_count(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL")
        builder.reason("AAPL", index=1, text="二番目", basis="news", source_id_count=2)
        builder.reason("AAPL", index=0, text="一番目", basis="filing")

        view = build(dashboard_db, "AAPL")

        assert [reason.text for reason in view.reasons] == ["一番目", "二番目"]
        assert view.reasons[1].basis.text == "news"
        assert view.reasons[1].source_id_count.text == "2"

    def test_a_reason_written_before_the_basis_tag_says_so(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        # Issue #191 added `basis`; NULL means "written before the tag", not
        # "the analysis chose no basis".
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL")
        builder.reason("AAPL", index=0, text="根拠", basis=None, source_id_count=0)

        view = build(dashboard_db, "AAPL")

        assert view.reasons[0].basis.absence == "pre_tagging"
        assert view.reasons[0].source_id_count.text == "0"


class TestNullMeanings:
    def test_execution_columns_read_as_unrecorded_not_as_unknown(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.legacy_candidate("AAPL")

        view = build(dashboard_db, "AAPL")

        assert stat(view, "実行状態").absence == "unrecorded"
        assert stat(view, "実行距離").absence == "unrecorded"

    def test_news_supply_reads_as_pre_measurement_not_as_none(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        # Issue #130 introduced the measurement; NULL is "not measured".
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL", news_supply_level=None)

        assert build(dashboard_db, "AAPL").news_supply_level.absence == (
            "pre_measurement"
        )

    def test_recorded_execution_state_is_shown_verbatim(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL", execution_state="READY")

        assert stat(build(dashboard_db, "AAPL"), "実行状態").text == "READY"


class TestTechnicalsAndSector:
    def test_technicals_come_from_the_candidate_metrics(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.universe("AAPL", sector="Information Technology")
        builder.candidate("AAPL")
        builder.verdict("AAPL")

        view = build(dashboard_db, "AAPL")

        assert stat(view, "終値").text == "101.25"
        assert stat(view, "RSI14").text == "41.50"
        assert stat(view, "SMA200").text == "90.00"
        assert stat(view, "平均出来高").text == "1,200,000"
        assert view.gics_sector.text == "Information Technology"

    def test_sector_is_unavailable_until_the_verdict_is_archived(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.universe("AAPL")
        builder.candidate("AAPL")

        assert build(dashboard_db, "AAPL").gics_sector.absence == "not_ingested"


class TestTracking:
    def test_an_untracked_verdict_has_no_panel(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL")

        assert build(dashboard_db, "AAPL").tracking is None

    def test_an_open_position_reports_its_entry_stop_and_state(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL")
        builder.position("AAPL", status="open")

        panel = build(dashboard_db, "AAPL").tracking

        assert panel is not None
        assert (panel.status.text, panel.status.tone) == ("open", "info")
        assert panel.recommendation.text == "proceed"
        labels = {item.label: item.value for item in panel.stats}
        assert labels["建値"].text == "101.25"
        assert labels["ストップ"].text == "95.00"
        assert labels["実現リターン"].absence == "none"

    def test_a_closed_position_reports_its_exit_and_realized_return(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL")
        builder.position(
            "AAPL", status="closed", exit_reason="stop", realized_return_pct=-6.25
        )

        panel = build(dashboard_db, "AAPL").tracking

        assert panel is not None
        assert panel.exit_reason.text == "stop"
        labels = {item.label: item.value for item in panel.stats}
        assert labels["実現リターン"].text == "-6.25%"
        assert labels["実現リターン"].tone == "neg"

    def test_a_skip_position_keeps_its_own_side(
        self, builder: Builder, dashboard_db: Fixture
    ) -> None:
        # Issue #190: skip is shadow-tracked, and the panel must say so
        # rather than implying the position was recommended.
        builder.run()
        builder.candidate("AAPL")
        builder.verdict("AAPL", recommendation="skip")
        builder.position("AAPL", recommendation="skip")

        panel = build(dashboard_db, "AAPL").tracking

        assert panel is not None
        assert panel.recommendation.text == "skip"
