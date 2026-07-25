"""Contracts for P3-14 Exposure Ceiling decisions."""

from __future__ import annotations

from datetime import date

import pytest

from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    DistributionResult,
)
from swing_copilot.regime.exposure import ExposureVerdict, determine_exposure
from swing_copilot.regime.gate import GateVerdict, MarketGate, RegimeSnapshot


def _snapshot(
    gate: GateVerdict,
    dd_level: DistributionLevel,
    *,
    data_quality: DataQuality = DataQuality.OK,
) -> RegimeSnapshot:
    distribution = DistributionResult(0.0, 0.0, 0.0, dd_level, data_quality)
    return RegimeSnapshot(
        date(2026, 7, 21),
        MarketGate(gate, 520.0, 500.0, 15.0),
        distribution,
        distribution,
        dd_level,
        data_quality,
    )


@pytest.mark.parametrize(
    ("gate", "dd_level", "expected"),
    [
        (GateVerdict.BULL, DistributionLevel.NORMAL, ExposureVerdict.NEW_ENTRY_ALLOWED),
        (
            GateVerdict.BULL,
            DistributionLevel.CAUTION,
            ExposureVerdict.NEW_ENTRY_ALLOWED,
        ),
        (GateVerdict.BULL, DistributionLevel.HIGH, ExposureVerdict.REDUCE_ONLY),
        (GateVerdict.NEUTRAL, DistributionLevel.NORMAL, ExposureVerdict.REDUCE_ONLY),
        (GateVerdict.BEAR, DistributionLevel.NORMAL, ExposureVerdict.CASH_PRIORITY),
        (GateVerdict.BULL, DistributionLevel.SEVERE, ExposureVerdict.CASH_PRIORITY),
    ],
)
def test_maps_gate_and_distribution_to_strictest_exposure(
    gate: GateVerdict, dd_level: DistributionLevel, expected: ExposureVerdict
) -> None:
    assert determine_exposure(_snapshot(gate, dd_level)).verdict is expected


@pytest.mark.parametrize(
    ("gate", "dd_level", "expected"),
    [
        (GateVerdict.UNKNOWN, DistributionLevel.NORMAL, ExposureVerdict.REDUCE_ONLY),
        (GateVerdict.UNKNOWN, DistributionLevel.HIGH, ExposureVerdict.CASH_PRIORITY),
        (GateVerdict.BULL, DistributionLevel.UNKNOWN, ExposureVerdict.REDUCE_ONLY),
        (GateVerdict.UNKNOWN, DistributionLevel.UNKNOWN, ExposureVerdict.CASH_PRIORITY),
    ],
)
def test_unknown_input_strictens_base_exposure_one_level(
    gate: GateVerdict, dd_level: DistributionLevel, expected: ExposureVerdict
) -> None:
    assert (
        determine_exposure(
            _snapshot(gate, dd_level, data_quality=DataQuality.INSUFFICIENT)
        ).verdict
        is expected
    )
