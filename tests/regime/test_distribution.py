"""Contract tests for Issue #22's Distribution Day calculations."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    DistributionThresholds,
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

    def test_a_gap_after_a_distribution_day_neither_recovers_nor_invalidates_it(
        self,
    ) -> None:
        """A `NaN` close cannot cancel a distribution day by counting as recovery.

        The recovery scan now walks a precomputed running maximum instead of
        re-slicing the tail, so `NaN` has to be skipped explicitly to keep the
        `NaN >= threshold` comparison it replaced. A gap must leave the count
        exactly where a shorter, gap-free history would.
        """
        closes: list[float] = [100.0] * 26
        volumes = [100] * 26
        closes[5], volumes[5] = 99.8, 101
        closes[6] = float("nan")

        assert (
            calculate_distribution_days(_bars(closes, volumes), date(2026, 1, 26)).d25
            == 1
        )

        # A real recovery after the gap still invalidates the day.
        closes[7] = 104.79  # 99.8 * 1.05 exactly
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
            # d25 alone is only CAUTION; the stricter d15 decides the level.
            (4.0, 6.0, 0.0, DistributionLevel.SEVERE),
        ],
    )
    def test_selects_strictest_distribution_level(
        self, d25: float, d15: float, d5: float, expected: DistributionLevel
    ) -> None:
        assert distribution_level(d25, d15, d5) is expected

    @pytest.mark.parametrize(
        ("d25", "d15", "d5", "expected"),
        [
            (7.0, 0.0, 0.0, DistributionLevel.SEVERE),
            (0.0, 6.0, 0.0, DistributionLevel.SEVERE),
            # The rejected 6/4 severe pair must now classify one level lower.
            (6.0, 0.0, 0.0, DistributionLevel.HIGH),
            (0.0, 4.0, 0.0, DistributionLevel.HIGH),
            (5.0, 0.0, 0.0, DistributionLevel.HIGH),
            (0.0, 3.0, 0.0, DistributionLevel.HIGH),
            (0.0, 0.0, 2.0, DistributionLevel.HIGH),
            (3.0, 0.0, 0.0, DistributionLevel.CAUTION),
            (2.0, 0.0, 0.0, DistributionLevel.NORMAL),
        ],
    )
    def test_default_thresholds_match_the_shipped_config_boundaries(
        self, d25: float, d15: float, d5: float, expected: DistributionLevel
    ) -> None:
        """Default `DistributionThresholds` mirror `config/settings.yaml`.

        A caller that omits `thresholds=` must classify a day exactly as the
        pipeline does, so the dataclass defaults may not drift from the
        shipped `regime.dd_*` values.
        """
        assert distribution_level(d25, d15, d5) is expected
        assert (
            distribution_level(d25, d15, d5, thresholds=DistributionThresholds())
            is expected
        )

    def test_custom_thresholds_change_the_level_boundary(self) -> None:
        thresholds = DistributionThresholds(caution_d25=10, high_d25=12, severe_d25=14)

        # d25=6 stays NORMAL under raised thresholds, unlike the defaults.
        assert (
            distribution_level(6.0, 0.0, 0.0, thresholds=thresholds)
            is DistributionLevel.NORMAL
        )
        # Exactly at the raised caution boundary, it enters CAUTION (>=).
        assert (
            distribution_level(10.0, 0.0, 0.0, thresholds=thresholds)
            is DistributionLevel.CAUTION
        )
        # Exactly at the raised severe boundary, it enters SEVERE (>=).
        assert (
            distribution_level(14.0, 0.0, 0.0, thresholds=thresholds)
            is DistributionLevel.SEVERE
        )

    def test_calculate_distribution_days_applies_custom_level_thresholds(self) -> None:
        closes = [100.0] * 26
        volumes = [100] * 26
        # Two distribution days: index 5 and index 10.
        closes[5], volumes[5] = 99.8, 101
        closes[10], volumes[10] = 99.7, 102

        default_result = calculate_distribution_days(
            _bars(closes, volumes), date(2026, 1, 26)
        )
        assert default_result.d25 == pytest.approx(2.0)
        assert default_result.level is DistributionLevel.NORMAL

        lowered = DistributionThresholds(caution_d25=2)
        lowered_result = calculate_distribution_days(
            _bars(closes, volumes), date(2026, 1, 26), thresholds=lowered
        )
        assert lowered_result.d25 == pytest.approx(2.0)
        assert lowered_result.level is DistributionLevel.CAUTION
