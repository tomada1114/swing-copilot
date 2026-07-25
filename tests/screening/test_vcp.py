"""P5-24 VCP contraction and boundary contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest

from swing_copilot.screening.base import ScreeningInput
from swing_copilot.screening.pipeline import ScreeningPipeline
from swing_copilot.screening.technical_signals import VcpBreakoutSignal
from swing_copilot.screening.vcp import (
    SwingPoint,
    VcpPattern,
    VcpThresholds,
    classify_dry_up,
    detect_atr_zigzag,
    extract_pattern,
    is_chasing_pivot,
    validate_contractions,
)
from tests.screening.conftest import make_bars

if TYPE_CHECKING:
    from swing_copilot.config import Settings


@pytest.mark.parametrize(
    ("depths", "expected"),
    [
        ([0.18, 0.135], True),  # exactly 0.75x is valid
        ([0.20, 0.152], False),  # 0.76x is not a contraction
    ],
)
def test_contraction_ratio_boundary(depths: list[float], expected: bool) -> None:
    result = validate_contractions(depths, pattern_days=60, is_small_cap=False)

    assert result.is_valid is expected


@pytest.mark.parametrize(
    ("depth", "is_small_cap", "expected"),
    [
        (0.08, False, True),
        (0.35, False, True),
        (0.40, False, False),
        (0.40, True, True),
    ],
)
def test_first_contraction_depth_boundaries(
    depth: float, is_small_cap: bool, expected: bool
) -> None:
    result = validate_contractions([depth, depth * 0.75], 15, is_small_cap)

    assert result.is_valid is expected


def test_one_contraction_and_short_history_are_not_valid() -> None:
    assert not validate_contractions([0.18], 60, False).is_valid
    assert not validate_contractions([0.18, 0.12], 14, False).is_valid


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(0.29, "ideal"), (0.30, "normal"), (0.71, "weak")],
)
def test_dry_up_boundaries(ratio: float, expected: str) -> None:
    assert classify_dry_up(ratio) == expected


def test_chasing_is_strictly_above_five_percent() -> None:
    assert not is_chasing_pivot(105.0, 100.0)
    assert is_chasing_pivot(105.01, 100.0)


def test_thresholds_expose_roadmap_defaults() -> None:
    assert VcpThresholds().zigzag_atr_multiplier == 2.0


def test_atr_zigzag_extracts_two_decreasing_contractions_and_pivot() -> None:
    closes = pd.Series([100.0, 99.0, 82.0, 85.0, 95.0, 94.0, 92.0, 94.0])
    atr = pd.Series([1.0] * len(closes))
    volumes = pd.Series([2_000_000] * len(closes))

    swings = detect_atr_zigzag(closes, atr, atr_multiplier=2.0)
    pattern = extract_pattern(swings, volumes)

    assert swings == (
        SwingPoint(0, "high", 100.0),
        SwingPoint(2, "low", 82.0),
        SwingPoint(4, "high", 95.0),
        SwingPoint(6, "low", 92.0),
    )
    assert pattern is not None
    assert pattern.depths == pytest.approx((0.18, 3.0 / 95.0))
    assert pattern.pivot == 95.0


def test_vcp_signal_records_pattern_metrics_and_rejects_chasing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = make_bars(
        "VCP",
        [100.0 + index * 0.1 for index in range(60)],
        start=pd.Timestamp("2026-01-01").date(),
    )
    data = ScreeningInput(
        as_of=pd.Timestamp("2026-12-31").date(),
        universe=(),
        fundamentals=pd.DataFrame(),
        bars=bars,
    )
    pattern = VcpPattern(
        depths=(0.18, 0.13),
        pattern_days=60,
        pivot=110.0,
        pivot_index=55,
        dry_up_ratio=0.25,
        dry_up_class="ideal",
    )
    monkeypatch.setattr(
        "swing_copilot.screening.technical_signals.detect_atr_zigzag",
        lambda *_args: (),
    )
    monkeypatch.setattr(
        "swing_copilot.screening.technical_signals.extract_pattern",
        lambda *_args: pattern,
    )

    hits = VcpBreakoutSignal(settings).evaluate(data, {"VCP"})

    assert len(hits) == 1
    assert hits[0].metrics["vcp_contraction_count"] == 2.0
    assert hits[0].metrics["vcp_depth_2"] == 0.13


def test_vcp_pipeline_classifies_a_non_hit_without_an_exception(
    settings: Settings,
) -> None:
    bars = make_bars("VCP", [100.0] * 60, start=pd.Timestamp("2026-01-01").date())
    data = ScreeningInput(
        as_of=pd.Timestamp("2026-12-31").date(),
        universe=(),
        fundamentals=pd.DataFrame(),
        bars=bars,
    )
    strategies = {
        "strategies": {
            "vcp": {
                "filters_all": [],
                "signals_all": ["vcp_breakout"],
                "candidate_limit": 10,
            }
        }
    }

    assert ScreeningPipeline(strategies, None, settings, "vcp").run(data) == []
