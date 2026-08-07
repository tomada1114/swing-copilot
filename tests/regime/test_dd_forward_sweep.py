"""Contract tests for `regime/dd_forward_sweep.py`.

Two invariants carry the module: a proposed boundary set must be one
`settings.yaml` would actually load, and the exposure class a candidate is
scored under must be the one `regime/exposure.py` would really assign.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from swing_copilot.config import RegimeConfig
from swing_copilot.regime.dd_forward import ForwardScanRequest, scan_forward
from swing_copilot.regime.dd_forward_sweep import (
    BOUNDARY_NAMES,
    GRID_RANGES,
    ClassStats,
    ExposureBoundaries,
    GridAxis,
    GridFilters,
    ScanFrame,
    SweepPoint,
    dd_only_exposure,
    score,
    sweep_boundary,
    sweep_grid,
)
from swing_copilot.regime.distribution import DistributionLevel, DistributionThresholds
from swing_copilot.regime.exposure import ExposureVerdict
from swing_copilot.regime.gate import DEFAULT_REGIME_THRESHOLDS
from tests.regime.conftest import market_bars

_TARGET = "SPY"
_HORIZON = 5
#: A few values per boundary. The grid tests assert ordering, filtering, and
#: collapsing -- properties that hold at any size -- so scoring the full
#: `GRID_RANGES` product would only make the suite slow.
_SMALL_RANGES = {name: GRID_RANGES[name][:3] for name in BOUNDARY_NAMES}


@pytest.fixture
def frame() -> ScanFrame:
    bars = market_bars(160)
    scan = scan_forward(
        ForwardScanRequest(
            bars=bars,
            start=date.min,
            as_of=max(bars["date"]),
            thresholds=DEFAULT_REGIME_THRESHOLDS,
            horizons=(_HORIZON,),
        )
    )
    return ScanFrame.build(scan, _TARGET, _HORIZON)


def _current() -> ExposureBoundaries:
    return ExposureBoundaries.from_thresholds(DEFAULT_REGIME_THRESHOLDS.distribution)


@pytest.mark.parametrize("axis", list(GridAxis))
def test_shipped_defaults_round_trip(axis: GridAxis) -> None:
    """`from_thresholds` and `applied_to` are inverses for a loadable set."""
    base = DEFAULT_REGIME_THRESHOLDS.distribution
    restored = ExposureBoundaries.from_thresholds(base).applied_to(base)
    assert restored == base
    assert axis in GridAxis


def test_is_loadable_agrees_with_the_settings_validator() -> None:
    """Every grid candidate can be written into `settings.yaml` and loaded back.

    `RegimeConfig` is the authority; the sweep only mirrors it, so the mirror is
    checked against the real validator over the whole candidate space rather
    than restated in a comment.
    """
    checked = 0
    for severe_d25 in GRID_RANGES["severe_d25"]:
        for high_d25 in GRID_RANGES["high_d25"]:
            for severe_d15 in GRID_RANGES["severe_d15"]:
                for high_d15 in GRID_RANGES["high_d15"]:
                    candidate = ExposureBoundaries(
                        severe_d25=severe_d25,
                        severe_d15=severe_d15,
                        high_d25=high_d25,
                        high_d15=high_d15,
                        high_d5=2,
                    )
                    applied = candidate.applied_to(
                        DEFAULT_REGIME_THRESHOLDS.distribution
                    )
                    loads = _loads(applied)
                    assert candidate.is_loadable == loads, candidate
                    checked += 1
    assert checked > 100


def _loads(thresholds: DistributionThresholds) -> bool:
    try:
        RegimeConfig(
            dd_severe_d25=thresholds.severe_d25,
            dd_severe_d15=thresholds.severe_d15,
            dd_high_d25=thresholds.high_d25,
            dd_high_d15=thresholds.high_d15,
            dd_high_d5=thresholds.high_d5,
            dd_caution_d25=thresholds.caution_d25,
        )
    except ValueError:
        return False
    return True


def test_applied_to_clamps_caution_below_high() -> None:
    """A candidate that undercuts the configured `caution_d25` stays loadable."""
    base = DistributionThresholds(caution_d25=3)
    tight = ExposureBoundaries(
        severe_d25=4, severe_d15=3, high_d25=2, high_d15=2, high_d5=1
    )
    assert tight.applied_to(base).caution_d25 == 1
    loose = ExposureBoundaries(
        severe_d25=12, severe_d15=8, high_d25=9, high_d15=6, high_d5=3
    )
    assert loose.applied_to(base).caution_d25 == 3


def test_dd_only_exposure_matches_the_shipped_mapping() -> None:
    """The three DD-driven ceilings are exactly `_base_exposure`'s, gate held BULL."""
    assert dd_only_exposure(DistributionLevel.SEVERE) is ExposureVerdict.CASH_PRIORITY
    assert dd_only_exposure(DistributionLevel.HIGH) is ExposureVerdict.REDUCE_ONLY
    assert (
        dd_only_exposure(DistributionLevel.CAUTION) is ExposureVerdict.NEW_ENTRY_ALLOWED
    )
    assert (
        dd_only_exposure(DistributionLevel.NORMAL) is ExposureVerdict.NEW_ENTRY_ALLOWED
    )


