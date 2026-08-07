"""Pure measurement of Distribution Day levels against the price action that followed.

`regime/distribution.py` classifies one `as_of` date; `regime/exposure.py` turns
that level into an Exposure Ceiling that can zero every candidate's share count.
Nothing in the repository measured whether the levels separate good forward
periods from bad ones -- `backtest/` never imports `regime.exposure` or
`regime.distribution`, so `settings.yaml`'s `dd_*` thresholds cannot move a
backtest number at all.

This module answers that question directly and read-only: replay every
observation date over a stored history, classify it with the same composite rule
`regime/gate.py::calculate_regime_snapshot` uses (the strictest of SPY's and
QQQ's own levels), and pair it with the return and drawdown that actually
followed.

The forward window is a deliberate, evaluation-only look-ahead. Each
observation's *classification* obeys the daily run's `date <= as_of` inclusive
boundary exactly; only the outcome attached to it reads later rows, and the whole
scan is still bounded by an outer `as_of` so the diagnostic cannot see past the
date it was asked about. No forward value is ever fed back into a level.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isnan
from statistics import fmean, median
from typing import TYPE_CHECKING

import pandas as pd

from swing_copilot.regime.distribution import (
    DistributionLevel,
    DistributionThresholds,
    calculate_distribution_days,
    distribution_level,
    distribution_severity,
)
from swing_copilot.regime.gate import (
    GateVerdict,
    RegimeThresholds,
    evaluate_market_gate,
)
from swing_copilot.screening.indicators import ema

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

#: Forward horizons in trading days. 25 is `backtest.max_hold_days`, so the
#: longest horizon matches how long a swing position is actually carried.
DEFAULT_HORIZONS = (5, 10, 25)

SPY_TARGET = "SPY"
QQQ_TARGET = "QQQ"
#: Equal-weight, daily-rebalanced basket of the stored non-index symbols. The
#: Exposure Ceiling gates single-stock entries, not index exposure, so this is
#: the closer proxy for what a `CASH_PRIORITY` day actually costs.
UNIVERSE_TARGET = "UNIVERSE_EW"
INDEX_TARGETS = (SPY_TARGET, QQQ_TARGET)

#: Rows of trailing history handed to `calculate_distribution_days` per
#: observation. The count is provably insensitive to anything older: a day is
#: dropped once `last_index - index >= window_days - 1`, and `count()` starts at
#: `len(visible) - days`, so only the final `window_days` comparison days can
#: contribute, and each needs one prior close. Replaying full slices instead
#: would be O(n^2) over ~1900 dates for an identical answer;
#: `tests/regime/test_dd_forward.py` pins the equivalence.
_TRAILING_ROWS = 80


@dataclass(frozen=True, slots=True)
class IndexCounts:
    """One index's Distribution Day counts at one observation date."""

    d25: float
    d15: float
    d5: float

    def level(self, thresholds: DistributionThresholds) -> DistributionLevel:
        """Classify these counts under `thresholds`."""
        return distribution_level(self.d25, self.d15, self.d5, thresholds=thresholds)


@dataclass(frozen=True, slots=True)
class ForwardOutcome:
    """Realised price action of one target over one horizon."""

    target: str
    horizon_days: int
    #: Close-to-close return from the observation date's close.
    total_return: float
    #: Worst close-to-close excursion inside the horizon, clamped at 0.0 so a
    #: window that never dips reports no drawdown. Close-based rather than
    #: intraday-low-based because the equal-weight basket has no meaningful
    #: intraday low, and the three targets must stay comparable.
    max_drawdown: float


