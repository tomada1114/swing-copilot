"""Tests for ScreeningPipeline: AND semantics, ranking, extensibility (FR-04, FR-05, NFR-07)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - imported for its @register_filter side effect
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - imported for its @register_signal side effect
)
from swing_copilot.screening.base import (
    FILTER_REGISTRY,
    SIGNAL_REGISTRY,
    ScreeningInput,
    SignalHit,
    register_signal,
)
from swing_copilot.screening.pipeline import ScreeningPipeline
from swing_copilot.universe import UniverseMember
from tests.screening.conftest import make_bars

AS_OF = date(2027, 1, 1)


def _uptrend_closes(days: int, base: float = 100.0) -> list[float]:
    return [base + 0.5 * i for i in range(days)]


def _member(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        company_name=symbol,
        gics_sector="Technology",
        source_symbol=symbol,
    )


STRATEGIES_CONFIG = {
    "strategies": {
        "default": {
            "filters_all": ["volume_min"],
            "signals_all": ["trend_sma"],
            "candidate_limit": 10,
            "ranking": ["rsi14_asc", "avg_volume_desc", "symbol_asc"],
        }
    }
}


def _uptrend_bars(
    symbol: str, *, days: int = 210, base: float = 100.0, volume: int = 2_000_000
) -> pd.DataFrame:
    return make_bars(
        symbol, _uptrend_closes(days, base=base), start=date(2026, 1, 1), volume=volume
    )


class TestAndSemantics:
    def test_symbol_must_pass_all_filters_and_all_signals(self, settings):
        bars = pd.concat(
            [
                _uptrend_bars("PASSES", volume=2_000_000),
                _uptrend_bars("LOW_VOLUME", volume=100),
            ]
        )
        universe = (_member("PASSES"), _member("LOW_VOLUME"))
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        pipeline = ScreeningPipeline(
            STRATEGIES_CONFIG, market_store=None, settings=settings
        )
        candidates = pipeline.run(data)

        assert [c.symbol for c in candidates] == ["PASSES"]

    def test_no_candidates_when_nothing_hits_all_signals(self, settings):
        downtrend = list(reversed(_uptrend_closes(210)))
        bars = make_bars("DOWN", downtrend, start=date(2026, 1, 1), volume=2_000_000)
        universe = (_member("DOWN"),)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        pipeline = ScreeningPipeline(
            STRATEGIES_CONFIG, market_store=None, settings=settings
        )
        candidates = pipeline.run(data)

        assert candidates == []

    def test_no_candidates_when_no_signals_are_configured(self, settings):
        bars = _uptrend_bars("AAPL")
        universe = (_member("AAPL"),)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )
        strategies_config = {
            "strategies": {
                "default": {
                    "filters_all": [],
                    "signals_all": [],
                    "candidate_limit": 10,
                    "ranking": [],
                }
            }
        }

        pipeline = ScreeningPipeline(
            strategies_config, market_store=None, settings=settings
        )
        candidates = pipeline.run(data)

        assert candidates == []


class TestCandidateAggregationAndRanking:
    def test_multiple_signal_hits_aggregate_into_one_candidate(self, settings):
        strategies_config = {
            "strategies": {
                "default": {
                    "filters_all": [],
                    "signals_all": ["trend_sma", "pullback_rsi"],
                    "candidate_limit": 10,
                    "ranking": ["rsi14_asc", "avg_volume_desc", "symbol_asc"],
                }
            }
        }
        rally = [100.0 + i for i in range(60)]
        pullback = [rally[-1] - i * 2 for i in range(1, 11)]
        closes = rally + pullback
        # Pad the front so both SMA200 (trend) and SMA50/RSI (pullback) have
        # enough history and the pullback still sits above the long SMA.
        long_history = [95.0 + 0.05 * i for i in range(150)] + closes
        bars = make_bars("BOTH", long_history, start=date(2025, 1, 1), volume=2_000_000)
        universe = (_member("BOTH"),)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        pipeline = ScreeningPipeline(
            strategies_config, market_store=None, settings=settings
        )
        candidates = pipeline.run(data)

        assert len(candidates) == 1
        assert candidates[0].signal_names == ("pullback_rsi", "trend_sma")

    def test_ranking_is_rsi_asc_then_volume_desc_then_symbol_asc(self, settings):
        # Three symbols all pass the trend signal; rank purely on the
        # pipeline's own rsi14/avg_volume computation.
        bars = pd.concat(
            [
                _uptrend_bars("HIGH_RSI_LOW_VOL", base=100.0, volume=1_500_000),
                _uptrend_bars("LOW_RSI", base=50.0, volume=1_500_000),
                _uptrend_bars("HIGH_RSI_HIGH_VOL", base=100.0, volume=3_000_000),
            ]
        )
        universe = (
            _member("HIGH_RSI_LOW_VOL"),
            _member("LOW_RSI"),
            _member("HIGH_RSI_HIGH_VOL"),
        )
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        strategies_config = {
            "strategies": {
                "default": {
                    "filters_all": [],
                    "signals_all": ["trend_sma"],
                    "candidate_limit": 10,
                    "ranking": ["rsi14_asc", "avg_volume_desc", "symbol_asc"],
                }
            }
        }
        pipeline = ScreeningPipeline(
            strategies_config, market_store=None, settings=settings
        )
        candidates = pipeline.run(data)

        rsis = [c.metrics["rsi14"] for c in candidates]
        assert rsis == sorted(rsis)
        assert [c.rank for c in candidates] == list(range(1, len(candidates) + 1))

    def test_candidate_limit_caps_results(self, settings):
        symbols = [f"SYM{i}" for i in range(15)]
        bars = pd.concat(
            [_uptrend_bars(symbol, base=100.0 + i) for i, symbol in enumerate(symbols)]
        )
        universe = tuple(_member(symbol) for symbol in symbols)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        strategies_config = {
            "strategies": {
                "default": {
                    "filters_all": [],
                    "signals_all": ["trend_sma"],
                    "candidate_limit": 10,
                    "ranking": ["rsi14_asc", "avg_volume_desc", "symbol_asc"],
                }
            }
        }
        pipeline = ScreeningPipeline(
            strategies_config, market_store=None, settings=settings
        )
        candidates = pipeline.run(data)

        assert len(candidates) <= 10

    def test_symbol_with_insufficient_history_for_ranking_metrics_is_dropped(
        self, settings
    ):
        # A signal that hits regardless of history, paired with a symbol
        # that has too few bars for the pipeline's own rsi14/atr14/avg_volume
        # computation (needs >= 20 days) — it must not become a candidate.
        @register_signal("always_hit_short_history_test_signal")
        class _AlwaysHitSignal:
            name = "always_hit_short_history_test_signal"

            def __init__(self, settings: object) -> None:
                pass

            def evaluate(
                self, _data: ScreeningInput, symbols: set[str]
            ) -> list[SignalHit]:
                return [
                    SignalHit(
                        symbol=symbol,
                        signal_name=self.name,
                        direction="long",
                        strength=1.0,
                        metrics={},
                    )
                    for symbol in sorted(symbols)
                ]

        try:
            bars = make_bars("SHORT", _uptrend_closes(5), start=date(2026, 1, 1))
            universe = (_member("SHORT"),)
            data = ScreeningInput(
                as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
            )
            strategies_config = {
                "strategies": {
                    "default": {
                        "filters_all": [],
                        "signals_all": ["always_hit_short_history_test_signal"],
                        "candidate_limit": 10,
                        "ranking": ["rsi14_asc", "avg_volume_desc", "symbol_asc"],
                    }
                }
            }
            pipeline = ScreeningPipeline(
                strategies_config, market_store=None, settings=settings
            )
            candidates = pipeline.run(data)

            assert candidates == []
        finally:
            del SIGNAL_REGISTRY["always_hit_short_history_test_signal"]

    def test_symbol_dropped_when_a_ranking_metric_is_nan_despite_enough_history(
        self, settings, monkeypatch
    ):
        # Defensive branch: even with >= 20 days of history (normally
        # guaranteeing non-NaN rsi14/atr14/avg_volume), a data gap could
        # still produce a NaN metric — such a symbol must not become a
        # candidate rather than sorting with a NaN key.
        monkeypatch.setattr(
            "swing_copilot.screening.pipeline.wilder_rsi",
            lambda series, _period: pd.Series(
                [float("nan")] * len(series), index=series.index
            ),
        )
        bars = _uptrend_bars("AAPL")
        universe = (_member("AAPL"),)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        pipeline = ScreeningPipeline(
            STRATEGIES_CONFIG, market_store=None, settings=settings
        )
        candidates = pipeline.run(data)

        assert candidates == []

    def test_same_input_produces_identical_candidates_across_runs(self, settings):
        bars = pd.concat(
            [
                _uptrend_bars("AAPL", base=100.0),
                _uptrend_bars("MSFT", base=90.0),
            ]
        )
        universe = (_member("AAPL"), _member("MSFT"))
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        pipeline = ScreeningPipeline(
            STRATEGIES_CONFIG, market_store=None, settings=settings
        )
        first_run = pipeline.run(data)
        second_run = pipeline.run(data)

        assert first_run == second_run

    def test_as_of_excludes_bars_from_the_future(self, settings):
        bars = _uptrend_bars("AAPL", days=210)
        future_row = pd.DataFrame(
            [
                {
                    "symbol": "AAPL",
                    "date": date(2027, 6, 1),
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1,
                }
            ]
        )
        bars = pd.concat([bars, future_row])
        universe = (_member("AAPL"),)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        pipeline = ScreeningPipeline(
            STRATEGIES_CONFIG, market_store=None, settings=settings
        )
        candidates = pipeline.run(data)

        assert candidates
        assert candidates[0].metrics["rsi14"] != pytest.approx(0.0)


class TestExtensibility:
    def test_new_signal_can_be_registered_without_editing_pipeline(self, settings):
        @register_signal("always_hit_test_signal")
        class _AlwaysHitSignal:
            name = "always_hit_test_signal"

            def __init__(self, settings: object) -> None:
                pass

            def evaluate(
                self, _data: ScreeningInput, symbols: set[str]
            ) -> list[SignalHit]:
                return [
                    SignalHit(
                        symbol=symbol,
                        signal_name=self.name,
                        direction="long",
                        strength=1.0,
                        metrics={},
                    )
                    for symbol in sorted(symbols)
                ]

        try:
            strategies_config = {
                "strategies": {
                    "default": {
                        "filters_all": [],
                        "signals_all": ["always_hit_test_signal"],
                        "candidate_limit": 10,
                        "ranking": ["rsi14_asc", "avg_volume_desc", "symbol_asc"],
                    }
                }
            }
            bars = _uptrend_bars("AAPL")
            universe = (_member("AAPL"),)
            data = ScreeningInput(
                as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
            )

            pipeline = ScreeningPipeline(
                strategies_config, market_store=None, settings=settings
            )
            candidates = pipeline.run(data)

            assert [c.symbol for c in candidates] == ["AAPL"]
            assert candidates[0].signal_names == ("always_hit_test_signal",)
        finally:
            del SIGNAL_REGISTRY["always_hit_test_signal"]

    def test_unregistered_strategy_key_raises_key_error(self, settings):
        with pytest.raises(KeyError):
            ScreeningPipeline(
                STRATEGIES_CONFIG,
                market_store=None,
                settings=settings,
                strategy_key="missing",
            )

    def test_unregistered_filter_key_raises_key_error(self, settings):
        bad_config = {
            "strategies": {
                "default": {
                    "filters_all": ["does_not_exist"],
                    "signals_all": [],
                    "candidate_limit": 10,
                    "ranking": [],
                }
            }
        }
        with pytest.raises(KeyError):
            ScreeningPipeline(bad_config, market_store=None, settings=settings)


def test_registry_contains_default_strategy_building_blocks():
    assert "profitable_positive_fcf_equity" in FILTER_REGISTRY
    assert "volume_min" in FILTER_REGISTRY
    assert "trend_sma" in SIGNAL_REGISTRY
    assert "pullback_rsi" in SIGNAL_REGISTRY