def test_caution_and_normal_are_indistinguishable_to_exposure() -> None:
    """The reason `dd_caution_d25` is not swept, asserted rather than asserted in prose."""
    assert dd_only_exposure(DistributionLevel.CAUTION) is dd_only_exposure(
        DistributionLevel.NORMAL
    )


def test_classify_partitions_every_day_exactly_once(frame: ScanFrame) -> None:
    """The three classes are disjoint and cover the scan."""
    point = score(frame, _current())
    assert sum(entry.days for entry in point.classes) == len(frame.counts)
    assert sum(entry.share for entry in point.classes) == pytest.approx(1.0)


def test_high_boundaries_cannot_move_a_cash_day(frame: ScanFrame) -> None:
    """`high_*` only splits the non-SEVERE days; `severe_*` alone drives CASH."""
    base = _current()
    for name in ("high_d25", "high_d15", "high_d5"):
        for point in sweep_boundary(frame, base, name, GRID_RANGES[name]):
            assert point.cash_share == score(frame, base).cash_share


def test_severe_boundaries_do_move_cash_days(frame: ScanFrame) -> None:
    """Raising `severe_d25` never increases the blocked share."""
    base = _current()
    points = sweep_boundary(frame, base, "severe_d25", (4, 6, 12))
    shares = [point.cash_share for point in points]
    assert shares == sorted(shares, reverse=True)


def test_sweep_boundary_skips_unloadable_values(frame: ScanFrame) -> None:
    """Values violating the order constraints are dropped, not scored."""
    base = _current()
    points = sweep_boundary(frame, base, "severe_d25", (1, 2, 3, 7))
    assert [point.boundaries.severe_d25 for point in points] == [7]


def test_sweep_boundary_rejects_an_unknown_name(frame: ScanFrame) -> None:
    assert set(BOUNDARY_NAMES) == set(GRID_RANGES)
    with pytest.raises(KeyError, match="dd_caution_d25"):
        sweep_boundary(frame, _current(), "dd_caution_d25", (1, 2))


def test_grid_discloses_what_it_dropped(frame: ScanFrame) -> None:
    """Filtered and collapsed candidates are counted, never silently truncated."""
    result = sweep_grid(
        frame,
        GridAxis.CASH,
        filters=GridFilters(min_episodes=1, max_cash_share=1.0),
        ranges=_SMALL_RANGES,
    )
    assert result.evaluated > 0
    assert len(result.points) + result.collapsed + result.filtered_out == (
        result.evaluated
    )


def test_grid_filters_are_enforced(frame: ScanFrame) -> None:
    """No survivor exceeds the cash-share ceiling or misses the episode floor."""
    filters = GridFilters(min_episodes=2, max_cash_share=0.5)
    result = sweep_grid(frame, GridAxis.CASH, filters=filters, ranges=_SMALL_RANGES)
    for point in result.points:
        blocked = point.stats(ExposureVerdict.CASH_PRIORITY)
        assert blocked is not None
        assert blocked.episodes >= filters.min_episodes
        assert point.cash_share <= filters.max_cash_share


def test_grid_is_ranked_by_the_requested_axis(frame: ScanFrame) -> None:
    """Each axis sorts by its own gap, descending."""
    filters = GridFilters(min_episodes=1, max_cash_share=1.0)
    for axis in GridAxis:
        result = sweep_grid(frame, axis, filters=filters, ranges=_SMALL_RANGES)
        keys = [point.rank_key(axis) for point in result.points]
        assert keys == sorted(keys, reverse=True)


def test_grid_collapses_duplicate_behaviour_per_axis(frame: ScanFrame) -> None:
    """Two candidates that classify identically on an axis yield one row."""
    filters = GridFilters(min_episodes=1, max_cash_share=1.0)
    result = sweep_grid(frame, GridAxis.CASH, filters=filters, ranges=_SMALL_RANGES)
    signatures = [point.signature(GridAxis.CASH) for point in result.points]
    assert len(signatures) == len(set(signatures))


def test_scan_frame_rejects_an_empty_scan() -> None:
    """A scan with nothing to classify is an error, not an empty sweep."""
    bars = market_bars(120)
    scan = scan_forward(
        ForwardScanRequest(
            bars=bars,
            start=date(2099, 1, 1),
            as_of=max(bars["date"]),
            thresholds=DEFAULT_REGIME_THRESHOLDS,
            horizons=(_HORIZON,),
        )
    )
    with pytest.raises(ValueError, match="観測日"):
        ScanFrame.build(scan, _TARGET, _HORIZON)


def test_days_without_a_forward_window_still_count_as_classified(
    frame: ScanFrame,
) -> None:
    """Tail days appear in the shares but not in the averages."""
    point = score(frame, _current())
    total_days = sum(entry.days for entry in point.classes)
    total_outcomes = sum(entry.outcome_days for entry in point.classes)
    assert total_days - total_outcomes == _HORIZON


