"""P3-16 Follow-Through Day state-machine contracts."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from swing_copilot.regime.distribution import DataQuality
from swing_copilot.regime.ftd import FtdState, calculate_ftd, calculate_ftd_snapshot


def _bars(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    start = date(2020, 3, 1)
    return pd.DataFrame(
        {
            "date": [start + timedelta(days=index) for index in range(len(closes))],
            "close": closes,
            "low": [close - 0.5 for close in closes],
            "high": [close + 0.5 for close in closes],
            "volume": volumes or [100.0] * len(closes),
        }
    )


def test_detects_correction_day1_and_day5_ftd_with_medium_quality():
    bars = _bars(
        [100.0, 99.0, 98.0, 97.0, 98.0, 98.2, 98.4, 98.6, 100.079],
        [100, 100, 100, 100, 100, 100, 100, 100, 150],
    )

    result = calculate_ftd("SPY", bars, date(2020, 3, 9))

    assert result.state is FtdState.FTD_CONFIRMED
    assert result.day_number == 5
    assert result.quality_score == 70
    assert [transition.state for transition in result.transitions] == [
        FtdState.CORRECTION_CONFIRMED,
        FtdState.DAY1,
        FtdState.DAY2_3,
        FtdState.FTD_CONFIRMED,
    ]


def test_day1_low_break_resets_to_correction_confirmed():
    bars = _bars([100.0, 99.0, 98.0, 97.0, 98.0, 98.1])
    bars.loc[5, "low"] = 97.4  # Day1 low was 97.5; strictly below resets.

    result = calculate_ftd("SPY", bars, date(2020, 3, 6))

    assert result.state is FtdState.CORRECTION_CONFIRMED
    assert result.day_number is None


def test_exact_correction_day1_midpoint_and_ftd_boundaries_are_inclusive():
    bars = _bars(
        [100.0, 99.0, 98.0, 97.0, 97.0, 97.1, 97.2, 98.415],
        [100, 100, 100, 100, 100, 100, 100, 101],
    )
    bars.loc[4, ["low", "high"]] = [96.0, 98.0]  # close 97 exactly midpoint.

    result = calculate_ftd("SPY", bars, date(2020, 3, 8))

    assert result.state is FtdState.FTD_CONFIRMED
    assert result.day_number == 4
    assert result.quality_score == 65


def test_day10_without_ftd_expires():
    bars = _bars(
        [
            100.0,
            99.0,
            98.0,
            97.0,
            98.0,
            98.1,
            98.2,
            98.3,
            98.4,
            98.5,
            98.6,
            98.7,
            98.8,
            98.9,
        ]
    )

    result = calculate_ftd("SPY", bars, date(2020, 3, 14))

    assert result.state is FtdState.EXPIRED
    assert result.day_number is None


def test_simultaneous_spy_and_qqq_confirmation_adds_fifteen_points():
    bars = _bars(
        [100.0, 99.0, 98.0, 97.0, 98.0, 98.2, 98.4, 98.6, 100.079],
        [100, 100, 100, 100, 100, 100, 100, 100, 150],
    )

    result = calculate_ftd_snapshot(bars, bars, date(2020, 3, 9))

    assert result.spy.quality_score == 85
    assert result.qqq.quality_score == 85
    assert result.spy.transitions[-1].quality_score == 85
    assert result.qqq.transitions[-1].quality_score == 85


def test_confirmations_on_different_days_do_not_get_the_simultaneous_bonus():
    spy = _bars(
        [100.0, 99.0, 98.0, 97.0, 98.0, 98.2, 98.4, 98.6, 100.079],
        [100, 100, 100, 100, 100, 100, 100, 100, 150],
    )
    qqq = _bars(
        [100.0, 99.0, 98.0, 97.0, 98.0, 98.2, 98.4, 98.6, 98.8, 100.282],
        [100, 100, 100, 100, 100, 100, 100, 100, 100, 150],
    )

    result = calculate_ftd_snapshot(spy, qqq, date(2020, 3, 10))

    assert result.spy.confirmed_at == date(2020, 3, 9)
    assert result.qqq.confirmed_at == date(2020, 3, 10)
    assert result.spy.quality_score == 70
    assert result.qqq.quality_score == 70


def test_future_rows_do_not_affect_as_of_and_missing_data_is_fail_soft():
    bars = _bars(
        [100.0, 99.0, 98.0, 97.0, 98.0, 98.2, 98.4, 98.6, 100.079],
        [100, 100, 100, 100, 100, 100, 100, 100, 150],
    )
    before_ftd = calculate_ftd("SPY", bars, date(2020, 3, 8))
    after_ftd = calculate_ftd("SPY", bars, date(2020, 3, 9))
    missing = calculate_ftd("QQQ", bars.iloc[:1], date(2020, 3, 1))

    assert before_ftd.state is FtdState.DAY2_3
    assert after_ftd.state is FtdState.FTD_CONFIRMED
    assert missing.state is FtdState.UNKNOWN
    assert missing.data_quality is DataQuality.INSUFFICIENT
