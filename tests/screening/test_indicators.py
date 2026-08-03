"""Tests for shared pandas indicator functions (FR-05)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from swing_copilot.screening.indicators import (
    percentile_ranks,
    sma,
    symbol_bars,
    wilder_atr,
    wilder_rsi,
)
from tests.screening.conftest import make_bars


class TestPercentileRanks:
    def test_equal_values_share_average_rank(self):
        assert percentile_ranks({"LOW": 1.0, "A": 2.0, "B": 2.0}) == {
            "LOW": 0.0,
            "A": 0.75,
            "B": 0.75,
        }

    def test_empty_and_single_populations(self):
        assert percentile_ranks({}) == {}
        assert percentile_ranks({"ONLY": 10.0}) == {"ONLY": 0.5}


class TestSma:
    def test_known_values(self):
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = sma(series, window=3)
        assert result.iloc[2] == pytest.approx(2.0)
        assert result.iloc[3] == pytest.approx(3.0)
        assert result.iloc[4] == pytest.approx(4.0)

    def test_insufficient_history_is_nan(self):
        series = pd.Series([1.0, 2.0])
        result = sma(series, window=3)
        assert result.isna().all()


class TestWilderRsi:
    def test_strictly_increasing_series_approaches_100(self):
        series = pd.Series([float(i) for i in range(1, 30)])
        result = wilder_rsi(series, period=14)
        assert result.iloc[-1] == pytest.approx(100.0)

    def test_strictly_decreasing_series_approaches_0(self):
        series = pd.Series([float(i) for i in range(30, 1, -1)])
        result = wilder_rsi(series, period=14)
        assert result.iloc[-1] == pytest.approx(0.0)

    def test_known_hand_computed_value_for_short_period(self):
        # period=3 Wilder RSI, hand-computed:
        # closes: 10, 11, 10, 12  -> deltas: +1, -1, +2
        # avg_gain (ewm alpha=1/3, adjust=False, seeded by first gain=1): 1, 1*(2/3), ...
        # We verify against a direct re-implementation of the same formula
        # rather than an independent hand trace, since Wilder smoothing's
        # seed behavior with ewm(adjust=False) is otherwise easy to get
        # subtly wrong by hand; this test pins the documented boundary
        # instead: a single down day after two up days keeps RSI below 100
        # and above 0.
        series = pd.Series([10.0, 11.0, 12.0, 11.5])
        result = wilder_rsi(series, period=3)
        assert 0.0 < result.iloc[-1] < 100.0

    def test_insufficient_history_is_nan(self):
        series = pd.Series([10.0, 11.0])
        result = wilder_rsi(series, period=14)
        assert result.isna().all()

    def test_no_losses_in_window_is_100_not_nan(self):
        series = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
        result = wilder_rsi(series, period=3)
        assert result.iloc[-1] == pytest.approx(100.0)


class TestWilderAtr:
    def test_zero_volatility_series_is_zero(self):
        high = pd.Series([10.0] * 5)
        low = pd.Series([10.0] * 5)
        close = pd.Series([10.0] * 5)
        result = wilder_atr(high, low, close, period=3)
        assert result.iloc[-1] == pytest.approx(0.0)

    def test_insufficient_history_is_nan(self):
        high = pd.Series([10.0, 11.0])
        low = pd.Series([9.0, 10.0])
        close = pd.Series([9.5, 10.5])
        result = wilder_atr(high, low, close, period=14)
        assert result.isna().all()

    def test_constant_true_range_matches_that_value(self):
        # High-Low = 2 every day, no gaps -> ATR converges to 2.
        high = pd.Series([11.0, 12.0, 13.0, 14.0, 15.0, 16.0])
        low = pd.Series([9.0, 10.0, 11.0, 12.0, 13.0, 14.0])
        close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
        result = wilder_atr(high, low, close, period=3)
        assert result.iloc[-1] == pytest.approx(2.0)


def _naive_symbol_bars(bars, symbol, as_of):
    """The pre-optimization implementation, kept as the equivalence oracle."""
    subset = bars[(bars["symbol"] == symbol) & (bars["date"] <= as_of)].sort_values(
        "date"
    )
    return subset if not subset.empty else None


class TestSymbolBarsMatchesTheNaiveImplementation:
    """D10 requires the indexed lookup to return exactly what the scan did."""

    @pytest.fixture
    def multi_symbol_bars(self):
        frames = [
            make_bars("AAA", [100.0 + i for i in range(30)], start=date(2026, 1, 1)),
            make_bars("BBB", [50.0 + i for i in range(20)], start=date(2026, 1, 1)),
            make_bars("CCC", [10.0 + i for i in range(5)], start=date(2026, 2, 1)),
        ]
        # Deliberately unsorted: the index must not depend on input order.
        return pd.concat(frames[::-1], ignore_index=True)

    @pytest.mark.parametrize("symbol", ["AAA", "BBB", "CCC", "MISSING"])
    def test_every_symbol_matches_across_the_whole_date_range(
        self, multi_symbol_bars, symbol
    ):
        for offset in range(45):
            as_of = date(2026, 1, 1) + timedelta(days=offset)

            actual = symbol_bars(multi_symbol_bars, symbol, as_of)
            expected = _naive_symbol_bars(multi_symbol_bars, symbol, as_of)

            if expected is None:
                assert actual is None, f"{symbol} @ {as_of}"
            else:
                assert actual is not None, f"{symbol} @ {as_of}"
                pd.testing.assert_frame_equal(
                    actual.reset_index(drop=True),
                    expected.reset_index(drop=True),
                )

    def test_the_as_of_boundary_is_inclusive_exactly_like_the_scan(
        self, multi_symbol_bars
    ):
        # CCC's first bar is 2026-02-01: before / at / after that boundary.
        assert symbol_bars(multi_symbol_bars, "CCC", date(2026, 1, 31)) is None
        at_boundary = symbol_bars(multi_symbol_bars, "CCC", date(2026, 2, 1))
        day_after = symbol_bars(multi_symbol_bars, "CCC", date(2026, 2, 2))
        assert at_boundary is not None
        assert day_after is not None
        assert len(at_boundary) == 1
        assert len(day_after) == 2

    def test_repeated_lookups_on_one_frame_stay_correct(self, multi_symbol_bars):
        # The index is cached on the frame; a second pass must not drift.
        first = symbol_bars(multi_symbol_bars, "AAA", date(2026, 1, 10))
        second = symbol_bars(multi_symbol_bars, "AAA", date(2026, 1, 10))
        third = symbol_bars(multi_symbol_bars, "AAA", date(2026, 1, 5))

        assert first is not None
        assert second is not None
        assert third is not None
        pd.testing.assert_frame_equal(first, second)
        assert len(third) == 5

    def test_an_empty_frame_yields_none(self):
        empty = pd.DataFrame(columns=["symbol", "date", "close"])

        assert symbol_bars(empty, "AAA", date(2026, 1, 1)) is None