def _class_stats(
    verdict: ExposureVerdict, *, share: float = 0.4, mean_return: float | None = 0.01
) -> ClassStats:
    """A `ClassStats` with only what each test varies set.

    The rest are placeholders no assertion below reads.
    """
    return ClassStats(
        verdict=verdict,
        days=10,
        share=share,
        outcome_days=10 if mean_return is not None else 0,
        episodes=2,
        mean_return=mean_return,
        median_return=mean_return,
        positive_rate=None if mean_return is None else 1.0,
        mean_drawdown=mean_return,
        worst_drawdown=mean_return,
    )


def test_reduce_gap_is_the_allowed_minus_reduced_mean() -> None:
    """A real gap is `NEW_ENTRY_ALLOWED`'s mean minus `REDUCE_ONLY`'s, nothing pooled."""
    allowed = _class_stats(ExposureVerdict.NEW_ENTRY_ALLOWED, mean_return=0.05)
    reduced = _class_stats(ExposureVerdict.REDUCE_ONLY, mean_return=0.01)
    point = SweepPoint(boundaries=_current(), classes=(allowed, reduced))
    assert point.reduce_gap == pytest.approx(0.04)


def test_reduce_gap_is_none_when_a_class_has_no_outcome_days() -> None:
    """Both classes existing is not enough; each also needs a real mean return."""
    allowed = _class_stats(ExposureVerdict.NEW_ENTRY_ALLOWED, mean_return=0.05)
    reduced_without_outcomes = _class_stats(
        ExposureVerdict.REDUCE_ONLY, mean_return=None
    )
    point = SweepPoint(
        boundaries=_current(), classes=(allowed, reduced_without_outcomes)
    )
    assert point.reduce_gap is None


def test_return_gap_is_none_without_a_cash_priority_class() -> None:
    """No blocked class at all is an unscoreable gap, not a crash."""
    allowed = _class_stats(ExposureVerdict.NEW_ENTRY_ALLOWED, mean_return=0.05)
    point = SweepPoint(boundaries=_current(), classes=(allowed,))
    assert point.return_gap is None


def test_signature_on_the_reduce_axis_uses_the_reduce_only_share() -> None:
    """The REDUCE axis signature keys on `REDUCE_ONLY`'s share, not `CASH_PRIORITY`'s."""
    cash = _class_stats(ExposureVerdict.CASH_PRIORITY, share=0.2, mean_return=-0.02)
    reduced = _class_stats(ExposureVerdict.REDUCE_ONLY, share=0.3, mean_return=0.01)
    point = SweepPoint(boundaries=_current(), classes=(cash, reduced))
    assert point.signature(GridAxis.REDUCE) == (0.3, point.rank_key(GridAxis.REDUCE))


def test_signature_on_the_reduce_axis_without_a_reduce_class_is_zero_share() -> None:
    """No `REDUCE_ONLY` day at all signs as a zero share, not a missing signature."""
    cash = _class_stats(ExposureVerdict.CASH_PRIORITY, share=0.2, mean_return=-0.02)
    point = SweepPoint(boundaries=_current(), classes=(cash,))
    assert point.signature(GridAxis.REDUCE) == (0.0, point.rank_key(GridAxis.REDUCE))


def test_grid_collapses_a_boundary_that_stops_changing_the_classification() -> None:
    """Two threshold values that classify the scan identically collapse to one row.

    A single day's `spy_d25=6` is SEVERE at `severe_d25` 5 or 6 alike (nothing in
    the data separates them), so both survive the filters and produce the same
    `(share, rank_key)` signature; `severe_d25=8` never reaches SEVERE at all and
    is filtered out before it can be collapsed.
    """
    rows = [
        {
            "spy_d25": 6,
            "spy_d15": 0,
            "spy_d5": 0,
            "qqq_d25": 0,
            "qqq_d15": 0,
            "qqq_d5": 0,
        }
    ]
    rows += [
        {
            "spy_d25": 0,
            "spy_d15": 0,
            "spy_d5": 0,
            "qqq_d25": 0,
            "qqq_d15": 0,
            "qqq_d5": 0,
        }
        for _ in range(19)
    ]
    frame = ScanFrame(
        counts=pd.DataFrame(rows),
        returns=pd.Series([-0.05, *([0.01] * 19)], dtype=float),
        drawdowns=pd.Series([-0.05, *([0.0] * 19)], dtype=float),
        target="SPY",
        horizon_days=5,
    )
    ranges = {
        "severe_d25": (5, 6, 8),
        "severe_d15": (4,),
        "high_d25": (2,),
        "high_d15": (1,),
        "high_d5": (1,),
    }
    result = sweep_grid(
        frame,
        GridAxis.CASH,
        filters=GridFilters(min_episodes=1, max_cash_share=1.0),
        ranges=ranges,
    )
    assert result.evaluated == 3
    assert result.filtered_out == 1
    assert result.collapsed == 1
    assert [point.boundaries.severe_d25 for point in result.points] == [5]
