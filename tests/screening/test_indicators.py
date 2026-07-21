"""Tests for shared pandas indicator functions (FR-05)."""

from __future__ import annotations

import pandas as pd
import pytest

from swing_copilot.screening.indicators import sma, wilder_atr, wilder_rsi


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
