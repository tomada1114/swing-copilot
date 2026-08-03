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


def _with_band_atr_multiple(settings, value):
    """Settings copy switching the pullback band to its ATR-normalized mode."""
    pullback = settings.technical_signals.pullback.model_copy(
        update={"band_atr_multiple": value}
    )
    technical = settings.technical_signals.model_copy(update={"pullback": pullback})
    return settings.model_copy(update={"technical_signals": technical})


class TestPullbackATRBand:
    """The ATR-normalized band (`band_atr_multiple`), and the legacy default."""

    # 60-day rally then a sharp 14-day slide: RSI 21.5, close 11.20 below
    # SMA50 with ATR14 2.64 -> 4.24 ATR units away, and 7.9% away in
    # percentage terms. The percentage band (3%) rejects it; a generous ATR
    # multiple admits it, so the two modes are distinguishable on one series.
    _CLOSES = [100.0 + i for i in range(60)] + [159.0 - i * 2.0 for i in range(1, 15)]
    # A shallower slide that lands 1.1% from SMA50 with RSI 31.7 -- inside
    # the legacy percentage band, so "unchanged default" is observable as a
    # hit rather than as two equally empty results.
    _LEGACY_HIT_CLOSES = [100.0 + i for i in range(60)] + [
        159.0 - i * 1.5 for i in range(1, 13)
    ]

    def _hits(self, settings, band_atr_multiple, closes=None):
        bars = make_bars("AAPL", closes or self._CLOSES, start=date(2026, 1, 1))
        data = _screening_input(bars)
        configured = _with_band_atr_multiple(settings, band_atr_multiple)
        return PullbackRSISignal(configured).evaluate(data, {"AAPL"})

    def test_none_keeps_the_legacy_percentage_band_hit(self, settings):
        bars = make_bars("AAPL", self._LEGACY_HIT_CLOSES, start=date(2026, 1, 1))
        data = _screening_input(bars)

        legacy = PullbackRSISignal(settings).evaluate(data, {"AAPL"})
        explicit_none = self._hits(settings, None, self._LEGACY_HIT_CLOSES)

        assert [hit.symbol for hit in legacy] == ["AAPL"]
        assert [hit.symbol for hit in explicit_none] == ["AAPL"]

    def test_none_keeps_the_legacy_percentage_band_rejection(self, settings):
        bars = make_bars("AAPL", self._CLOSES, start=date(2026, 1, 1))
        data = _screening_input(bars)

        assert PullbackRSISignal(settings).evaluate(data, {"AAPL"}) == []
        assert self._hits(settings, None) == []

    def test_a_generous_multiple_admits_a_close_the_percentage_band_rejects(
        self, settings
    ):
        assert [hit.symbol for hit in self._hits(settings, 20.0)] == ["AAPL"]

    def test_a_tight_multiple_rejects_the_same_close(self, settings):
        assert self._hits(settings, 0.01) == []

    def test_the_boundary_multiple_is_inclusive(self, settings):
        # Distance is 4.24 ATR units: 4.3 admits, 4.2 does not.
        assert [hit.symbol for hit in self._hits(settings, 4.3)] == ["AAPL"]
        assert self._hits(settings, 4.2) == []

    def test_the_atr_band_ignores_the_percentage_band_entirely(self, settings):
        # sma_band_pct set absurdly narrow: if the two modes were combined,
        # no close could ever qualify. A generous ATR multiple still hits.
        bars = make_bars("AAPL", self._CLOSES, start=date(2026, 1, 1))
        data = _screening_input(bars)
        pullback = settings.technical_signals.pullback.model_copy(
            update={"sma_band_pct": 0.0001, "band_atr_multiple": 20.0}
        )
        technical = settings.technical_signals.model_copy(update={"pullback": pullback})

        hits = PullbackRSISignal(
            settings.model_copy(update={"technical_signals": technical})
        ).evaluate(data, {"AAPL"})

        assert [hit.symbol for hit in hits] == ["AAPL"]

    def test_a_flat_series_with_zero_atr_is_rejected_fail_safe(self, settings):
        # A perfectly constant close makes ATR14 zero; the ATR-normalized
        # distance is then undefined, so the band must not open up.
        bars = make_bars("AAPL", [100.0] * 70, start=date(2026, 1, 1))
        for column in ("high", "low", "open"):
            bars[column] = 100.0
        data = _screening_input(bars)
        configured = _with_band_atr_multiple(settings, 2.0)

        assert PullbackRSISignal(configured).evaluate(data, {"AAPL"}) == []

    def test_a_nan_atr_is_rejected_fail_safe(self, settings):
        # A symbol with no high/low data at all leaves ATR14 undefined. The
        # band must close rather than admit a symbol whose distance cannot be
        # measured.
        bars = make_bars("AAPL", self._CLOSES, start=date(2026, 1, 1))
        bars["high"] = float("nan")
        bars["low"] = float("nan")
        data = _screening_input(bars)
        configured = _with_band_atr_multiple(settings, 20.0)

        assert PullbackRSISignal(configured).evaluate(data, {"AAPL"}) == []


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
