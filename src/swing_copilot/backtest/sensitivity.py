"""Parameter sensitivity grid and overfitting judgement (P2-10, roadmap §5 P2-10).

Pure grid-layout and judgement logic, kept separate from I/O: callers (the
`copilot-backtest grid` CLI subcommand) run the 25 backtests and hand the
resulting `GridCell` values here for the spike/plateau verdict.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.config import BacktestConfig

# Grid points as a percentage of the strategy's base (`settings.backtest`)
# value. Fixed by roadmap §5 P2-10 -- not a (要検証) threshold, so not config.
ATR_MULTIPLIER_PCT_GRID: tuple[int, ...] = (50, 75, 100, 125, 150)
MAX_HOLD_PCT_GRID: tuple[int, ...] = (80, 90, 100, 110, 120)
_GRID_COLS = len(MAX_HOLD_PCT_GRID)

SPIKE = "SPIKE"
PLATEAU = "PLATEAU"
NEITHER = "NEITHER"
INCONCLUSIVE = "INCONCLUSIVE"

_VERDICT_LABELS: dict[str, str] = {
    SPIKE: "スパイク（過学習疑い）",
    PLATEAU: "プラトー（頑健）",
    NEITHER: "判定なし",
    INCONCLUSIVE: "判定不能（データ不足）",
}


def grid_param_values(
    base_atr_multiplier: float, base_max_hold_days: int
) -> list[tuple[int, int, float, int]]:
    """The 25 (atr_pct, max_hold_pct, atr_multiplier, max_hold_days) cells.

    Row-major: ATR-multiplier percentage varies slower (outer loop),
    max-hold percentage faster (inner loop) -- matches `GridCell` ordering.

    Args:
        base_atr_multiplier: The strategy's un-gridded `exit_atr_multiple`.
        base_max_hold_days: The strategy's un-gridded `max_hold_days`.

    Returns:
        25 tuples, one per cell.
    """
    return [
        (
            atr_pct,
            max_hold_pct,
            base_atr_multiplier * atr_pct / 100,
            max(1, round(base_max_hold_days * max_hold_pct / 100)),
        )
        for atr_pct in ATR_MULTIPLIER_PCT_GRID
        for max_hold_pct in MAX_HOLD_PCT_GRID
    ]


@dataclass(frozen=True, slots=True)
class GridCell:
    """One sensitivity grid cell's result."""

    atr_multiplier_pct: int
    max_hold_pct: int
    expectancy_per_trade: float | None
    trade_count: int


@dataclass(frozen=True, slots=True)
class SensitivityGridResult:
    """The full grid plus its overfitting verdict."""

    cells: tuple[GridCell, ...]  # 25, row-major (see grid_param_values)
    verdict: str
    verdict_label: str


def is_gray_cell(cell: GridCell, gray_trade_count_threshold: int) -> bool:
    """Whether `cell` is excluded from spike/plateau conclusions (REQ-032)."""
    return (
        cell.trade_count < gray_trade_count_threshold
        or cell.expectancy_per_trade is None
    )


def _neighbor_indices(index: int) -> list[int]:
    row, col = divmod(index, _GRID_COLS)
    neighbors = []
    if row > 0:
        neighbors.append(index - _GRID_COLS)
    if row < len(ATR_MULTIPLIER_PCT_GRID) - 1:
        neighbors.append(index + _GRID_COLS)
    if col > 0:
        neighbors.append(index - 1)
    if col < _GRID_COLS - 1:
        neighbors.append(index + 1)
    return neighbors


def _expectancy(cell: GridCell) -> float:
    """The expectancy of a cell already known to be non-gray (non-None)."""
    value = cell.expectancy_per_trade
    assert value is not None  # noqa: S101 - callers only pass non-gray cells
    return value


def _is_spike(
    cells: Sequence[GridCell],
    best_index: int,
    best: GridCell,
    gray_trade_count_threshold: int,
    spike_multiplier: float,
) -> bool:
    neighbor_values = [
        _expectancy(cells[i])
        for i in _neighbor_indices(best_index)
        if not is_gray_cell(cells[i], gray_trade_count_threshold)
    ]
    if not neighbor_values:
        return False  # no comparable neighbor -- can't assess spikiness
    median = statistics.median(neighbor_values)
    if median <= 0:
        return False  # ratio is undefined/not meaningful against <=0 baseline
    return (_expectancy(best) / median) > spike_multiplier


def _is_plateau(
    non_gray: Sequence[GridCell], best: GridCell, plateau_tolerance_pct: float
) -> bool:
    best_value = _expectancy(best)
    tolerance = plateau_tolerance_pct * abs(best_value)
    return all(abs(_expectancy(cell) - best_value) <= tolerance for cell in non_gray)


def judge_grid(
    cells: Sequence[GridCell], thresholds: BacktestConfig
) -> SensitivityGridResult:
    """Classify a 25-cell sensitivity grid as spike/plateau/neither/inconclusive.

    Args:
        cells: Exactly 25 cells in `grid_param_values`'s row-major order.
        thresholds: Supplies `insufficient_trade_count_threshold` (gray-cell
            cutoff), `sensitivity_spike_multiplier`, and
            `sensitivity_plateau_tolerance_pct`.

    Returns:
        The cells (unchanged) plus a verdict and its Japanese label.
    """
    gray_threshold = thresholds.insufficient_trade_count_threshold
    non_gray_indexed = [
        (i, cell)
        for i, cell in enumerate(cells)
        if not is_gray_cell(cell, gray_threshold)
    ]
    if not non_gray_indexed:
        return SensitivityGridResult(
            cells=tuple(cells),
            verdict=INCONCLUSIVE,
            verdict_label=_VERDICT_LABELS[INCONCLUSIVE],
        )

    best_index, best = max(non_gray_indexed, key=lambda pair: _expectancy(pair[1]))
    non_gray = [cell for _, cell in non_gray_indexed]

    if _is_spike(
        cells,
        best_index,
        best,
        gray_threshold,
        thresholds.sensitivity_spike_multiplier,
    ):
        verdict = SPIKE
    elif _is_plateau(non_gray, best, thresholds.sensitivity_plateau_tolerance_pct):
        verdict = PLATEAU
    else:
        verdict = NEITHER

    return SensitivityGridResult(
        cells=tuple(cells), verdict=verdict, verdict_label=_VERDICT_LABELS[verdict]
    )
