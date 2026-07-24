"""Pure market-gate evaluation from point-in-time index values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    DistributionResult,
    DistributionThresholds,
    calculate_distribution_days,
)
from swing_copilot.screening.indicators import ema

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd


class GateVerdict(StrEnum):
    """Code-owned market direction verdict."""

    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class MarketGate:
    """Result of comparing SPY trend and VIX risk appetite."""

    verdict: GateVerdict
    spy_close: float | None
    spy_ema: float | None
    vix_close: float | None


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    """Complete deterministic market state for one run's ``as_of`` date."""

    as_of: date
    gate: MarketGate
    spy_distribution: DistributionResult
    qqq_distribution: DistributionResult
    dd_level: DistributionLevel
    data_quality: DataQuality


@dataclass(frozen=True, slots=True)
class GateThresholds:
    """Configurable, unvalidated market-gate thresholds."""

    ema_period: int = 50
    bull_vix_max: float = 20.0
    bear_spy_ema_ratio: float = 0.97
    bear_vix_min: float = 30.0


@dataclass(frozen=True, slots=True)
class RegimeThresholds:
    """All code-owned gate and Distribution Day configuration."""

    gate: GateThresholds
    distribution: DistributionThresholds


DEFAULT_GATE_THRESHOLDS = GateThresholds()
DEFAULT_REGIME_THRESHOLDS = RegimeThresholds(
    gate=DEFAULT_GATE_THRESHOLDS,
    distribution=DistributionThresholds(),
)


def evaluate_market_gate(
    spy_close: float | None,
    spy_ema: float | None,
    vix_close: float | None,
    *,
    thresholds: GateThresholds = DEFAULT_GATE_THRESHOLDS,
) -> MarketGate:
    """Classify the market without I/O or wall-clock access.

    The strict comparisons deliberately keep threshold-equal observations
    neutral, as required by Issue #22.
    """
    if spy_close is None or spy_ema is None or vix_close is None:
        return MarketGate(GateVerdict.UNKNOWN, spy_close, spy_ema, vix_close)
    if (
        spy_close < spy_ema * thresholds.bear_spy_ema_ratio
        or vix_close > thresholds.bear_vix_min
    ):
        verdict = GateVerdict.BEAR
    elif spy_close > spy_ema and vix_close < thresholds.bull_vix_max:
        verdict = GateVerdict.BULL
    else:
        verdict = GateVerdict.NEUTRAL
    return MarketGate(verdict, spy_close, spy_ema, vix_close)


def calculate_regime_snapshot(
    spy_bars: pd.DataFrame,
    qqq_bars: pd.DataFrame,
    vix_bars: pd.DataFrame,
    as_of: date,
    thresholds: RegimeThresholds = DEFAULT_REGIME_THRESHOLDS,
) -> RegimeSnapshot:
    """Build one snapshot from already-fetched OHLCV frames.

    Every input is explicitly trimmed at the functional-core boundary so a
    caller cannot accidentally introduce future market data.
    """
    spy = spy_bars.loc[spy_bars["date"] <= as_of].sort_values("date")
    qqq = qqq_bars.loc[qqq_bars["date"] <= as_of].sort_values("date")
    vix = vix_bars.loc[vix_bars["date"] <= as_of].sort_values("date")
    spy_close = float(spy.iloc[-1]["close"]) if not spy.empty else None
    spy_ema_series = (
        ema(spy["close"], thresholds.gate.ema_period) if not spy.empty else None
    )
    spy_ema = (
        float(spy_ema_series.iloc[-1])
        if spy_ema_series is not None
        and not spy_ema_series.empty
        and spy_ema_series.notna().iloc[-1]
        else None
    )
    vix_close = float(vix.iloc[-1]["close"]) if not vix.empty else None
    gate = evaluate_market_gate(
        spy_close,
        spy_ema,
        vix_close,
        thresholds=thresholds.gate,
    )
    spy_distribution = calculate_distribution_days(
        spy, as_of, thresholds=thresholds.distribution
    )
    qqq_distribution = calculate_distribution_days(
        qqq, as_of, thresholds=thresholds.distribution
    )
    if (
        spy_distribution.level is DistributionLevel.UNKNOWN
        or qqq_distribution.level is DistributionLevel.UNKNOWN
    ):
        dd_level = DistributionLevel.UNKNOWN
    else:
        levels = (spy_distribution.level, qqq_distribution.level)
        dd_level = max(levels, key=_distribution_severity)
    is_insufficient = (
        gate.verdict is GateVerdict.UNKNOWN
        or spy_distribution.data_quality is DataQuality.INSUFFICIENT
        or qqq_distribution.data_quality is DataQuality.INSUFFICIENT
    )
    return RegimeSnapshot(
        as_of,
        gate,
        spy_distribution,
        qqq_distribution,
        dd_level,
        DataQuality.INSUFFICIENT if is_insufficient else DataQuality.OK,
    )


def _distribution_severity(level: DistributionLevel) -> int:
    return {
        DistributionLevel.NORMAL: 0,
        DistributionLevel.CAUTION: 1,
        DistributionLevel.HIGH: 2,
        DistributionLevel.SEVERE: 3,
        DistributionLevel.UNKNOWN: 4,
    }[level]
