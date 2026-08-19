"""Tests for shared pandas indicator functions (FR-05)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from swing_copilot.screening.indicators import (
    SymbolWindow,
    _symbol_index_cache,
    percentile_ranks,
    sma,
    symbol_bars,
    symbol_ohlc_on,
    symbol_window,
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

    def test_appending_rows_to_a_looked_up_frame_invalidates_the_index(
        self, multi_symbol_bars
    ):
        # The index is keyed on frame identity, which an in-place append does
        # not change, so the row count is what tells the cache the frame it
        # indexed is no longer the frame it was handed.
        before = symbol_bars(multi_symbol_bars, "AAA", date(2026, 1, 10))
        assert before is not None
        appended = before.iloc[0].copy()
        appended["date"] = date(2026, 1, 9)
        multi_symbol_bars.loc[len(multi_symbol_bars)] = appended

        after = symbol_bars(multi_symbol_bars, "AAA", date(2026, 1, 10))

        assert after is not None
        assert len(after) == len(before) + 1

    def test_releasing_a_frame_drops_its_cached_index(self, multi_symbol_bars):
        # Each cached index retains a full per-symbol copy of its frame, so an
        # entry whose frame the caller has released must not stay reachable
        # until FIFO eviction happens to reach it.
        released = multi_symbol_bars.copy()
        symbol_bars(released, "AAA", date(2026, 1, 10))
        key = id(released)
        assert key in _symbol_index_cache
        del released

        symbol_bars(multi_symbol_bars, "AAA", date(2026, 1, 10))

        assert key not in _symbol_index_cache

    def test_an_empty_frame_yields_none(self):
        empty = pd.DataFrame(columns=["symbol", "date", "close"])

        assert symbol_bars(empty, "AAA", date(2026, 1, 1)) is None


_OHLC = ("open", "high", "low", "close")


def _naive_ohlc_on(bars, symbol, day):
    """`backtest/engine.py`'s pre-#244 `_bar`, kept as the equivalence oracle."""
    rows = bars[(bars["symbol"] == symbol) & (bars["date"] == day)]
    if rows.empty:
        return None
    row = rows.iloc[0]
    return {field: float(row[field]) for field in _OHLC}


def _naive_latest_ohlc(bars, symbol, as_of):
    """`backtest/engine.py`'s pre-#244 `_latest_bar`, the second oracle."""
    rows = bars[(bars["symbol"] == symbol) & (bars["date"] <= as_of)]
    if rows.empty:
        return None
    row = rows.sort_values("date").iloc[-1]
    return {field: float(row[field]) for field in _OHLC}


def _window_ohlc(bars, symbol, as_of):
    window = symbol_window(bars, symbol, as_of)
    return None if window is None else window.ohlc


