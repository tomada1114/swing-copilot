"""Deterministic primitives for the P5-24 Volatility Contraction Pattern."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import pandas as pd

_MIN_ZIGZAG_BARS = 3
_MIN_PATTERN_CONTRACTIONS = 2
_PIVOT_VOLUME_WINDOW = 10
_VOLUME_BASELINE_WINDOW = 50


@dataclass(frozen=True, slots=True)
class VcpThresholds:
    """VCP defaults from roadmap §5 P5-24 (all marked 要検証 there)."""

    zigzag_atr_multiplier: float = 2.0
    first_depth_min: float = 0.08
    first_depth_max: float = 0.35
    small_cap_first_depth_max: float = 0.50
    contraction_ratio_max: float = 0.75
    min_contractions: int = 2
    pattern_days_min: int = 15
    pattern_days_max: int = 325
    dry_up_ideal_max: float = 0.30
    dry_up_weak_min: float = 0.70
    chase_pivot_pct: float = 0.05


@dataclass(frozen=True, slots=True)
class ContractionValidation:
    """Deterministic VCP validation outcome, including an inspectable reason."""

    is_valid: bool
    reason: str | None = None


_DEFAULT_THRESHOLDS = VcpThresholds()


@dataclass(frozen=True, slots=True)
class SwingPoint:
    """One ATR-filtered local price extreme."""

    index: int
    kind: str  # "high" or "low"
    price: float


@dataclass(frozen=True, slots=True)
class VcpPattern:
    """Detected VCP evidence used by the screening signal and reports."""

    depths: tuple[float, ...]
    pattern_days: int
    pivot: float
    pivot_index: int
    dry_up_ratio: float | None
    dry_up_class: str | None


def detect_atr_zigzag(
    closes: pd.Series, atr: pd.Series, atr_multiplier: float
) -> tuple[SwingPoint, ...]:
    """Find alternating local extremes whose reversal clears an ATR threshold.

    The detector deliberately operates on the close series: it avoids
    intraday extrema that are unavailable to the end-of-day decision at
    `as_of`, while ATR supplies the volatility-scaled minimum reversal.
    """
    if len(closes) < _MIN_ZIGZAG_BARS or len(closes) != len(atr):
        return ()
    candidates: list[SwingPoint] = []
    for index in range(1, len(closes) - 1):
        previous = float(closes.iloc[index - 1])
        current = float(closes.iloc[index])
        following = float(closes.iloc[index + 1])
        if current >= previous and current > following:
            candidates.append(SwingPoint(index, "high", current))
        elif current <= previous and current < following:
            candidates.append(SwingPoint(index, "low", current))
    if float(closes.iloc[0]) > float(closes.iloc[1]):
        candidates.insert(0, SwingPoint(0, "high", float(closes.iloc[0])))
    elif float(closes.iloc[0]) < float(closes.iloc[1]):
        candidates.insert(0, SwingPoint(0, "low", float(closes.iloc[0])))

    swings: list[SwingPoint] = []
    for point in candidates:
        atr_value = float(atr.iloc[point.index])
        if pd.isna(atr_value) or atr_value <= 0.0:
            continue
        if not swings:
            swings.append(point)
            continue
        prior = swings[-1]
        if prior.kind == point.kind:
            if (point.kind == "high" and point.price > prior.price) or (
                point.kind == "low" and point.price < prior.price
            ):
                swings[-1] = point
            continue
        if abs(point.price - prior.price) >= atr_multiplier * atr_value:
            swings.append(point)
    return tuple(swings)


def extract_pattern(
    swings: tuple[SwingPoint, ...], volumes: pd.Series
) -> VcpPattern | None:
    """Extract high-to-low contractions and final-contraction pivot evidence."""
    contractions: list[tuple[SwingPoint, SwingPoint]] = []
    for high, low in pairwise(swings):
        if high.kind == "high" and low.kind == "low" and high.price > 0.0:
            contractions.append((high, low))
    if len(contractions) < _MIN_PATTERN_CONTRACTIONS:
        return None
    depths = tuple((high.price - low.price) / high.price for high, low in contractions)
    first_high = contractions[0][0]
    final_high = contractions[-1][0]
    final_low = contractions[-1][1]
    start = max(0, final_high.index - _PIVOT_VOLUME_WINDOW)
    before_pivot = volumes.iloc[start : final_high.index]
    baseline = volumes.iloc[
        max(0, final_high.index - _VOLUME_BASELINE_WINDOW) : final_high.index
    ]
    ratio = None
    if (
        len(before_pivot) == _PIVOT_VOLUME_WINDOW
        and len(baseline) == _VOLUME_BASELINE_WINDOW
        and float(baseline.mean()) > 0.0
    ):
        ratio = float(before_pivot.mean() / baseline.mean())
    return VcpPattern(
        depths=depths,
        pattern_days=final_low.index - first_high.index + 1,
        pivot=final_high.price,
        pivot_index=final_high.index,
        dry_up_ratio=ratio,
        dry_up_class=classify_dry_up(ratio) if ratio is not None else None,
    )


def validate_contractions(
    depths: list[float],
    pattern_days: int,
    is_small_cap: bool,
    thresholds: VcpThresholds = _DEFAULT_THRESHOLDS,
) -> ContractionValidation:
    """Validate VCP depth, decreasing-ratio, count, and duration contracts."""
    if len(depths) < thresholds.min_contractions:
        return ContractionValidation(False, "INSUFFICIENT_CONTRACTIONS")
    maximum = (
        thresholds.small_cap_first_depth_max
        if is_small_cap
        else thresholds.first_depth_max
    )
    first = depths[0]
    if not thresholds.first_depth_min <= first <= maximum:
        return ContractionValidation(False, "FIRST_DEPTH_OUT_OF_RANGE")
    if not thresholds.pattern_days_min <= pattern_days <= thresholds.pattern_days_max:
        return ContractionValidation(False, "PATTERN_DURATION_OUT_OF_RANGE")
    if any(
        later > earlier * thresholds.contraction_ratio_max
        for earlier, later in pairwise(depths)
    ):
        return ContractionValidation(False, "CONTRACTIONS_NOT_DECREASING")
    return ContractionValidation(True)


def classify_dry_up(
    ratio: float, thresholds: VcpThresholds = _DEFAULT_THRESHOLDS
) -> str:
    """Classify pivot-preceding volume without treating exact bounds as ideal."""
    if ratio < thresholds.dry_up_ideal_max:
        return "ideal"
    if ratio > thresholds.dry_up_weak_min:
        return "weak"
    return "normal"


def is_chasing_pivot(
    close: float, pivot: float, thresholds: VcpThresholds = _DEFAULT_THRESHOLDS
) -> bool:
    """Return whether price is strictly more than the allowed pivot extension."""
    return pivot > 0.0 and close > pivot * (1.0 + thresholds.chase_pivot_pct)
