"""Threshold sweeps over an already-replayed Distribution Day scan.

The level boundaries change only how counts are *classified*, never the counts:
`calculate_distribution_days` reads `window_days`/`dd_decline_pct`/
`stall_abs_change_pct`/`recovery_pct` when counting, and the boundaries only
inside `distribution_level`. One expensive replay (`dd_forward.scan_forward`)
therefore supports thousands of cheap re-classifications here.

Five boundaries are swept, not six. `regime/exposure.py::_base_exposure` maps
`SEVERE` to `CASH_PRIORITY` and `HIGH` to `REDUCE_ONLY`, and falls through for
everything else -- `CAUTION` and `NORMAL` reach the identical branch, and
`DistributionLevel.CAUTION` has no other consumer in the package. So
`dd_caution_d25` cannot move an exposure ceiling and is held at its configured
value; scoring it would invent a distinction the pipeline does not make.

Candidates are filtered through the same order constraints
`config.RegimeConfig._validate_dd_level_order` enforces, so a sweep can only
propose a `settings.yaml` that actually loads.

Scores are in-sample over one stored history. They rank candidates; they do not
validate them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from itertools import product
from typing import TYPE_CHECKING

import pandas as pd

from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    DistributionResult,
    DistributionThresholds,
)
from swing_copilot.regime.exposure import ExposureVerdict, determine_exposure
from swing_copilot.regime.gate import GateVerdict, MarketGate, RegimeSnapshot

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from swing_copilot.regime.dd_forward import ForwardScan

#: Smallest `dd_high_d25` that leaves room for a `dd_caution_d25` of at least 1,
#: which `RegimeConfig` requires (`Field(ge=1)` plus `high_d25 > caution_d25`).
_MIN_HIGH_D25 = 2

#: Candidate values per swept boundary. Wide enough to contain both the shipped
#: 7/6/5/3/2 and the far looser settings the measured count distribution
#: suggests, without exploding the constrained product.
GRID_RANGES: dict[str, tuple[int, ...]] = {
    "severe_d25": tuple(range(4, 14)),
    "severe_d15": tuple(range(2, 11)),
    "high_d25": tuple(range(2, 13)),
    "high_d15": tuple(range(1, 10)),
    "high_d5": tuple(range(1, 7)),
}
BOUNDARY_NAMES = tuple(GRID_RANGES)
#: `(column, boundary)` pairs feeding each level test, in the order
#: `distribution_level` applies them.
_SEVERE_TESTS = (("d25", "severe_d25"), ("d15", "severe_d15"))
_HIGH_TESTS = (("d25", "high_d25"), ("d15", "high_d15"), ("d5", "high_d5"))


@dataclass(frozen=True, slots=True)
class ExposureBoundaries:
    """The five level boundaries that can move an Exposure Ceiling."""

    severe_d25: int
    severe_d15: int
    high_d25: int
    high_d15: int
    high_d5: int

    @classmethod
    def from_thresholds(cls, thresholds: DistributionThresholds) -> ExposureBoundaries:
        """Read the boundaries currently carried by `thresholds`."""
        return cls(
            severe_d25=thresholds.severe_d25,
            severe_d15=thresholds.severe_d15,
            high_d25=thresholds.high_d25,
            high_d15=thresholds.high_d15,
            high_d5=thresholds.high_d5,
        )

    @property
    def is_loadable(self) -> bool:
        """Whether `settings.yaml` would accept these without a `ValueError`.

        Mirrors `config.RegimeConfig._validate_dd_level_order` for the swept
        boundaries. Its third clause, `high_d25 > caution_d25`, is always
        satisfiable while `high_d25 >= 2` because `dd_caution_d25` is free.
        """
        return (
            self.severe_d25 > self.high_d25 >= _MIN_HIGH_D25
            and self.severe_d15 > self.high_d15 >= 1
            and self.high_d5 >= 1
        )

    def applied_to(self, base: DistributionThresholds) -> DistributionThresholds:
        """Return `base` with these boundaries, leaving counting rules alone.

        `caution_d25` is clamped below `high_d25` when the candidate would
        otherwise make `base`'s configured value unloadable.
        """
        return replace(
            base,
            severe_d25=self.severe_d25,
            severe_d15=self.severe_d15,
            high_d25=self.high_d25,
            high_d15=self.high_d15,
            high_d5=self.high_d5,
            caution_d25=min(base.caution_d25, self.high_d25 - 1),
        )

    def label(self) -> str:
        """Compact rendering in `settings.yaml` reading order."""
        return (
            f"{self.severe_d25}/{self.severe_d15}/"
            f"{self.high_d25}/{self.high_d15}/{self.high_d5}"
        )


class GridAxis(StrEnum):
    """Which exposure decision a grid search is ranking candidates for.

    The two are genuinely independent: `severe_d25`/`severe_d15` alone decide
    `CASH_PRIORITY`, and the three `high_*` boundaries alone decide how the
    remaining days split between `REDUCE_ONLY` and `NEW_ENTRY_ALLOWED`. Ranking
    a five-boundary grid on one gap fills the result with variants of a single
    behaviour on the other.
    """

    CASH = "CASH"
    REDUCE = "REDUCE"


@dataclass(frozen=True, slots=True)
class ClassStats:
    """Forward action of the days one DD-driven exposure class covers."""

    verdict: ExposureVerdict
    #: Days classified here, and their share of the scan.
    days: int
    share: float
    #: Of those, the ones whose forward window fits inside the history. Only
    #: these carry a return, so they are what the aggregates average over.
    outcome_days: int
    #: Contiguous runs, the honest effective sample size: daily observations
    #: with overlapping forward windows are nowhere near independent.
    episodes: int
    #: `None` when no day in the class has a complete forward window.
    mean_return: float | None
    median_return: float | None
    positive_rate: float | None
    mean_drawdown: float | None
    worst_drawdown: float | None


@dataclass(frozen=True, slots=True)
class SweepPoint:
    """One candidate boundary set scored on one target and horizon."""

    boundaries: ExposureBoundaries
    classes: tuple[ClassStats, ...]

    def stats(self, verdict: ExposureVerdict) -> ClassStats | None:
        """Return one class's aggregate, or `None` when no day landed in it."""
        for entry in self.classes:
            if entry.verdict is verdict:
                return entry
        return None

    @property
    def cash_share(self) -> float:
        """Share of days on which DD alone forces `CASH_PRIORITY`."""
        stats = self.stats(ExposureVerdict.CASH_PRIORITY)
        return stats.share if stats else 0.0

    @property
    def return_gap(self) -> float | None:
        """Forward return avoided by sitting out `CASH_PRIORITY` days.

        Mean return of every other day minus the mean of the blocked ones.
        Positive means the block earns its keep; at or below zero means the
        blocked days were not the weak ones.
        """
        return self._gap(lambda stats: stats.mean_return)

    @property
    def drawdown_gap(self) -> float | None:
        """Drawdown avoided by sitting out `CASH_PRIORITY` days.

        Positive means the blocked days really did dip deeper.
        """
        return self._gap(lambda stats: stats.mean_drawdown)

    @property
    def reduce_gap(self) -> float | None:
        """Forward return given up by halving risk on `REDUCE_ONLY` days.

        `NEW_ENTRY_ALLOWED` mean minus `REDUCE_ONLY` mean. Separate from
        `return_gap` because the three `high_*` boundaries cannot move a single
        `CASH_PRIORITY` day -- this is the only axis on which they score.
        """
        allowed = self.stats(ExposureVerdict.NEW_ENTRY_ALLOWED)
        reduced = self.stats(ExposureVerdict.REDUCE_ONLY)
        if allowed is None or reduced is None:
            return None
        if allowed.mean_return is None or reduced.mean_return is None:
            return None
        return allowed.mean_return - reduced.mean_return

    def rank_key(self, axis: GridAxis) -> float:
        """The gap this axis is ranked by, with a missing gap sorting last."""
        gap = self.return_gap if axis is GridAxis.CASH else self.reduce_gap
        return float("-inf") if gap is None else gap

    def signature(self, axis: GridAxis) -> tuple[float, float]:
        """What this candidate does on one axis, ignoring which boundary did it.

        Many boundary sets classify the scan identically -- once a boundary sits
        past the highest count ever observed, raising it further changes
        nothing -- and the `high_*` boundaries never move a `CASH_PRIORITY` day
        at all. Collapsing on this keeps a ranked list from filling with
        variants of one behaviour.
        """
        reduced = self.stats(ExposureVerdict.REDUCE_ONLY)
        share = (
            self.cash_share
            if axis is GridAxis.CASH
            else (reduced.share if reduced else 0.0)
        )
        return (share, self.rank_key(axis))

    def _gap(self, read: Callable[[ClassStats], float | None]) -> float | None:
        """Contrast the blocked class against the day-weighted mean of the rest.

        Weighting is by `outcome_days`, not `days`: a class's mean is taken over
        the days that have a complete forward window, so the pooled mean must be
        weighted by the same population.
        """
        blocked = self.stats(ExposureVerdict.CASH_PRIORITY)
        blocked_value = read(blocked) if blocked is not None else None
        if blocked_value is None:
            return None
        weighted = [
            (entry.outcome_days, value)
            for entry in self.classes
            if entry.verdict is not ExposureVerdict.CASH_PRIORITY
            and (value := read(entry)) is not None
        ]
        total = sum(days for days, _ in weighted)
        if not total:
            return None
        return sum(days * value for days, value in weighted) / total - blocked_value


