"""Tests for trend/pullback signals and the liquidity filter (FR-05)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from swing_copilot.screening.base import ScreeningInput
from swing_copilot.screening.technical_signals import (
    MinAverageVolumeFilter,
    PullbackRSISignal,
    TrendSMASignal,
)
from swing_copilot.universe import UniverseMember
from tests.screening.conftest import make_bars

AS_OF = date(2027, 1, 1)


def _uptrend_closes(days: int) -> list[float]:
    return [100.0 + 0.5 * i for i in range(days)]


def _screening_input(bars: pd.DataFrame) -> ScreeningInput:
    return ScreeningInput(
        as_of=AS_OF, universe=(), fundamentals=pd.DataFrame(), bars=bars
    )


class TestTrendSMASignal:
    def test_hits_when_price_above_long_sma_and_short_above_long(self, settings):
        bars = make_bars("AAPL", _uptrend_closes(210), start=date(2026, 1, 1))
        data = _screening_input(bars)

        hits = TrendSMASignal(settings).evaluate(data, {"AAPL"})

        assert [hit.symbol for hit in hits] == ["AAPL"]
        assert hits[0].metrics["sma_short"] > 0
        assert hits[0].metrics["sma_long"] > 0

    def test_no_hit_in_a_downtrend(self, settings):
        closes = list(reversed(_uptrend_closes(210)))
        bars = make_bars("AAPL", closes, start=date(2026, 1, 1))
        data = _screening_input(bars)

        hits = TrendSMASignal(settings).evaluate(data, {"AAPL"})

        assert hits == []

    def test_no_hit_with_insufficient_history(self, settings):
        bars = make_bars("AAPL", _uptrend_closes(50), start=date(2026, 1, 1))
        data = _screening_input(bars)

        hits = TrendSMASignal(settings).evaluate(data, {"AAPL"})

        assert hits == []

    def test_no_hit_when_symbol_has_no_bars_at_all(self, settings):
        bars = make_bars("AAPL", _uptrend_closes(210), start=date(2026, 1, 1))
        data = _screening_input(bars)

        hits = TrendSMASignal(settings).evaluate(data, {"NO_BARS"})

        assert hits == []

    def test_only_evaluates_requested_symbols(self, settings):
        bars = pd.concat(
            [
                make_bars("AAPL", _uptrend_closes(210), start=date(2026, 1, 1)),
                make_bars("MSFT", _uptrend_closes(210), start=date(2026, 1, 1)),
            ]
        )
        data = _screening_input(bars)

        hits = TrendSMASignal(settings).evaluate(data, {"AAPL"})

        assert [hit.symbol for hit in hits] == ["AAPL"]


class TestPullbackRSISignal:
    def test_hits_when_rsi_low_and_close_near_sma50(self, settings):
        # Rally to build up SMA50 history, then a controlled multi-day
        # pullback that lands the close right at the SMA50 with low RSI.
        rally = [100.0 + i for i in range(60)]
        pullback = [rally[-1] - i * 2 for i in range(1, 11)]
        closes = rally + pullback
        bars = make_bars("AAPL", closes, start=date(2026, 1, 1))
        data = _screening_input(bars)

        hits = PullbackRSISignal(settings).evaluate(data, {"AAPL"})

        assert [hit.symbol for hit in hits] == ["AAPL"]
        assert (
            hits[0].metrics["rsi14"] < settings.technical_signals.pullback.rsi_threshold
        )

    def test_no_hit_when_far_outside_sma_band(self, settings):
        rally = [100.0 + i for i in range(60)]
        crash = [rally[-1] - i * 10 for i in range(1, 21)]
        closes = rally + crash
        bars = make_bars("AAPL", closes, start=date(2026, 1, 1))
        data = _screening_input(bars)

        hits = PullbackRSISignal(settings).evaluate(data, {"AAPL"})

        assert hits == []

    def test_no_hit_with_insufficient_history(self, settings):
        bars = make_bars("AAPL", _uptrend_closes(30), start=date(2026, 1, 1))
        data = _screening_input(bars)

        hits = PullbackRSISignal(settings).evaluate(data, {"AAPL"})

        assert hits == []

    def test_no_hit_when_symbol_has_no_bars_at_all(self, settings):
        bars = make_bars("AAPL", _uptrend_closes(60), start=date(2026, 1, 1))
        data = _screening_input(bars)

        hits = PullbackRSISignal(settings).evaluate(data, {"NO_BARS"})

        assert hits == []


class TestMinAverageVolumeFilter:
    def test_passes_above_floor(self, settings):
        bars = make_bars(
            "AAPL", _uptrend_closes(30), start=date(2026, 1, 1), volume=2_000_000
        )
        universe = (_member("AAPL"),)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        result = MinAverageVolumeFilter(settings).apply(data)

        assert result == {"AAPL"}

    def test_fails_below_floor(self, settings):
        bars = make_bars(
            "AAPL", _uptrend_closes(30), start=date(2026, 1, 1), volume=100
        )
        universe = (_member("AAPL"),)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        result = MinAverageVolumeFilter(settings).apply(data)

        assert result == set()

    def test_fails_with_insufficient_history(self, settings):
        bars = make_bars("AAPL", _uptrend_closes(5), start=date(2026, 1, 1))
        universe = (_member("AAPL"),)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        result = MinAverageVolumeFilter(settings).apply(data)

        assert result == set()


def _member(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        company_name=symbol,
        gics_sector="Information Technology",
        source_symbol=symbol,
    )


def test_boundary_rsi_exactly_at_threshold_does_not_hit(settings, monkeypatch):
    # The pullback condition is strictly "<"; pin RSI to exactly the
    # threshold (with the close pinned exactly on SMA50) and confirm that
    # does not count as a hit.
    threshold = float(settings.technical_signals.pullback.rsi_threshold)
    bars = make_bars("FLAT", _uptrend_closes(60), start=date(2026, 1, 1))
    data = _screening_input(bars)

    monkeypatch.setattr(
        "swing_copilot.screening.technical_signals.wilder_rsi",
        lambda series, _period: pd.Series(
            [threshold] * len(series), index=series.index
        ),
    )
    monkeypatch.setattr(
        "swing_copilot.screening.technical_signals.sma",
        lambda series, _window: pd.Series(
            [float(series.iloc[-1])] * len(series), index=series.index
        ),
    )

    hits = PullbackRSISignal(settings).evaluate(data, {"FLAT"})

    assert hits == []
