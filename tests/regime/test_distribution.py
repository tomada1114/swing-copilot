"""Contract tests for Issue #22's Distribution Day calculations."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    calculate_distribution_days,
    distribution_level,
)


def _bars(closes: list[float], volumes: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [
                date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes))
            ],
            "close": closes,
            "volume": volumes,
        }
    )


class TestDistributionDays:
    def test_counts_dd_and_stall_day_independently(self) -> None:
        closes = [100.0] * 26
        volumes = [100] * 26
        closes[5], volumes[5] = 99.8, 101
        closes[10], volumes[10] = 99.95, 102

        result = calculate_distribution_days(_bars(closes, volumes), date(2026, 1, 26))

        assert result.data_quality is DataQuality.OK
        assert result.d25 == pytest.approx(1.5)
        assert result.level is DistributionLevel.NORMAL

    @pytest.mark.parametrize(
        ("count", "quality"),
        [
            (24, DataQuality.INSUFFICIENT),
            (25, DataQuality.INSUFFICIENT),
            (26, DataQuality.OK),
        ],
    )
    def test_requires_26_prices_for_25_day_window(
        self, count: int, quality: DataQuality
    ) -> None:
        result = calculate_distribution_days(
            _bars([100.0] * count, [100] * count), date(2026, 1, 26)
        )

        assert result.data_quality is quality

    def test_invalidates_count_at_exact_five_percent_recovery(self) -> None:
        closes = [100.0] * 26
        volumes = [100] * 26
        closes[5], volumes[5] = 99.8, 101
        closes[6] = 104.789
        assert (
            calculate_distribution_days(_bars(closes, volumes), date(2026, 1, 26)).d25
            == 1
        )

        closes[6] = 104.79  # 99.8 * 1.05 exactly
        assert (
            calculate_distribution_days(_bars(closes, volumes), date(2026, 1, 26)).d25
            == 0
        )

    def test_expires_distribution_day_on_its_twenty_fifth_trading_day(self) -> None:
        closes = [100.0] * 26
        volumes = [100] * 26
        closes[1], volumes[1] = 99.8, 101

        assert (
            calculate_distribution_days(_bars(closes, volumes), date(2026, 1, 26)).d25
            == 0
        )

    @pytest.mark.parametrize(
        ("d25", "d15", "d5", "expected"),
        [
            (2.0, 0.0, 0.0, DistributionLevel.NORMAL),
            (3.0, 0.0, 0.0, DistributionLevel.CAUTION),
            (1.0, 0.0, 2.0, DistributionLevel.HIGH),
            (4.0, 4.0, 0.0, DistributionLevel.SEVERE),
        ],
    )
    def test_selects_strictest_distribution_level(
        self, d25: float, d15: float, d5: float, expected: DistributionLevel
    ) -> None:
        assert distribution_level(d25, d15, d5) is expected