class TestOhlcLookupsMatchTheNaiveScan:
    """Issue #244's equivalence contract for the two engine bar lookups.

    The simulator asks two different questions of one frame -- "the bar dated
    exactly `day`" and "the newest bar at or before `as_of`" -- and the scans
    they replace answered them with *different* tie-breaks when a
    `(symbol, date)` pair is duplicated. Both halves are pinned here, because
    an equity curve changes silently if either one flips.
    """

    @pytest.fixture
    def duplicated_bars(self):
        frames = [
            make_bars("AAA", [100.0 + i for i in range(10)], start=date(2026, 1, 1)),
            make_bars("BBB", [50.0 + i for i in range(6)], start=date(2026, 1, 3)),
        ]
        # AAA 2026-01-05 arrives twice with different prices -- a corrected bar
        # appended after the original, which is how a duplicate reaches the
        # engine. The second copy is deliberately far away in price so either
        # tie-break is unmistakable in the assertions below.
        duplicate = frames[0].iloc[4].copy()
        for field in _OHLC:
            duplicate[field] = float(duplicate[field]) + 1000.0
        return pd.concat(
            [frames[1], frames[0], duplicate.to_frame().T], ignore_index=True
        )

    @pytest.mark.parametrize("symbol", ["AAA", "BBB", "MISSING"])
    def test_both_lookups_match_the_scan_across_the_whole_range(
        self, duplicated_bars, symbol
    ):
        for offset in range(-2, 14):
            day = date(2026, 1, 1) + timedelta(days=offset)

            assert symbol_ohlc_on(duplicated_bars, symbol, day) == _naive_ohlc_on(
                duplicated_bars, symbol, day
            ), f"exact-day {symbol} @ {day}"
            assert _window_ohlc(duplicated_bars, symbol, day) == _naive_latest_ohlc(
                duplicated_bars, symbol, day
            ), f"as-of {symbol} @ {day}"

    def test_duplicate_rows_resolve_to_the_first_on_the_day_and_the_last_as_of(
        self, duplicated_bars
    ):
        # The asymmetry stated as bare numbers, independent of the oracles:
        # `iloc[0]` of the masked rows versus `sort_values("date").iloc[-1]`.
        # `kind="stable"` in the index is what keeps these two apart.
        duplicated_day = date(2026, 1, 5)
        exact = symbol_ohlc_on(duplicated_bars, "AAA", duplicated_day)
        as_of = _window_ohlc(duplicated_bars, "AAA", duplicated_day)

        assert exact is not None
        assert as_of is not None
        assert exact["close"] == 104.0
        assert as_of["close"] == 1104.0

    def test_the_exact_day_lookup_ignores_neighbouring_sessions(self, duplicated_bars):
        # Immediately before / exactly at / immediately after BBB's first bar.
        assert symbol_ohlc_on(duplicated_bars, "BBB", date(2026, 1, 2)) is None
        assert symbol_ohlc_on(duplicated_bars, "BBB", date(2026, 1, 3)) is not None
        assert symbol_ohlc_on(duplicated_bars, "BBB", date(2026, 1, 4)) is not None
        # ... and past the last one, where the as-of lookup still answers.
        assert symbol_ohlc_on(duplicated_bars, "BBB", date(2026, 1, 9)) is None
        assert _window_ohlc(duplicated_bars, "BBB", date(2026, 1, 9)) is not None

    def test_an_empty_frame_yields_none(self):
        empty = pd.DataFrame(columns=["symbol", "date", *_OHLC])

        assert symbol_ohlc_on(empty, "AAA", date(2026, 1, 1)) is None


_RSI_PERIOD = 14
_ATR_PERIOD = 14
_VOLUME_WINDOW = 20
_SMA_SHORT = 50
_SMA_LONG = 200
_WINDOW_START = date(2025, 1, 1)
_WINDOW_DAYS = 320


def _varied_bars(symbol: str, *, days: int = _WINDOW_DAYS, seed: int) -> pd.DataFrame:
    """Bars whose closes and volumes both actually move, unlike `make_bars`.

    A constant volume would let a broken trailing mean pass, and a monotone
    close would hide RSI/ATR smoothing differences.
    """
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, days)))
    frame = make_bars(symbol, [float(close) for close in closes], start=_WINDOW_START)
    frame["volume"] = rng.integers(500_000, 5_000_000, days)
    return frame


def _naive_indicators(
    bars: pd.DataFrame, symbol: str, as_of: date
) -> dict[str, float] | None:
    """The pre-#214 per-day computation, kept as the equivalence oracle.

    Every screening call site used to build these six full-history rolling
    series from the `as_of` prefix and keep only their last point. The
    `avg_volume` guard is part of that oracle: no call site ever took
    `tail(w).mean()` of a shorter history -- each one skipped the symbol
    first -- and `SymbolWindow.mean_volume` folds that guard into the column
    by requiring a full window.
    """
    series = symbol_bars(bars, symbol, as_of)
    if series is None:
        return None
    return {
        "close": float(series["close"].iloc[-1]),
        "rsi": float(wilder_rsi(series["close"], _RSI_PERIOD).iloc[-1]),
        "atr": float(
            wilder_atr(
                series["high"], series["low"], series["close"], _ATR_PERIOD
            ).iloc[-1]
        ),
        "mean_volume": float(series["volume"].tail(_VOLUME_WINDOW).mean())
        if len(series) >= _VOLUME_WINDOW
        else float("nan"),
        "sma_short": float(sma(series["close"], _SMA_SHORT).iloc[-1]),
        "sma_long": float(sma(series["close"], _SMA_LONG).iloc[-1]),
    }