def dd_only_exposure(level: DistributionLevel) -> ExposureVerdict:
    """The ceiling this level alone imposes, holding the market gate at `BULL`.

    Calls the shipped `determine_exposure` instead of restating
    `_base_exposure`'s table, so this cannot drift from the daily run. A real
    gate can only make the result stricter, so this is the loosest ceiling a day
    at `level` could have had.
    """
    counts = DistributionResult(0.0, 0.0, 0.0, level, DataQuality.OK)
    snapshot = RegimeSnapshot(
        as_of=_UNUSED_DATE,
        gate=MarketGate(GateVerdict.BULL, 1.0, 1.0, 1.0),
        spy_distribution=counts,
        qqq_distribution=counts,
        dd_level=level,
        data_quality=DataQuality.OK,
    )
    return determine_exposure(snapshot).verdict


#: `determine_exposure` never reads `RegimeSnapshot.as_of`; a fixed placeholder
#: keeps `dd_only_exposure` free of a clock it has no business touching.
_UNUSED_DATE = date(1970, 1, 1)


@dataclass(frozen=True, slots=True)
class ScanFrame:
    """Column view of a scan, so a sweep re-classifies without a Python loop."""

    #: One row per observation, columns `{spy,qqq}_{d25,d15,d5}`. Comparing a
    #: whole column against a candidate boundary is what makes a grid of
    #: thousands of candidates affordable.
    counts: pd.DataFrame
    #: Forward return and drawdown for the scored target/horizon, `NaN` where
    #: the window ran off the end of the history.
    returns: pd.Series
    drawdowns: pd.Series
    target: str
    horizon_days: int

    @classmethod
    def build(cls, scan: ForwardScan, target: str, horizon_days: int) -> ScanFrame:
        """Flatten `scan` for one `(target, horizon)` pair.

        Raises:
            ValueError: The scan has no observations to classify.
        """
        if not scan.observations:
            msg = "観測日が0件のスキャンはスイープできません"
            raise ValueError(msg)
        rows = []
        returns: list[float] = []
        drawdowns: list[float] = []
        for observation in scan.observations:
            rows.append(
                {
                    "spy_d25": observation.spy.d25,
                    "spy_d15": observation.spy.d15,
                    "spy_d5": observation.spy.d5,
                    "qqq_d25": observation.qqq.d25,
                    "qqq_d15": observation.qqq.d15,
                    "qqq_d5": observation.qqq.d5,
                }
            )
            outcome = observation.outcome(target, horizon_days)
            returns.append(float("nan") if outcome is None else outcome.total_return)
            drawdowns.append(float("nan") if outcome is None else outcome.max_drawdown)
        return cls(
            counts=pd.DataFrame(rows),
            returns=pd.Series(returns, dtype=float),
            drawdowns=pd.Series(drawdowns, dtype=float),
            target=target,
            horizon_days=horizon_days,
        )

    def _any_index(self, tests: Sequence[tuple[str, int]]) -> pd.Series:
        """`True` where either index trips any `(metric, boundary)` test.

        The composite level is `max()` over the two indices' own levels, so a
        test firing on either index fires on the composite.
        """
        mask = pd.Series(False, index=self.counts.index)
        for metric, boundary in tests:
            for prefix in ("spy", "qqq"):
                mask |= self.counts[f"{prefix}_{metric}"] >= boundary
        return mask

    def classify(self, boundaries: ExposureBoundaries) -> pd.Series:
        """Label every observation with the ceiling DD alone imposes."""
        values = boundaries
        severe = self._any_index(
            [(metric, getattr(values, name)) for metric, name in _SEVERE_TESTS]
        )
        high = ~severe & self._any_index(
            [(metric, getattr(values, name)) for metric, name in _HIGH_TESTS]
        )
        labels = pd.Series(
            dd_only_exposure(DistributionLevel.NORMAL).value, index=self.counts.index
        )
        labels[high] = dd_only_exposure(DistributionLevel.HIGH).value
        labels[severe] = dd_only_exposure(DistributionLevel.SEVERE).value
        return labels


