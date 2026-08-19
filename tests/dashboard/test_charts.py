"""Server-rendered SVG: geometry, escaping, and the no-external-asset rule."""

from __future__ import annotations

import re
from datetime import date

import pytest

from swing_copilot.dashboard import charts
from swing_copilot.dashboard.models import (
    ClassificationBar,
    ClassificationPanel,
    RegimePoint,
)


def make_panel(*bars: ClassificationBar) -> ClassificationPanel:
    return ClassificationPanel(
        recommendation="proceed",
        horizon_days=5,
        bars=bars,
        total=sum(bar.total for bar in bars),
    )


BAR = ClassificationBar(
    run_date=date(2027, 3, 1),
    counts=(("HIT", 2), ("MISS_SEVERE", 1)),
    total=3,
)


class TestClassificationChart:
    def test_carries_an_intrinsic_size_so_a_small_facet_is_not_blown_up(self) -> None:
        # A two-bar facet stretched to fill its column renders the axis type
        # at headline size; the SVG therefore states its own pixel size and
        # the container scrolls.
        svg = charts.classification_chart(make_panel(BAR))

        assert re.search(r'<svg class="chart" width="\d+" height="\d+"', svg)

    def test_a_one_bar_facet_is_padded_to_the_same_width_as_a_full_one(self) -> None:
        narrow = charts.classification_chart(make_panel(BAR))
        wide = charts.classification_chart(make_panel(*([BAR] * 6)))

        assert _width(narrow) == _width(wide)

    def test_every_segment_carries_its_own_hover_title(self) -> None:
        svg = charts.classification_chart(make_panel(BAR))

        assert "<title>HIT: 2</title>" in svg
        assert "<title>MISS_SEVERE: 1</title>" in svg

    def test_the_stack_total_is_labelled_directly(self) -> None:
        svg = charts.classification_chart(make_panel(BAR))

        assert '<text class="chart-value"' in svg
        assert ">3</text>" in svg

    def test_each_classification_uses_its_shared_tone_token(self) -> None:
        svg = charts.classification_chart(make_panel(BAR))

        assert "var(--tone-good)" in svg
        assert "var(--tone-critical)" in svg

    @pytest.mark.parametrize(
        ("total", "expected_top"),
        [
            pytest.param(3, "5", id="small"),
            pytest.param(42, "50", id="medium"),
            pytest.param(250, "300", id="large"),
        ],
    )
    def test_the_axis_maximum_rounds_up_to_a_readable_step(
        self, total: int, expected_top: str
    ) -> None:
        bar = ClassificationBar(
            run_date=date(2027, 3, 1), counts=(("HIT", total),), total=total
        )

        svg = charts.classification_chart(make_panel(bar))

        assert f'text-anchor="end">{expected_top}</text>' in svg

    def test_an_empty_panel_still_renders_an_axis(self) -> None:
        svg = charts.classification_chart(make_panel())

        assert '<svg class="chart"' in svg
        assert "chart-grid" in svg


class TestRegimeChart:
    def test_plots_one_marker_per_reading_and_one_strip_cell_per_run(self) -> None:
        points = (
            RegimePoint(date(2027, 2, 26), 13.0, "NORMAL", "BULL"),
            RegimePoint(date(2027, 3, 1), 21.0, "SEVERE", "BEAR"),
        )

        svg = charts.regime_chart(points)

        assert svg.count("<circle") == 2
        assert "<polyline" in svg
        assert "var(--tone-quiet)" in svg
        assert "var(--tone-critical)" in svg

    def test_a_run_without_a_reading_keeps_its_strip_cell_but_no_marker(self) -> None:
        points = (
            RegimePoint(date(2027, 2, 26), None, "NORMAL", "BULL"),
            RegimePoint(date(2027, 3, 1), 15.0, "HIGH", "BEAR"),
        )

        svg = charts.regime_chart(points)

        assert svg.count("<circle") == 1
        assert svg.count("<rect") == 2

    def test_a_flat_series_does_not_divide_by_zero(self) -> None:
        points = (
            RegimePoint(date(2027, 2, 26), 15.0, "NORMAL", "BULL"),
            RegimePoint(date(2027, 3, 1), 15.0, "NORMAL", "BULL"),
        )

        svg = charts.regime_chart(points)

        assert svg.count("<circle") == 2

    def test_no_reading_at_all_renders_nothing(self) -> None:
        points = (RegimePoint(date(2027, 3, 1), None, None, None),)

        assert charts.regime_chart(points) == ""

    def test_an_unrecorded_level_says_so_in_the_tooltip(self) -> None:
        points = (RegimePoint(date(2027, 3, 1), 15.0, None, None),)

        svg = charts.regime_chart(points)

        assert "DD 未記録" in svg
        assert "gate 未記録" in svg


class TestEscaping:
    def test_markup_in_a_classification_name_cannot_break_out(self) -> None:
        bar = ClassificationBar(
            run_date=date(2027, 3, 1), counts=(("<script>", 1),), total=1
        )

        svg = charts.classification_chart(make_panel(bar))

        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg


@pytest.mark.parametrize(
    "svg",
    [
        pytest.param(charts.classification_chart(make_panel(BAR)), id="classification"),
        pytest.param(
            charts.regime_chart(
                (RegimePoint(date(2027, 3, 1), 15.0, "NORMAL", "BULL"),)
            ),
            id="regime",
        ),
    ],
)
def test_no_chart_references_an_external_resource(svg: str) -> None:
    assert "http" not in svg
    assert "<script" not in svg


def _width(svg: str) -> int:
    match = re.search(r'width="(\d+)"', svg)
    assert match is not None
    return int(match.group(1))
