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
    distribution_severity,
)
from swing_copilot.regime.ftd import (
    DEFAULT_FTD_THRESHOLDS,
    FtdSnapshot,
    FtdThresholds,
    calculate_ftd_snapshot,
)
from swing_copilot.screening.indicators import sma

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
    """Result of comparing SPY's long trend with the panic VIX threshold."""

    verdict: GateVerdict
    spy_close: float | None
    spy_sma200: float | None
    vix_close: float | None
    is_panic: bool = False


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    """Complete deterministic market state for one run's ``as_of`` date."""

    as_of: date
    gate: MarketGate
    spy_distribution: DistributionResult
    qqq_distribution: DistributionResult
    dd_level: DistributionLevel
    data_quality: DataQuality
    ftd: FtdSnapshot | None = None


@dataclass(frozen=True, slots=True)
class GateThresholds:
    """Configurable, unvalidated market-gate thresholds."""

    sma_period: int = 200
    bear_spy_sma_ratio: float = 0.97
    bear_vix_min: float = 30.0


@dataclass(frozen=True, slots=True)
class RegimeThresholds:
    """All code-owned gate and Distribution Day configuration."""

    gate: GateThresholds
    distribution: DistributionThresholds
    ftd: FtdThresholds = DEFAULT_FTD_THRESHOLDS


DEFAULT_GATE_THRESHOLDS = GateThresholds()
DEFAULT_REGIME_THRESHOLDS = RegimeThresholds(
    gate=DEFAULT_GATE_THRESHOLDS,
    distribution=DistributionThresholds(),
)


def evaluate_market_gate(
    spy_close: float | None,
    spy_sma200: float | None,
    vix_close: float | None,
    *,
    thresholds: GateThresholds = DEFAULT_GATE_THRESHOLDS,
) -> MarketGate:
    """Classify the market without I/O or wall-clock access.

    The trend state uses a 3% SMA200 buffer. The VIX threshold is a separate
    panic flag because VIX values from 20 through 30 intentionally do not
    change the trend state or exposure decision.
    """
    is_panic = vix_close is not None and vix_close > thresholds.bear_vix_min
    if spy_close is None or spy_sma200 is None or vix_close is None:
        # VIX is an independent hard stop. Preserve a visible panic even when
        # the trend inputs are incomplete, so UNKNOWN cannot loosen VIX > 30.
        return MarketGate(
            GateVerdict.UNKNOWN, spy_close, spy_sma200, vix_close, is_panic
        )
    if spy_close < spy_sma200 * thresholds.bear_spy_sma_ratio:
        verdict = GateVerdict.BEAR
    elif spy_close < spy_sma200:
        verdict = GateVerdict.NEUTRAL
    else:
        verdict = GateVerdict.BULL
    return MarketGate(verdict, spy_close, spy_sma200, vix_close, is_panic)


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
    spy_sma_series = (
        sma(spy["close"], thresholds.gate.sma_period) if not spy.empty else None
    )
    spy_sma200 = (
        float(spy_sma_series.iloc[-1])
        if spy_sma_series is not None
        and not spy_sma_series.empty
        and spy_sma_series.notna().iloc[-1]
        else None
    )
    vix_close = float(vix.iloc[-1]["close"]) if not vix.empty else None
    gate = evaluate_market_gate(
        spy_close,
        spy_sma200,
        vix_close,
        thresholds=thresholds.gate,
    )
    spy_distribution = calculate_distribution_days(
        spy, as_of, thresholds=thresholds.distribution
    )
    qqq_distribution = calculate_distribution_days(
        qqq, as_of, thresholds=thresholds.distribution
    )
    ftd = calculate_ftd_snapshot(
        spy,
        qqq,
        as_of,
        thresholds=thresholds.ftd,
        spy_sma_period=thresholds.gate.sma_period,
    )
    if (
        spy_distribution.level is DistributionLevel.UNKNOWN
        or qqq_distribution.level is DistributionLevel.UNKNOWN
    ):
        dd_level = DistributionLevel.UNKNOWN
    else:
        levels = (spy_distribution.level, qqq_distribution.level)
        dd_level = max(levels, key=distribution_severity)
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
        ftd,
    )