def _episodes(mask: pd.Series) -> int:
    """Count contiguous `True` runs."""
    return int((mask & ~mask.shift(1, fill_value=False)).sum())


def _class_stats(
    frame: ScanFrame, verdict: ExposureVerdict, mask: pd.Series
) -> ClassStats:
    """Aggregate one exposure class's days and their forward outcomes."""
    returns = frame.returns[mask].dropna()
    drawdowns = frame.drawdowns[mask].dropna()
    has_outcomes = not returns.empty
    return ClassStats(
        verdict=verdict,
        days=int(mask.sum()),
        share=float(mask.mean()),
        outcome_days=len(returns),
        episodes=_episodes(mask),
        mean_return=float(returns.mean()) if has_outcomes else None,
        median_return=float(returns.median()) if has_outcomes else None,
        positive_rate=float((returns > 0.0).mean()) if has_outcomes else None,
        mean_drawdown=float(drawdowns.mean()) if has_outcomes else None,
        worst_drawdown=float(drawdowns.min()) if has_outcomes else None,
    )


def score(frame: ScanFrame, boundaries: ExposureBoundaries) -> SweepPoint:
    """Classify the whole scan under `boundaries` and aggregate each class.

    Observations whose forward window ran off the end still count toward the
    day shares -- they are real classified days -- but contribute no return.
    """
    labels = frame.classify(boundaries)
    classes = [
        _class_stats(frame, verdict, labels == verdict.value)
        for verdict in (
            ExposureVerdict.CASH_PRIORITY,
            ExposureVerdict.REDUCE_ONLY,
            ExposureVerdict.NEW_ENTRY_ALLOWED,
        )
    ]
    return SweepPoint(
        boundaries=boundaries,
        classes=tuple(entry for entry in classes if entry.days),
    )


