"""Hand-calculated fixture tests for backtest/sensitivity.py (Issue #19, P2-10)."""

from __future__ import annotations

import dataclasses

import pytest

from swing_copilot.backtest.sensitivity import (
    ATR_MULTIPLIER_PCT_GRID,
    ENTRY_LIMIT_ATR_MULTIPLE_GRID,
    INCONCLUSIVE,
    MAX_HOLD_PCT_GRID,
    NEITHER,
    PLATEAU,
    SPIKE,
    GridCell,
    entry_limit_grid_values,
    grid_param_values,
    is_gray_cell,
    judge_grid,
)
from swing_copilot.config import BacktestConfig

_THRESHOLDS = BacktestConfig()  # insufficient=30, spike=1.5, plateau_tolerance=0.20
_BEST_INDEX = 12  # (atr_pct=100, max_hold_pct=100): row 2, col 2 of a 5x5 grid


def _uniform_grid(value: float, trade_count: int = 50) -> list[GridCell]:
    return [
        GridCell(
            atr_multiplier_pct=atr_pct,
            max_hold_pct=max_hold_pct,
            expectancy_per_trade=value,
            trade_count=trade_count,
        )
        for atr_pct in ATR_MULTIPLIER_PCT_GRID
        for max_hold_pct in MAX_HOLD_PCT_GRID
    ]


def _with_cell(
    cells: list[GridCell],
    index: int,
    *,
    expectancy_per_trade: float | None = None,
    trade_count: int | None = None,
) -> list[GridCell]:
    old = cells[index]
    updated = list(cells)
    updated[index] = dataclasses.replace(
        old,
        expectancy_per_trade=(
            old.expectancy_per_trade
            if expectancy_per_trade is None
            else expectancy_per_trade
        ),
        trade_count=old.trade_count if trade_count is None else trade_count,
    )
    return updated


class TestGridParamValues:
    def test_produces_25_cells_in_row_major_order(self):
        cells = grid_param_values(base_atr_multiplier=2.5, base_max_hold_days=60)

        assert len(cells) == 25
        assert cells[0] == (50, 40, 1.25, 24)
        assert cells[12] == (100, 100, 2.5, 60)
        assert cells[24] == (150, 200, 3.75, 120)

    def test_max_hold_days_rounds_and_floors_at_one(self):
        cells = grid_param_values(base_atr_multiplier=1.0, base_max_hold_days=1)

        # 40% of 1 day rounds to 0, floored to the minimum of 1 day.
        atr_pct, max_hold_pct, _atr_value, max_hold_days = cells[0]
        assert (atr_pct, max_hold_pct) == (50, 40)
        assert max_hold_days == 1

    def test_entry_limit_grid_is_absolute_and_includes_compatibility_arm(self):
        assert entry_limit_grid_values() == ENTRY_LIMIT_ATR_MULTIPLE_GRID
        assert entry_limit_grid_values() == (0.0, 0.5, 1.0, 1.5, 2.0)


class TestIsGrayCell:
    @pytest.mark.parametrize(
        ("trade_count", "expected_gray"), [(29, True), (30, False), (31, False)]
    )
    def test_trade_count_boundary(self, trade_count, expected_gray):
        cell = GridCell(100, 100, expectancy_per_trade=10.0, trade_count=trade_count)
        assert is_gray_cell(cell, gray_trade_count_threshold=30) is expected_gray

    def test_none_expectancy_is_gray_regardless_of_trade_count(self):
        cell = GridCell(100, 100, expectancy_per_trade=None, trade_count=100)
        assert is_gray_cell(cell, gray_trade_count_threshold=30) is True


