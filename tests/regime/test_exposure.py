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
from swing_copilot.regime.ftd import FtdResult, FtdSnapshot, FtdState
from swing_copilot.regime.gate import GateVerdict, MarketGate, RegimeSnapshot


def _snapshot(
    gate: GateVerdict,
    dd_level: DistributionLevel,
    *,
    data_quality: DataQuality = DataQuality.OK,
    ftd_state: FtdState | None = None,
    is_panic: bool = False,
) -> RegimeSnapshot:
    distribution = DistributionResult(0.0, 0.0, 0.0, dd_level, data_quality)
    ftd = None
    if ftd_state is not None:
        ftd = FtdSnapshot(
            date(2026, 7, 21),
            FtdResult("SPY", ftd_state, DataQuality.OK, None, None, None, ()),
            FtdResult(
                "QQQ",
                FtdState.AWAITING_CORRECTION,
                DataQuality.OK,
                None,
                None,
                None,
                (),
            ),
        )
    return RegimeSnapshot(
        date(2026, 7, 21),
        MarketGate(gate, 520.0, 500.0, 15.0, is_panic),
        distribution,
        distribution,
        dd_level,
        data_quality,
        ftd,
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
        (GateVerdict.BULL, DistributionLevel.HIGH, ExposureVerdict.NEW_ENTRY_ALLOWED),
        (GateVerdict.NEUTRAL, DistributionLevel.NORMAL, ExposureVerdict.REDUCE_ONLY),
        (GateVerdict.BEAR, DistributionLevel.NORMAL, ExposureVerdict.CASH_PRIORITY),
        (GateVerdict.BULL, DistributionLevel.SEVERE, ExposureVerdict.REDUCE_ONLY),
    ],
)
def test_maps_gate_and_distribution_to_strictest_exposure(
    gate: GateVerdict, dd_level: DistributionLevel, expected: ExposureVerdict
) -> None:
    assert determine_exposure(_snapshot(gate, dd_level)).verdict is expected


def test_ftd_allows_reentry_below_the_sma200_buffer() -> None:
    decision = determine_exposure(
        _snapshot(
            GateVerdict.BEAR,
            DistributionLevel.SEVERE,
            ftd_state=FtdState.FTD_CONFIRMED,
        )
    )

    assert decision.verdict is ExposureVerdict.REDUCE_ONLY
    assert decision.is_ftd_active


def test_panic_vix_overrides_ftd_reentry() -> None:
    decision = determine_exposure(
        _snapshot(
            GateVerdict.BULL,
            DistributionLevel.NORMAL,
            ftd_state=FtdState.FTD_CONFIRMED,
            is_panic=True,
        )
    )

    assert decision.verdict is ExposureVerdict.CASH_PRIORITY
    assert not decision.is_ftd_active


@pytest.mark.parametrize(
    ("gate", "dd_level", "expected"),
    [
        (GateVerdict.UNKNOWN, DistributionLevel.NORMAL, ExposureVerdict.REDUCE_ONLY),
        (GateVerdict.UNKNOWN, DistributionLevel.SEVERE, ExposureVerdict.CASH_PRIORITY),
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