@dataclass(frozen=True, slots=True)
class Observation:
    """One observation date's point-in-time state and what followed it."""

    as_of: date
    gate: GateVerdict
    spy: IndexCounts
    qqq: IndexCounts
    outcomes: tuple[ForwardOutcome, ...]

    def level(self, thresholds: DistributionThresholds) -> DistributionLevel:
        """Composite level: the strictest of the two indices' own levels.

        Mirrors `calculate_regime_snapshot`, which takes `max()` over the two
        `DistributionResult.level`s by `distribution_severity`. Classifying the
        stored counts here rather than re-running the counter is what makes a
        threshold sweep cheap: only these boundaries move, never the counts.
        """
        return max(
            (self.spy.level(thresholds), self.qqq.level(thresholds)),
            key=distribution_severity,
        )

    def outcome(self, target: str, horizon_days: int) -> ForwardOutcome | None:
        """Return the matching outcome, or `None` when the window ran off the end."""
        for outcome in self.outcomes:
            if outcome.target == target and outcome.horizon_days == horizon_days:
                return outcome
        return None


@dataclass(frozen=True, slots=True)
class ForwardScanRequest:
    """Everything one scan measures over, already read out of the store."""

    #: Tidy bars (`storage.market_store.BARS_COLUMNS`) for SPY, QQQ, ^VIX and
    #: any universe members, already trimmed to `date <= as_of` by the caller's
    #: repository read.
    bars: pd.DataFrame
    #: First observation date. Earlier rows are still read, as the history the
    #: counter and the EMA need.
    start: date
    #: Last visible date. Nothing after it is read, for any purpose.
    as_of: date
    thresholds: RegimeThresholds
    horizons: tuple[int, ...] = DEFAULT_HORIZONS


@dataclass(frozen=True, slots=True)
class ForwardScan:
    """The replayed observations plus what the scan could not measure."""

    start: date
    as_of: date
    horizons: tuple[int, ...]
    observations: tuple[Observation, ...]
    #: Trading dates in `[start, as_of]` skipped because the history before
    #: them was too short to count a full window or seed the gate EMA.
    warmup_skipped: int
    #: Symbols contributing to `UNIVERSE_TARGET`, 0 when only index bars were
    #: supplied (the basket target is then absent from every observation).
    universe_symbols: int

    @property
    def targets(self) -> tuple[str, ...]:
        """Targets actually measured, in report order."""
        return (
            (*INDEX_TARGETS, UNIVERSE_TARGET)
            if self.universe_symbols
            else INDEX_TARGETS
        )


@dataclass(frozen=True, slots=True)
class LevelStats:
    """Forward-action distribution of one level, for one target and horizon."""

    level: DistributionLevel
    target: str
    horizon_days: int
    #: Observation days. Overlapping windows make these strongly dependent;
    #: `episode_count` is the honest effective sample size.
    sample_size: int
    #: Contiguous runs at this level that contributed at least one observation.
    episode_count: int
    mean_return: float
    median_return: float
    positive_rate: float
    mean_drawdown: float
    median_drawdown: float
    worst_drawdown: float