def sweep_boundary(
    frame: ScanFrame, base: ExposureBoundaries, name: str, values: Sequence[int]
) -> tuple[SweepPoint, ...]:
    """Move one boundary away from the configured set, holding the other four.

    Args:
        frame: The flattened scan to score against.
        base: The configured boundaries to vary from.
        name: A `BOUNDARY_NAMES` entry.
        values: Candidate values; ones the order constraints reject are skipped.

    Returns:
        One point per loadable value, in `values` order.

    Raises:
        KeyError: `name` is not a swept boundary.
    """
    if name not in GRID_RANGES:
        msg = f"未知の閾値名: {name}"
        raise KeyError(msg)
    candidates = (replace(base, **{name: value}) for value in values)
    return tuple(
        score(frame, candidate) for candidate in candidates if candidate.is_loadable
    )


def _loadable_candidates(
    ranges: dict[str, tuple[int, ...]],
) -> Iterator[ExposureBoundaries]:
    """Enumerate the order-constraint-satisfying corner of the boundary grid."""
    for values in product(*(ranges[name] for name in BOUNDARY_NAMES)):
        candidate = ExposureBoundaries(**dict(zip(BOUNDARY_NAMES, values, strict=True)))
        if candidate.is_loadable:
            yield candidate


@dataclass(frozen=True, slots=True)
class GridFilters:
    """Floors a grid candidate must clear before it is ranked at all."""

    #: Minimum distinct `CASH_PRIORITY` runs. Overlapping daily windows make raw
    #: day counts look far more independent than they are, so a candidate that
    #: fires in a handful of episodes is dropped however good its gap looks.
    min_episodes: int = 10
    #: Ceiling on the share of days sent to `CASH_PRIORITY`. A block that fires
    #: most of the time is a default stance, not a warning, whatever it scores.
    max_cash_share: float = 0.35


