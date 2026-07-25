"""Contract tests for the P5-21 Minervini Stage 2 signal."""

from __future__ import annotations

from datetime import date

import pandas as pd

from swing_copilot.screening.base import RejectionReasonCode, ScreeningInput
from swing_copilot.screening.pipeline import ScreeningPipeline
from swing_copilot.screening.technical_signals import MinerviniStage2Signal
from swing_copilot.universe import UniverseMember
from tests.screening.conftest import make_bars

AS_OF = date(2027, 1, 1)


def _member(symbol: str) -> UniverseMember:
    return UniverseMember(symbol, symbol, "Technology", symbol)


def _input(*series: tuple[str, list[float]]) -> ScreeningInput:
    bars = pd.concat(
        [make_bars(symbol, closes, start=date(2026, 1, 1)) for symbol, closes in series]
    )
    return ScreeningInput(
        as_of=AS_OF,
        universe=tuple(_member(symbol) for symbol, _ in series),
        fundamentals=pd.DataFrame(),
        bars=bars,
    )


def test_seven_conditions_are_recorded_and_strength_is_fraction_of_seven(settings):
    # AAPL's weighted return leads its three-member universe, while the
    # monotonic price series satisfies the six non-RS conditions.
    data = _input(
        ("AAPL", [100.0 + index for index in range(253)]),
        ("MSFT", [100.0 + index * 0.6 for index in range(253)]),
        ("NVDA", [100.0 + index * 0.3 for index in range(253)]),
    )

    hits = MinerviniStage2Signal(settings).evaluate(data, {"AAPL", "MSFT", "NVDA"})

    hit = next(hit for hit in hits if hit.symbol == "AAPL")
    assert hit.strength == 1.0
    assert hit.metrics["minervini_criteria_met"] == 7.0
    assert hit.metrics["minervini_rs_percentile"] == 100.0
    assert all(
        hit.metrics[f"minervini_condition_{number}"] == 1.0 for number in range(1, 8)
    )


def test_boundaries_and_configured_minimum_are_inclusive(settings, monkeypatch):
    data = _input(("AAPL", [100.0 + index for index in range(253)]))
    signal = MinerviniStage2Signal(settings)
    monkeypatch.setattr(
        signal, "_rs_percentiles", lambda _data, _symbols: {"AAPL": 70.0}
    )

    hits = signal.evaluate(data, {"AAPL"})

    assert len(hits) == 1
    assert hits[0].metrics["minervini_condition_7"] == 1.0


def test_insufficient_52_week_history_does_not_hit(settings):
    data = _input(("AAPL", [100.0 + index for index in range(200)]))

    hits = MinerviniStage2Signal(settings).evaluate(data, {"AAPL"})

    assert hits == []


def test_insufficient_52_week_history_is_recorded_as_data_quality(settings):
    data = _input(("AAPL", [100.0 + index for index in range(199)]))
    strategies = {
        "strategies": {
            "stage2": {
                "filters_all": [],
                "signals_all": ["minervini_stage2"],
                "candidate_limit": 10,
            }
        }
    }

    result = ScreeningPipeline(
        strategies, None, settings, "stage2"
    ).run_with_rejections(data)

    assert result.candidates == []
    assert (
        result.rejections[0].reason_code
        is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
    )


def test_strategy_min_criteria_config_controls_inclusive_pass_line(settings):
    data = _input(
        ("AAPL", [100.0 + index for index in range(253)]),
        ("MSFT", [100.0 + index * 0.6 for index in range(253)]),
    )
    strategies = {
        "strategies": {
            "stage2": {
                "filters_all": [],
                "signals_all": ["minervini_stage2"],
                "candidate_limit": 10,
                "minervini": {"min_criteria": 7},
            }
        }
    }

    candidates = ScreeningPipeline(strategies, None, settings, "stage2").run(data)

    assert [candidate.symbol for candidate in candidates] == ["AAPL"]