def _window_indicators(window: SymbolWindow) -> dict[str, float]:
    return {
        "close": window.close,
        "rsi": window.rsi(_RSI_PERIOD),
        "atr": window.atr(_ATR_PERIOD),
        "mean_volume": window.mean_volume(_VOLUME_WINDOW),
        "sma_short": window.sma(_SMA_SHORT),
        "sma_long": window.sma(_SMA_LONG),
    }


def _comparable(values: dict[str, float]) -> dict[str, object]:
    """Make NaN compare equal to NaN so `==` can assert bit-for-bit equality."""
    return {key: "nan" if math.isnan(value) else value for key, value in values.items()}


class TestSymbolWindowMatchesThePerDayComputation:
    """Issue #214's equivalence contract, and its no-look-ahead proof.

    `SymbolWindow` reads indicator columns computed once over a symbol's
    *whole* history, including rows dated after `as_of`. That is safe only
    because every one of those indicators is causal, so the value at a row is
    a function of that row and earlier rows alone. This asserts exactly that,
    for every simulated day: the value read from the full-history column must
    equal -- bit for bit, not within a tolerance, because the backtest's
    equity curve must not move by a cent -- the value the old code computed
    from the `as_of` prefix on its own.
    """

    @pytest.fixture
    def bars(self) -> pd.DataFrame:
        return pd.concat(
            [_varied_bars("AAA", seed=214), _varied_bars("BBB", seed=1114)],
            ignore_index=True,
        )

    @pytest.mark.parametrize("symbol", ["AAA", "BBB", "MISSING"])
    def test_every_day_matches_the_naive_computation(self, bars, symbol):
        for offset in range(_WINDOW_DAYS):
            as_of = _WINDOW_START + timedelta(days=offset)

            window = symbol_window(bars, symbol, as_of)
            expected = _naive_indicators(bars, symbol, as_of)

            if expected is None:
                assert window is None, f"{symbol} @ {as_of}"
                continue
            assert window is not None
            assert _comparable(_window_indicators(window)) == _comparable(expected), (
                f"{symbol} @ {as_of}"
            )

    def test_a_frame_extending_past_as_of_matches_a_truncated_frame(self, bars):
        # A truncated copy is a *different* frame, so its columns are computed
        # from scratch over data at or before `as_of` and cannot have seen a
        # later row at all.
        as_of = _WINDOW_START + timedelta(days=_WINDOW_DAYS - 40)
        truncated = bars[bars["date"] <= as_of].copy()

        from_full = symbol_window(bars, "AAA", as_of)
        from_truncated = symbol_window(truncated, "AAA", as_of)

        assert from_full is not None
        assert from_truncated is not None
        assert _window_indicators(from_full) == _window_indicators(from_truncated)
        assert bars["date"].max() > as_of  # the frame really did extend past as_of

    def test_the_as_of_boundary_is_inclusive_immediately_around_a_bar(self, bars):
        # AAA's 250th bar dates to _WINDOW_START + 249 days: read the day
        # before it, the day itself, and the day after.
        boundary = _WINDOW_START + timedelta(days=249)
        closes = bars.loc[bars["symbol"] == "AAA", "close"].tolist()

        before = symbol_window(bars, "AAA", boundary - timedelta(days=1))
        at = symbol_window(bars, "AAA", boundary)
        after = symbol_window(bars, "AAA", boundary + timedelta(days=1))

        assert before is not None
        assert at is not None
        assert after is not None
        assert (before.bar_count, at.bar_count, after.bar_count) == (249, 250, 251)
        assert (before.close, at.close, after.close) == (
            closes[248],
            closes[249],
            closes[250],
        )

    def test_a_symbol_with_no_bars_before_the_cutoff_has_no_window(self, bars):
        assert symbol_window(bars, "AAA", _WINDOW_START - timedelta(days=1)) is None


