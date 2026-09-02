"""P5-24 VCP contraction and boundary contracts."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from swing_copilot.config import StrategiesConfig
from swing_copilot.screening.base import ScreeningInput, SignalHit
from swing_copilot.screening.pipeline import ScreeningPipeline
from swing_copilot.screening.technical_signals import VcpBreakoutSignal
from swing_copilot.screening.vcp import (
    SwingPoint,
    VcpPattern,
    VcpThresholds,
    classify_dry_up,
    detect_atr_zigzag,
    evaluate_vcp,
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


def _alternating_swings(low_prices: list[float]) -> tuple[SwingPoint, ...]:
    """High(100)->low pairs spaced 5 bars apart, starting at index 60."""
    swings: list[SwingPoint] = []
    for pair_index, low in enumerate(low_prices):
        base = 60 + pair_index * 10
        swings.append(SwingPoint(base, "high", 100.0))
        swings.append(SwingPoint(base + 5, "low", low))
    return tuple(swings)


def test_extract_pattern_keeps_only_the_most_recent_max_contractions() -> None:
    # Five contractions with depths 0.5/0.4/0.3/0.2/0.1; the default
    # max_contractions=4 must drop the oldest one entirely.
    swings = _alternating_swings([50.0, 60.0, 70.0, 80.0, 90.0])
    volumes = pd.Series([2_000_000] * 110)

    pattern = extract_pattern(swings, volumes)

    assert pattern is not None
    assert pattern.depths == pytest.approx((0.4, 0.3, 0.2, 0.1))
    # pattern_days spans the *retained* window: high at 70 to low at 105.
    assert pattern.pattern_days == 36
    assert pattern.pivot == 100.0


def test_extract_pattern_window_tracks_configured_max_contractions() -> None:
    swings = _alternating_swings([50.0, 60.0, 70.0, 80.0, 90.0])
    volumes = pd.Series([2_000_000] * 110)

    pattern = extract_pattern(swings, volumes, VcpThresholds(max_contractions=2))

    assert pattern is not None
    assert pattern.depths == pytest.approx((0.2, 0.1))
    assert pattern.pattern_days == 16


def _vcp_hit_closes() -> list[float]:
    """A rising warmup ending in a two-contraction VCP that must hit.

    Depths are 22.8/110.8 (~0.206) then 9.9/99 (0.1, a <=0.75x
    contraction); the final close 93.0 sits below the 99.0 pivot.
    """
    closes = [100.0 + index * 0.03 for index in range(360)]  # ends at 110.77
    high1 = 110.8
    closes.append(high1)
    closes += [high1 - (index + 1) * 2.85 for index in range(8)]  # low 88.0
    closes += [88.0 + (index + 1) * 1.375 for index in range(8)]  # high 99.0
    closes += [99.0 - (index + 1) * 1.2375 for index in range(8)]  # low 89.1
    closes += [89.1 + (index + 1) * 0.65 for index in range(6)]  # close 93.0
    return closes


def test_vcp_verdict_is_independent_of_supplied_history_length(
    settings: Settings,
) -> None:
    # Issue #186 DoD: the same symbol/as_of must screen identically whether
    # the caller supplied the daily pipeline's shorter window or the
    # backtest's longer one. The long supply prepends old turbulence whose
    # 50%-deep swings would have joined (and invalidated) the pattern under
    # the unbounded pre-#186 definition.
    recent = _vcp_hit_closes()
    turbulence: list[float] = []
    for _cycle in range(10):
        turbulence += [100.0 - index * 5.0 for index in range(10)]  # to 55.0
        turbulence += [55.0 + index * 5.0 for index in range(10)]  # back to 100
    start = pd.Timestamp("2024-01-01").date()
    long_bars = make_bars("VCP", turbulence + recent, start=start)
    short_bars = make_bars("VCP", recent, start=start + timedelta(days=len(turbulence)))
    as_of = start + timedelta(days=len(turbulence) + len(recent))
    signal = VcpBreakoutSignal(settings)

    def _evaluate(bars: pd.DataFrame) -> list[SignalHit]:
        data = ScreeningInput(
            as_of=as_of,
            universe=(),
            fundamentals=pd.DataFrame(),
            bars=bars,
        )
        return signal.evaluate(data, {"VCP"})

    long_hits = _evaluate(long_bars)
    short_hits = _evaluate(short_bars)

    assert len(long_hits) == 1
    assert long_hits == short_hits


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
    # Patched on `vcp` rather than `technical_signals`: Issue #188 moved the
    # zigzag -> pattern -> validate -> chase sequence into `evaluate_vcp`, so
    # the signal and the rejection classifier decide with the same code.
    monkeypatch.setattr(
        "swing_copilot.screening.vcp.detect_atr_zigzag",
        lambda *_args: (),
    )
    monkeypatch.setattr(
        "swing_copilot.screening.vcp.extract_pattern",
        lambda *_args: pattern,
    )

    hits = VcpBreakoutSignal(settings).evaluate(data, {"VCP"})

    assert len(hits) == 1
    assert hits[0].metrics["vcp_contraction_count"] == 2.0
    assert hits[0].metrics["vcp_depth_2"] == 0.13


@pytest.mark.parametrize(
    ("dry_up_ratio", "expected_reason"),
    [
        pytest.param(None, "DRY_UP_UNAVAILABLE", id="no-volume-baseline"),
        pytest.param(0.25, None, id="valid-setup"),
    ],
)
def test_evaluate_vcp_reports_the_stage_a_setup_stopped_at(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    dry_up_ratio: float | None,
    expected_reason: str | None,
) -> None:
    # Issue #188: `miss_reason` is the single verdict both the signal and the
    # rejection classifier read, so every stage must be nameable -- including
    # the one where the dry-up baseline could not be measured at all.
    bars = make_bars(
        "VCP",
        [100.0 + index * 0.1 for index in range(60)],
        start=pd.Timestamp("2026-01-01").date(),
    )
    pattern = VcpPattern(
        depths=(0.18, 0.13),
        pattern_days=60,
        pivot=200.0,
        pivot_index=55,
        dry_up_ratio=dry_up_ratio,
        dry_up_class=None if dry_up_ratio is None else "ideal",
    )
    monkeypatch.setattr(
        "swing_copilot.screening.vcp.extract_pattern", lambda *_args: pattern
    )

    evaluation = evaluate_vcp(
        bars, VcpThresholds(**settings.technical_signals.vcp.model_dump())
    )

    assert evaluation.miss_reason == expected_reason
    assert evaluation.is_hit is (expected_reason is None)
    assert evaluation.close == pytest.approx(105.9)


def test_vcp_signal_emits_no_hit_for_a_symbol_it_has_no_bars_for(
    settings: Settings,
) -> None:
    data = ScreeningInput(
        as_of=pd.Timestamp("2026-12-31").date(),
        universe=(),
        fundamentals=pd.DataFrame(),
        bars=make_bars("OTHER", [100.0] * 60, start=pd.Timestamp("2026-01-01").date()),
    )

    assert VcpBreakoutSignal(settings).evaluate(data, {"VCP"}) == []


def test_vcp_signal_emits_no_hit_when_the_evaluation_is_a_miss(
    settings: Settings,
) -> None:
    # A flat series produces no admissible contraction sequence at all.
    data = ScreeningInput(
        as_of=pd.Timestamp("2026-12-31").date(),
        universe=(),
        fundamentals=pd.DataFrame(),
        bars=make_bars("VCP", [100.0] * 60, start=pd.Timestamp("2026-01-01").date()),
    )

    assert VcpBreakoutSignal(settings).evaluate(data, {"VCP"}) == []


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
    strategies = StrategiesConfig.model_validate(
        {
            "strategies": {
                "vcp": {
                    "filters_all": [],
                    "signals_all": ["vcp_breakout"],
                    "candidate_limit": 10,
                }
            }
        }
    )

    assert ScreeningPipeline(strategies, None, settings, "vcp").run(data) == []