@dataclass(frozen=True, slots=True)
class GridResult:
    """A scored grid, with what it dropped disclosed rather than implied."""

    #: Distinct behaviours, best `(return_gap, reduce_gap)` first. Each is the
    #: lowest-valued boundary set producing that behaviour.
    points: tuple[SweepPoint, ...]
    #: Loadable candidates enumerated, before filters and collapsing.
    evaluated: int
    #: Candidates dropped by `GridFilters`.
    filtered_out: int
    #: Candidates that survived the filters but duplicated a kept behaviour.
    collapsed: int


def sweep_grid(
    frame: ScanFrame,
    axis: GridAxis,
    *,
    filters: GridFilters | None = None,
    ranges: dict[str, tuple[int, ...]] | None = None,
) -> GridResult:
    """Score the constrained boundary grid on one axis, best gap first.

    Args:
        frame: The flattened scan to score against.
        axis: Which exposure decision to rank and collapse on.
        filters: Episode and share floors; defaults to `GridFilters()`.
        ranges: Candidate values per boundary; defaults to `GRID_RANGES`.

    Returns:
        The distinct surviving behaviours and the counts behind them.
    """
    limits = filters or GridFilters()
    kept = []
    evaluated = 0
    filtered_out = 0
    for candidate in _loadable_candidates(ranges or GRID_RANGES):
        evaluated += 1
        point = score(frame, candidate)
        blocked = point.stats(ExposureVerdict.CASH_PRIORITY)
        if (
            point.rank_key(axis) == float("-inf")
            or blocked is None
            or blocked.episodes < limits.min_episodes
            or point.cash_share > limits.max_cash_share
        ):
            filtered_out += 1
            continue
        kept.append(point)
    kept.sort(key=lambda point: point.rank_key(axis), reverse=True)
    seen: set[tuple[float, float]] = set()
    distinct = []
    for point in kept:
        signature = point.signature(axis)
        if signature in seen:
            continue
        seen.add(signature)
        distinct.append(point)
    return GridResult(
        points=tuple(distinct),
        evaluated=evaluated,
        filtered_out=filtered_out,
        collapsed=len(kept) - len(distinct),
    )