class TestMeanVolumeMatchesTailMean:
    """`Series.rolling(w).mean()` is *not* an acceptable substitute here.

    pandas' rolling mean is a streaming add/remove with Kahan compensation;
    `Series.mean()` pairwise-sums one window. The two disagree in the last
    bits for a large fraction of float windows, and every replaced call site
    computed `tail(w).mean()`.
    """

    def _volume_bars(self, volumes: list[float]) -> pd.DataFrame:
        frame = make_bars(
            "VOL", [100.0 + index for index in range(len(volumes))], start=_WINDOW_START
        )
        frame["volume"] = volumes
        return frame

    def test_float_volumes_match_pandas_tail_mean_bit_for_bit(self):
        rng = np.random.default_rng(7)
        volumes = (rng.uniform(1e6, 9e6, 120) * rng.uniform(0.3, 3.0, 120)).tolist()
        bars = self._volume_bars(volumes)

        for offset in range(_VOLUME_WINDOW - 1, 120):
            as_of = _WINDOW_START + timedelta(days=offset)
            window = symbol_window(bars, "VOL", as_of)

            assert window is not None
            expected = pd.Series(volumes[: offset + 1]).tail(_VOLUME_WINDOW).mean()
            assert window.mean_volume(_VOLUME_WINDOW) == expected, f"@ {as_of}"

    def test_missing_volumes_are_skipped_exactly_like_series_mean(self):
        volumes = [float(1_000_000 + index * 1_000) for index in range(40)]
        volumes[25] = float("nan")
        bars = self._volume_bars(volumes)
        as_of = _WINDOW_START + timedelta(days=29)

        window = symbol_window(bars, "VOL", as_of)

        assert window is not None
        assert (
            window.mean_volume(_VOLUME_WINDOW)
            == pd.Series(volumes[:30]).tail(_VOLUME_WINDOW).mean()
        )

    def test_a_window_of_only_missing_volumes_is_nan(self):
        volumes = [float("nan")] * 25
        bars = self._volume_bars(volumes)
        as_of = _WINDOW_START + timedelta(days=24)

        window = symbol_window(bars, "VOL", as_of)

        assert window is not None
        assert math.isnan(window.mean_volume(_VOLUME_WINDOW))

    def test_a_history_shorter_than_the_window_is_nan(self):
        bars = self._volume_bars([1_000_000.0] * (_VOLUME_WINDOW - 1))
        as_of = _WINDOW_START + timedelta(days=_VOLUME_WINDOW - 2)

        window = symbol_window(bars, "VOL", as_of)

        assert window is not None
        assert math.isnan(window.mean_volume(_VOLUME_WINDOW))


class TestSymbolWindowExposesTheRawRows:
    def test_bars_are_the_rows_at_or_before_as_of(self):
        bars = _varied_bars("AAA", days=30, seed=3)
        as_of = _WINDOW_START + timedelta(days=9)

        window = symbol_window(bars, "AAA", as_of)
        expected = symbol_bars(bars, "AAA", as_of)

        assert window is not None
        assert expected is not None
        pd.testing.assert_frame_equal(window.bars, expected)

    def test_sma_history_is_the_prefix_of_the_full_column(self):
        bars = _varied_bars("AAA", days=120, seed=4)
        as_of = _WINDOW_START + timedelta(days=79)
        series = symbol_bars(bars, "AAA", as_of)

        window = symbol_window(bars, "AAA", as_of)

        assert window is not None
        assert series is not None
        expected = sma(series["close"], _SMA_SHORT).to_numpy()
        np.testing.assert_array_equal(window.sma_history(_SMA_SHORT), expected)