def _closes_by_symbol(bars: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Pivot tidy bars into a date-indexed close matrix, bounded at `as_of`."""
    visible = bars.loc[bars["date"] <= as_of]
    return visible.pivot_table(
        index="date", columns="symbol", values="close"
    ).sort_index()


def _equal_weight_basket(closes: pd.DataFrame, symbols: Sequence[str]) -> pd.Series:
    """Chain an equal-weight, daily-rebalanced index level from member closes.

    Each day's return is the mean of the members that have both that close and
    the previous one, so a symbol whose history starts mid-scan joins without
    printing a spurious jump. The stored membership is today's, not each date's,
    so the basket carries survivorship bias; it is used only to contrast levels
    measured over the same members, where the bias is common to every level.
    """
    members = closes.reindex(columns=list(symbols))
    daily = members.pct_change(fill_method=None).mean(axis=1, skipna=True).fillna(0.0)
    return (1.0 + daily).cumprod()


def _forward_outcomes(
    series: pd.Series, index: int, target: str, horizons: tuple[int, ...]
) -> list[ForwardOutcome]:
    """Build every horizon's outcome from `series` position `index`.

    A gap in a target's history (a symbol with no bar on a date SPY traded)
    leaves `NaN` after the reindex onto the trading calendar. Such a horizon is
    omitted rather than emitted, because `NaN` propagates silently through
    `fmean` and would render a whole level's average unusable without saying so.
    """
    base = float(series.iloc[index])
    outcomes: list[ForwardOutcome] = []
    if not base > 0.0:
        return outcomes
    for horizon in horizons:
        if index + horizon >= len(series):
            continue
        end = float(series.iloc[index + horizon])
        window = series.iloc[index + 1 : index + horizon + 1]
        trough = float(window.min()) if window.notna().any() else end
        if isnan(end) or isnan(trough):
            continue
        outcomes.append(
            ForwardOutcome(
                target=target,
                horizon_days=horizon,
                total_return=end / base - 1.0,
                max_drawdown=min(0.0, trough / base - 1.0),
            )
        )
    return outcomes


def scan_forward(request: ForwardScanRequest) -> ForwardScan:
    """Replay every trading date in `[start, as_of]` and attach forward outcomes.

    SPY's stored dates are the trading calendar, matching
    `backtest/runner.py::_trading_days`.

    Args:
        request: Bars, window, thresholds, and horizons for one scan.

    Returns:
        Observations in date order, plus the warm-up and basket-coverage counts
        the caller must disclose.

    Raises:
        ValueError: `bars` holds no SPY or no QQQ rows at or before `as_of`.
    """
    closes = _closes_by_symbol(request.bars, request.as_of)
    missing = [symbol for symbol in INDEX_TARGETS if symbol not in closes.columns]
    if missing:
        msg = f"バーが見つかりません: {', '.join(missing)}（{request.as_of} 以前）"
        raise ValueError(msg)

    visible = request.bars.loc[request.bars["date"] <= request.as_of]
    spy_bars = visible.loc[visible["symbol"] == SPY_TARGET].sort_values("date")
    qqq_bars = visible.loc[visible["symbol"] == QQQ_TARGET].sort_values("date")
    spy_bars = spy_bars.reset_index(drop=True)
    qqq_bars = qqq_bars.reset_index(drop=True)

    calendar = list(spy_bars["date"])
    qqq_rows = {row_date: index for index, row_date in enumerate(qqq_bars["date"])}
    vix_closes = (
        dict(closes["^VIX"].dropna().items()) if "^VIX" in closes.columns else {}
    )
    universe = tuple(
        symbol
        for symbol in closes.columns
        if symbol not in INDEX_TARGETS and not symbol.startswith("^")
    )

    # Seeded from the first `ema_period` closes of whatever series it is given,
    # so a longer history than the daily run's 800-day window shifts the value
    # by the seed's decayed weight only -- ~1e-13 at period 50 after two years.
    # `tests/regime/test_dd_forward.py` pins that this never flips a verdict.
    spy_ema = ema(spy_bars["close"], request.thresholds.gate.ema_period)
    series_by_target = {
        SPY_TARGET: closes[SPY_TARGET].reindex(calendar),
        QQQ_TARGET: closes[QQQ_TARGET].reindex(calendar),
    }
    if universe:
        series_by_target[UNIVERSE_TARGET] = _equal_weight_basket(
            closes, universe
        ).reindex(calendar)

    observations: list[Observation] = []
    warmup_skipped = 0
    for index, as_of in enumerate(calendar):
        if as_of < request.start:
            continue
        qqq_index = qqq_rows.get(as_of)
        ema_value = (
            float(spy_ema.iloc[index]) if pd.notna(spy_ema.iloc[index]) else None
        )
        if qqq_index is None or ema_value is None:
            warmup_skipped += 1
            continue
        spy_counts = calculate_distribution_days(
            spy_bars.iloc[max(0, index - _TRAILING_ROWS) : index + 1],
            as_of,
            thresholds=request.thresholds.distribution,
        )
        qqq_counts = calculate_distribution_days(
            qqq_bars.iloc[max(0, qqq_index - _TRAILING_ROWS) : qqq_index + 1],
            as_of,
            thresholds=request.thresholds.distribution,
        )
        if DistributionLevel.UNKNOWN in (spy_counts.level, qqq_counts.level):
            warmup_skipped += 1
            continue
        gate = evaluate_market_gate(
            float(spy_bars.iloc[index]["close"]),
            ema_value,
            vix_closes.get(as_of),
            thresholds=request.thresholds.gate,
        )
        outcomes = [
            outcome
            for target, series in series_by_target.items()
            for outcome in _forward_outcomes(series, index, target, request.horizons)
        ]
        observations.append(
            Observation(
                as_of=as_of,
                gate=gate.verdict,
                spy=IndexCounts(spy_counts.d25, spy_counts.d15, spy_counts.d5),
                qqq=IndexCounts(qqq_counts.d25, qqq_counts.d15, qqq_counts.d5),
                outcomes=tuple(outcomes),
            )
        )
    return ForwardScan(
        start=request.start,
        as_of=request.as_of,
        horizons=request.horizons,
        observations=tuple(observations),
        warmup_skipped=warmup_skipped,
        universe_symbols=len(universe),
    )


def level_series(
    scan: ForwardScan, thresholds: DistributionThresholds
) -> tuple[DistributionLevel, ...]:
    """Classify every observation under one set of level boundaries."""
    return tuple(observation.level(thresholds) for observation in scan.observations)


def _episode_ids(levels: Sequence[DistributionLevel]) -> list[int]:
    """Label each observation with the index of its contiguous same-level run."""
    ids: list[int] = []
    current = -1
    previous: DistributionLevel | None = None
    for level in levels:
        if level != previous:
            current += 1
            previous = level
        ids.append(current)
    return ids


def summarise_level(
    scan: ForwardScan,
    levels: Sequence[DistributionLevel],
    level: DistributionLevel,
    key: tuple[str, int],
) -> LevelStats | None:
    """Aggregate one level's forward outcomes for one `(target, horizon)`.

    Args:
        scan: The replayed observations.
        levels: `level_series` output, aligned to `scan.observations`.
        level: The level to aggregate.
        key: `(target, horizon_days)` to read out of each observation.

    Returns:
        The aggregate, or `None` when no observation at `level` has a complete
        window for `key` -- an empty bucket is reported as absent, never as zero.
    """
    target, horizon = key
    episodes = _episode_ids(levels)
    returns: list[float] = []
    drawdowns: list[float] = []
    seen_episodes: set[int] = set()
    for observation, observed_level, episode in zip(
        scan.observations, levels, episodes, strict=True
    ):
        if observed_level is not level:
            continue
        outcome = observation.outcome(target, horizon)
        if outcome is None:
            continue
        returns.append(outcome.total_return)
        drawdowns.append(outcome.max_drawdown)
        seen_episodes.add(episode)
    if not returns:
        return None
    return LevelStats(
        level=level,
        target=target,
        horizon_days=horizon,
        sample_size=len(returns),
        episode_count=len(seen_episodes),
        mean_return=fmean(returns),
        median_return=median(returns),
        positive_rate=sum(value > 0.0 for value in returns) / len(returns),
        mean_drawdown=fmean(drawdowns),
        median_drawdown=median(drawdowns),
        worst_drawdown=min(drawdowns),
    )


def summarise_levels(
    scan: ForwardScan, thresholds: DistributionThresholds, key: tuple[str, int]
) -> tuple[LevelStats, ...]:
    """Aggregate every non-empty level for one `(target, horizon)`, strictest first."""
    levels = level_series(scan, thresholds)
    stats = (
        summarise_level(scan, levels, level, key)
        for level in sorted(set(levels), key=distribution_severity, reverse=True)
    )
    return tuple(entry for entry in stats if entry is not None)