class TestJudgeGrid:
    def test_spike_example_from_issue(self):
        # Best cell (index 12, atr=100%/max_hold=100%) expectancy=50; its 4
        # neighbors (indices 7, 17, 11, 13) are 25, 30, 35, 30 -> median 30.
        # 50 / 30 = 1.667 > 1.5 -> spike. Other cells set far from plateau
        # range so the spike verdict is unambiguous.
        cells = _uniform_grid(5.0)
        cells = _with_cell(cells, 12, expectancy_per_trade=50.0)
        cells = _with_cell(cells, 7, expectancy_per_trade=25.0)
        cells = _with_cell(cells, 17, expectancy_per_trade=30.0)
        cells = _with_cell(cells, 11, expectancy_per_trade=35.0)
        cells = _with_cell(cells, 13, expectancy_per_trade=30.0)

        result = judge_grid(cells, _THRESHOLDS)

        assert result.verdict == SPIKE
        assert "過学習疑い" in result.verdict_label

    def test_plateau_example_from_issue(self):
        # All 25 cells within 45..55 (best=55, tolerance=0.2*55=11, range
        # 44..66 covers all) -> plateau. Neighbor ratios stay well under 1.5.
        cells = [
            GridCell(atr, hold, expectancy_per_trade=50.0, trade_count=50)
            for atr in ATR_MULTIPLIER_PCT_GRID
            for hold in MAX_HOLD_PCT_GRID
        ]
        cells = _with_cell(cells, 0, expectancy_per_trade=45.0)
        cells = _with_cell(cells, 24, expectancy_per_trade=55.0)

        result = judge_grid(cells, _THRESHOLDS)

        assert result.verdict == PLATEAU
        assert "頑健" in result.verdict_label

    def test_gray_cell_excluded_from_conclusion(self):
        cells = _uniform_grid(50.0)
        # A single gray cell with an extreme value must not become "best" or
        # otherwise pollute the plateau/spike computation.
        cells = _with_cell(cells, 5, expectancy_per_trade=99999.0, trade_count=1)

        result = judge_grid(cells, _THRESHOLDS)

        assert result.verdict == PLATEAU

    def test_all_cells_gray_is_inconclusive(self):
        cells = _uniform_grid(50.0, trade_count=10)

        result = judge_grid(cells, _THRESHOLDS)

        assert result.verdict == INCONCLUSIVE
        assert "データ不足" in result.verdict_label

    @pytest.mark.parametrize(
        ("best_value", "expected_spike"),
        [(149.9, False), (150.0, False), (150.1, True)],
    )
    def test_spike_ratio_boundary_is_strictly_greater_than(
        self, best_value, expected_spike
    ):
        # Fill the grid with 1.0 everywhere (never plateau-eligible against a
        # ~150 best), then set the 4 neighbors of the best cell to exactly
        # 100.0 (median 100) and the best cell itself to the boundary value.
        cells = _uniform_grid(1.0)
        for neighbor_index in (7, 17, 11, 13):
            cells = _with_cell(cells, neighbor_index, expectancy_per_trade=100.0)
        cells = _with_cell(cells, _BEST_INDEX, expectancy_per_trade=best_value)

        result = judge_grid(cells, _THRESHOLDS)

        assert (result.verdict == SPIKE) is expected_spike
        if not expected_spike:
            assert result.verdict == NEITHER

    @pytest.mark.parametrize(
        ("outlier_value", "expected_plateau"), [(40.0, True), (39.0, False)]
    )
    def test_plateau_tolerance_boundary_is_inclusive(
        self, outlier_value, expected_plateau
    ):
        # 24 cells at 50.0 (best stays 50.0, tolerance = 0.2*50 = 10), one
        # low outlier at the boundary. The outlier is always lower than the
        # rest so it never becomes "best" and shifts the tolerance basis.
        cells = _uniform_grid(50.0)
        cells = _with_cell(cells, 5, expectancy_per_trade=outlier_value)

        result = judge_grid(cells, _THRESHOLDS)

        assert (result.verdict == PLATEAU) is expected_plateau
        if not expected_plateau:
            assert result.verdict == NEITHER

    def test_isolated_best_with_no_non_gray_neighbors_skips_spike_check(self):
        # The best cell's 4 neighbors are all gray -- spike can't be assessed,
        # so judge_grid falls through to the plateau check instead of raising.
        cells = _uniform_grid(50.0)
        cells = _with_cell(cells, _BEST_INDEX, expectancy_per_trade=1000.0)
        for neighbor_index in (7, 17, 11, 13):
            cells = _with_cell(cells, neighbor_index, trade_count=1)

        result = judge_grid(cells, _THRESHOLDS)

        assert result.verdict != SPIKE

    def test_non_positive_neighbor_median_skips_spike_check(self):
        # Neighbor median <= 0 makes the spike ratio undefined/meaningless,
        # so it must not raise (division by zero) or falsely report a spike.
        cells = _uniform_grid(1.0)
        for neighbor_index in (7, 17, 11, 13):
            cells = _with_cell(cells, neighbor_index, expectancy_per_trade=0.0)
        cells = _with_cell(cells, _BEST_INDEX, expectancy_per_trade=1000.0)

        result = judge_grid(cells, _THRESHOLDS)

        assert result.verdict != SPIKE
