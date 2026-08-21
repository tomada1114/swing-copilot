"""Pure, point-in-time Follow-Through Day state machine (P3-16)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import isnan
from typing import TYPE_CHECKING

from swing_copilot.regime.distribution import DataQuality

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd


class FtdState(StrEnum):
    """Explicit FTD lifecycle states used by the market exposure label."""

    UNKNOWN = "UNKNOWN"
    AWAITING_CORRECTION = "AWAITING_CORRECTION"
    CORRECTION_CONFIRMED = "CORRECTION_CONFIRMED"
    DAY1 = "DAY1"
    DAY2_3 = "DAY2_3"
    FTD_CONFIRMED = "FTD_CONFIRMED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class FtdThresholds:
    """Configurable P3-16 thresholds (roadmap §5; values require validation)."""

    correction_decline_pct: float = 0.03
    correction_down_days: int = 3
    ftd_gain_pct: float = 0.0125
    quality_add_at_min_gain: int = 5
    quality_add_at_medium_gain: int = 10
    quality_add_at_high_gain: int = 20
    simultaneous_confirmation_bonus: int = 15


DEFAULT_FTD_THRESHOLDS = FtdThresholds()
_MIN_BARS = 2
_FTD_FIRST_DAY = 4
_FTD_LAST_DAY = 10
_EARLY_FTD_LAST_DAY = 7
_MEDIUM_GAIN_PCT = 0.015
_HIGH_GAIN_PCT = 0.02
_FLOAT_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class FtdTransition:
    """One state change, kept for DuckDB audit history."""

    date: date
    state: FtdState
    day_number: int | None
    quality_score: int | None


@dataclass(frozen=True, slots=True)
class FtdResult:
    """Current FTD state for one index, plus its deterministic state history."""

    symbol: str
    state: FtdState
    data_quality: DataQuality
    day_number: int | None
    quality_score: int | None
    confirmed_at: date | None
    transitions: tuple[FtdTransition, ...]
    ftd_day_low: float | None = None


@dataclass(frozen=True, slots=True)
class FtdSnapshot:
    """SPY/QQQ FTD outcomes for one point-in-time regime snapshot."""

    as_of: date
    spy: FtdResult
    qqq: FtdResult


@dataclass(frozen=True, slots=True)
class _Machine:
    state: FtdState = FtdState.AWAITING_CORRECTION
    day1_low: float | None = None
    day_number: int | None = None
    quality_score: int | None = None
    confirmed_at: date | None = None
    ftd_day_low: float | None = None


@dataclass(frozen=True, slots=True)
class _DayObservation:
    close: float
    previous_close: float
    low: float
    high: float
    volume: float
    previous_volume: float


def calculate_ftd_snapshot(
    spy_bars: pd.DataFrame,
    qqq_bars: pd.DataFrame,
    as_of: date,
    *,
    thresholds: FtdThresholds = DEFAULT_FTD_THRESHOLDS,
    spy_sma_period: int | None = None,
) -> FtdSnapshot:
    """Calculate both FTD states using rows visible at ``as_of`` only.

    ``spy_sma_period`` is optional so the standalone FTD diagnostic remains
    independent. The production regime path supplies SMA200 so a confirmed
    FTD expires when the index has recovered to the normal trend regime.
    """
    spy = calculate_ftd(
        "SPY",
        spy_bars,
        as_of,
        thresholds=thresholds,
        sma_period=spy_sma_period,
    )
    qqq = calculate_ftd("QQQ", qqq_bars, as_of, thresholds=thresholds)
    if (
        spy.state is FtdState.FTD_CONFIRMED
        and qqq.state is FtdState.FTD_CONFIRMED
        and spy.confirmed_at == qqq.confirmed_at
    ):
        spy = _apply_simultaneous_bonus(spy, thresholds)
        qqq = _apply_simultaneous_bonus(qqq, thresholds)
    return FtdSnapshot(as_of, spy, qqq)


def calculate_ftd(
    symbol: str,
    bars: pd.DataFrame,
    as_of: date,
    *,
    thresholds: FtdThresholds = DEFAULT_FTD_THRESHOLDS,
    sma_period: int | None = None,
) -> FtdResult:
    """Run one explicit FTD state machine over a point-in-time OHLCV series.

    When supplied, ``sma_period`` expires a confirmed SPY FTD on the first
    visible close at or above that SMA. This is a state transition, rather
    than a final-result filter, so a later dip cannot resurrect an old FTD.
    """
    visible = bars.loc[bars["date"] <= as_of].sort_values("date").reset_index(drop=True)
    if len(visible) < _MIN_BARS or not {"low", "high"}.issubset(visible.columns):
        return FtdResult(
            symbol, FtdState.UNKNOWN, DataQuality.INSUFFICIENT, None, None, None, ()
        )

    machine = _Machine()
    transitions: list[FtdTransition] = []
    rolling_high = float(visible.iloc[0]["close"])
    down_days = 0
    sma_values = (
        visible["close"].rolling(window=sma_period, min_periods=sma_period).mean()
        if sma_period is not None
        else None
    )
    for index in range(1, len(visible)):
        previous = visible.iloc[index - 1]
        current = visible.iloc[index]
        date_value = current["date"]
        close = float(current["close"])
        previous_close = float(previous["close"])
        low = float(current["low"])
        high = float(current["high"])
        volume = float(current["volume"])
        previous_volume = float(previous["volume"])
        if close > rolling_high:
            rolling_high = close
            down_days = 0
        elif close < previous_close:
            down_days += 1
        else:
            down_days = 0

        before = machine
        machine = transition(
            machine,
            _DayObservation(close, previous_close, low, high, volume, previous_volume),
            correction_observed=(
                close <= rolling_high * (1.0 - thresholds.correction_decline_pct)
                and down_days >= thresholds.correction_down_days
            ),
            thresholds=thresholds,
        )
        current_sma = (
            float(sma_values.iloc[index])
            if sma_values is not None and not isnan(float(sma_values.iloc[index]))
            else None
        )
        if (
            machine.state is FtdState.FTD_CONFIRMED
            and current_sma is not None
            and close >= current_sma
        ):
            machine = _Machine(FtdState.EXPIRED)
        if (
            machine.state is FtdState.FTD_CONFIRMED
            and before.state is not FtdState.FTD_CONFIRMED
        ):
            machine = replace(machine, confirmed_at=date_value)
        if machine.state is not before.state:
            transitions.append(
                FtdTransition(
                    date_value,
                    machine.state,
                    machine.day_number,
                    machine.quality_score,
                )
            )
    return FtdResult(
        symbol,
        machine.state,
        DataQuality.OK,
        machine.day_number,
        machine.quality_score,
        machine.confirmed_at,
        tuple(transitions),
        machine.ftd_day_low,
    )


def transition(  # noqa: PLR0911 - each explicit state branch is part of the audit trail
    machine: _Machine,
    observation: _DayObservation,
    correction_observed: bool,
    thresholds: FtdThresholds = DEFAULT_FTD_THRESHOLDS,
) -> _Machine:
    """Pure one-day transition for the FTD state machine."""
    if machine.state in (FtdState.AWAITING_CORRECTION, FtdState.EXPIRED):
        return (
            _Machine(FtdState.CORRECTION_CONFIRMED) if correction_observed else machine
        )
    if machine.state is FtdState.CORRECTION_CONFIRMED:
        is_day1 = (
            observation.close > observation.previous_close
            or observation.close >= (observation.low + observation.high) / 2.0
        )
        return _Machine(FtdState.DAY1, observation.low, 1) if is_day1 else machine
    if machine.state in (FtdState.DAY1, FtdState.DAY2_3):
        if machine.day1_low is not None and observation.low < machine.day1_low:
            return _Machine(FtdState.CORRECTION_CONFIRMED)
        day_number = (machine.day_number or 1) + 1
        gain = observation.close / observation.previous_close - 1.0
        if (
            _FTD_FIRST_DAY <= day_number <= _FTD_LAST_DAY
            and gain + _FLOAT_TOLERANCE >= thresholds.ftd_gain_pct
            and observation.volume > observation.previous_volume
        ):
            return _Machine(
                FtdState.FTD_CONFIRMED,
                machine.day1_low,
                day_number,
                _quality_score(day_number, gain, thresholds),
                None,
                observation.low,
            )
        if day_number >= _FTD_LAST_DAY:
            return _Machine(FtdState.EXPIRED)
        return _Machine(FtdState.DAY2_3, machine.day1_low, day_number)
    if machine.state is FtdState.FTD_CONFIRMED:
        if machine.ftd_day_low is not None and observation.close < machine.ftd_day_low:
            return _Machine(FtdState.EXPIRED)
        return machine
    return machine


def _quality_score(day_number: int, gain: float, thresholds: FtdThresholds) -> int:
    base = 60 if _FTD_FIRST_DAY <= day_number <= _EARLY_FTD_LAST_DAY else 50
    if gain + _FLOAT_TOLERANCE >= _HIGH_GAIN_PCT:
        bonus = thresholds.quality_add_at_high_gain
    elif gain + _FLOAT_TOLERANCE >= _MEDIUM_GAIN_PCT:
        bonus = thresholds.quality_add_at_medium_gain
    else:
        bonus = thresholds.quality_add_at_min_gain
    return base + bonus


def _with_simultaneous_bonus(
    score: int | None, thresholds: FtdThresholds
) -> int | None:
    return (
        score + thresholds.simultaneous_confirmation_bonus
        if score is not None
        else None
    )


def _apply_simultaneous_bonus(
    result: FtdResult, thresholds: FtdThresholds
) -> FtdResult:
    score = _with_simultaneous_bonus(result.quality_score, thresholds)
    if not result.transitions:
        return replace(result, quality_score=score)
    last = result.transitions[-1]
    transitions = (*result.transitions[:-1], replace(last, quality_score=score))
    return replace(result, quality_score=score, transitions=transitions)
