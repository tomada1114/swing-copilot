"""Pure IBD-style Distribution Day counting."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd


class DataQuality(StrEnum):
    """Whether an index history supports the requested calculation."""

    OK = "OK"
    INSUFFICIENT = "INSUFFICIENT"


class DistributionLevel(StrEnum):
    """Risk level from Distribution Day counts."""

    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    HIGH = "HIGH"
    SEVERE = "SEVERE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class DistributionResult:
    """Counts and severity for one index, evaluated at one ``as_of`` date."""

    d25: float
    d15: float
    d5: float
    level: DistributionLevel
    data_quality: DataQuality


@dataclass(frozen=True, slots=True)
class DistributionThresholds:
    """Configurable, unvalidated Distribution Day thresholds."""

    window_days: int = 25
    dd_decline_pct: float = -0.002
    stall_abs_change_pct: float = 0.001
    recovery_pct: float = 0.05


DEFAULT_DISTRIBUTION_THRESHOLDS = DistributionThresholds()


_SEVERE_D25 = 6
_SEVERE_D15 = 4
_HIGH_D25 = 5
_HIGH_D15 = 3
_HIGH_D5 = 2
_CAUTION_D25 = 3


def calculate_distribution_days(
    bars: pd.DataFrame,
    as_of: date,
    *,
    thresholds: DistributionThresholds = DEFAULT_DISTRIBUTION_THRESHOLDS,
) -> DistributionResult:
    """Count valid distribution/stall days using only rows through ``as_of``.

    A full 25 comparison-day window needs 26 prices: the first price is the
    prior close for the earliest eligible day. A day expires on the 25th
    trading day after its observation, therefore it remains live for 24
    subsequent observations.
    """
    visible = bars.loc[bars["date"] <= as_of].sort_values("date").reset_index(drop=True)
    if len(visible) < thresholds.window_days + 1:
        return DistributionResult(
            0.0,
            0.0,
            0.0,
            DistributionLevel.UNKNOWN,
            DataQuality.INSUFFICIENT,
        )

    valid_weights: list[tuple[int, float]] = []
    last_index = len(visible) - 1
    for index in range(1, len(visible)):
        previous = visible.iloc[index - 1]
        current = visible.iloc[index]
        previous_close = float(previous["close"])
        if previous_close <= 0 or float(current["volume"]) <= float(previous["volume"]):
            continue
        change = float(current["close"]) / previous_close - 1.0
        weight = 1.0 if change <= thresholds.dd_decline_pct else 0.0
        if weight == 0.0 and abs(change) < thresholds.stall_abs_change_pct:
            weight = 0.5
        if weight == 0.0 or last_index - index >= thresholds.window_days - 1:
            continue
        later_closes = visible.iloc[index + 1 :]["close"]
        if (
            later_closes >= float(current["close"]) * (1.0 + thresholds.recovery_pct)
        ).any():
            continue
        valid_weights.append((index, weight))

    def count(days: int) -> float:
        start = max(1, len(visible) - days)
        return sum(weight for index, weight in valid_weights if index >= start)

    d25, d15, d5 = count(25), count(15), count(5)
    return DistributionResult(
        d25,
        d15,
        d5,
        distribution_level(d25, d15, d5),
        DataQuality.OK,
    )


def distribution_level(d25: float, d15: float, d5: float) -> DistributionLevel:
    """Return the strictest configured Distribution Day severity."""
    if d25 >= _SEVERE_D25 or d15 >= _SEVERE_D15:
        return DistributionLevel.SEVERE
    if d25 >= _HIGH_D25 or d15 >= _HIGH_D15 or d5 >= _HIGH_D5:
        return DistributionLevel.HIGH
    if d25 >= _CAUTION_D25:
        return DistributionLevel.CAUTION
    return DistributionLevel.NORMAL
