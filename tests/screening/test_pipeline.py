"""Tests for ScreeningPipeline: AND semantics, ranking, extensibility (FR-04, FR-05, NFR-07)."""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from typing import TYPE_CHECKING

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
    Candidate,
    ScreeningInput,
    SignalHit,
    register_signal,
)
from swing_copilot.screening.pipeline import ScreeningPipeline
from swing_copilot.universe import UniverseMember
from tests.screening.conftest import make_bars

if TYPE_CHECKING:
    from collections.abc import Mapping

    from swing_copilot.config import Settings

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

    def test_ranking_is_by_descending_score_with_ranks_assigned_in_order(
        self, settings
    ):
        # Three symbols all pass the trend signal; rank on the pipeline's
        # own composite score (REQ-002, REQ-010).
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
                }
            }
        }
        pipeline = ScreeningPipeline(
            strategies_config, market_store=None, settings=settings
        )
        candidates = pipeline.run(data)

        scores = [c.metrics["score"] for c in candidates]
        assert scores == sorted(scores, reverse=True)
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


def _score_pipeline(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    metrics_by_symbol: dict[str, dict[str, float]],
    *,
    ranking: Mapping[str, object] | None = None,
    candidate_limit: int = 10,
) -> list[Candidate]:
    """Run the pipeline with `_ranking_metrics` fully controlled per symbol.

    Isolates score-computation/ranking (`run()`) from indicator plumbing
    (`_ranking_metrics`), which has its own coverage elsewhere.
    """
    strategy: dict[str, object] = {
        "filters_all": [],
        "signals_all": ["score_test_signal"],
        "candidate_limit": candidate_limit,
    }
    if ranking is not None:
        strategy["ranking"] = ranking
    strategies_config = {"strategies": {"default": strategy}}
    pipeline = ScreeningPipeline(
        strategies_config, market_store=None, settings=settings
    )
    monkeypatch.setattr(
        pipeline,
        "_ranking_metrics",
        lambda _data, symbol: metrics_by_symbol.get(symbol),
    )
    universe = tuple(_member(symbol) for symbol in metrics_by_symbol)
    data = ScreeningInput(
        as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=pd.DataFrame()
    )
    return pipeline.run(data)


@pytest.mark.usefixtures("score_test_signal")
class TestCompositeScoring:
    """REQ-002..REQ-005, REQ-010, REQ-030, and the two boundary conditions."""

    @pytest.fixture
    def score_test_signal(self):
        @register_signal("score_test_signal")
        class _AlwaysHitSignal:
            name = "score_test_signal"

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

        yield "score_test_signal"
        del SIGNAL_REGISTRY["score_test_signal"]

    def test_score_matches_hand_calculation_example_1(self, settings, monkeypatch):
        # Issue Example 1: rsi_threshold=45, default weights, liquidity=0.8
        # achieved via a 6-candidate set where AAPL is 5th of 6 by avg_volume.
        metrics_by_symbol = {
            "B1": _metrics(rsi14=50.0, avg_volume=10.0),
            "B2": _metrics(rsi14=50.0, avg_volume=20.0),
            "B3": _metrics(rsi14=50.0, avg_volume=30.0),
            "B4": _metrics(rsi14=50.0, avg_volume=40.0),
            "AAPL": _metrics(rsi14=30.0, avg_volume=50.0, sma50=110.0),
            "B5": _metrics(rsi14=50.0, avg_volume=60.0),
        }
        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)

        aapl = next(c for c in candidates if c.symbol == "AAPL")
        expected_rsi_pullback = (45.0 - 30.0) / 45.0
        expected_score = 0.5 * expected_rsi_pullback + 0.3 * 1.0 + 0.2 * 0.8
        assert aapl.metrics["score"] == pytest.approx(expected_score)
        assert aapl.metrics["score_rsi_pullback"] == pytest.approx(
            0.5 * expected_rsi_pullback
        )
        assert aapl.metrics["score_trend_quality"] == pytest.approx(0.3)
        assert aapl.metrics["score_liquidity"] == pytest.approx(0.16)

    def test_score_matches_hand_calculation_example_2_boundary_clamps(
        self, settings, monkeypatch
    ):
        # Issue Example 2: rsi14 == rsi_threshold -> rsi_pullback=0;
        # sma50/sma200-1 = 0.15 (> 0.10) -> trend_quality clamped to 1.0;
        # single candidate -> liquidity = 0.5.
        metrics_by_symbol = {"XYZ": _metrics(rsi14=45.0, sma50=115.0)}

        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)

        xyz = candidates[0]
        assert xyz.metrics["score_rsi_pullback"] == pytest.approx(0.0)
        assert xyz.metrics["score_trend_quality"] == pytest.approx(0.3)
        assert xyz.metrics["score_liquidity"] == pytest.approx(0.1)
        assert xyz.metrics["score"] == pytest.approx(0.4)

    def test_rsi_pullback_clamped_to_zero_when_rsi_above_threshold(
        self, settings, monkeypatch
    ):
        metrics_by_symbol = {"AAA": _metrics(rsi14=60.0)}
        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)
        assert candidates[0].metrics["score_rsi_pullback"] == pytest.approx(0.0)

    def test_rsi_pullback_is_full_weight_when_rsi_is_zero(self, settings, monkeypatch):
        metrics_by_symbol = {"AAA": _metrics(rsi14=0.0)}
        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)
        assert candidates[0].metrics["score_rsi_pullback"] == pytest.approx(0.5)

    def test_trend_quality_is_partial_below_normalization_band(
        self, settings, monkeypatch
    ):
        # sma50/sma200 - 1 = 0.05 -> trend_quality = 0.5 (not clamped).
        metrics_by_symbol = {"AAA": _metrics(rsi14=45.0, sma50=105.0)}
        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)
        assert candidates[0].metrics["score_trend_quality"] == pytest.approx(0.15)

    def test_trend_quality_is_exactly_full_weight_at_normalization_boundary(
        self, settings, monkeypatch
    ):
        # sma50/sma200 - 1 == 0.10 exactly -> trend_quality = 1.0.
        metrics_by_symbol = {"AAA": _metrics(rsi14=45.0, sma50=110.0)}
        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)
        assert candidates[0].metrics["score_trend_quality"] == pytest.approx(0.3)

    def test_liquidity_percentile_across_multiple_candidates(
        self, settings, monkeypatch
    ):
        # REQ-005: lowest avg_volume -> 0.0, middle -> 0.5, highest -> 1.0.
        metrics_by_symbol = {
            "LOW": _metrics(rsi14=45.0, avg_volume=10.0),
            "MID": _metrics(rsi14=45.0, avg_volume=20.0),
            "HIGH": _metrics(rsi14=45.0, avg_volume=30.0),
        }
        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)
        by_symbol = {c.symbol: c for c in candidates}
        assert by_symbol["LOW"].metrics["score_liquidity"] == pytest.approx(0.0)
        assert by_symbol["MID"].metrics["score_liquidity"] == pytest.approx(0.1)
        assert by_symbol["HIGH"].metrics["score_liquidity"] == pytest.approx(0.2)

    def test_liquidity_is_deterministic_fixed_value_for_a_single_candidate(
        self, settings, monkeypatch
    ):
        metrics_by_symbol = {"SOLO": _metrics(rsi14=45.0)}
        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)
        assert candidates[0].metrics["score_liquidity"] == pytest.approx(0.1)

    def test_tie_broken_by_ascending_symbol_when_scores_are_equal(
        self, settings, monkeypatch
    ):
        # Zero out the liquidity weight so distinct avg_volume values (which
        # would otherwise rank differently by percentile) can't break the
        # tie themselves — only rsi_pullback/trend_quality drive the score,
        # and both are identical here.
        metrics_by_symbol = {
            "ZZZ": _metrics(rsi14=45.0, avg_volume=10.0),
            "AAA": _metrics(rsi14=45.0, avg_volume=20.0),
        }
        ranking = {
            "score_weights": {
                "rsi_pullback": 0.5,
                "trend_quality": 0.5,
                "liquidity": 0.0,
            }
        }
        candidates = _score_pipeline(
            settings, monkeypatch, metrics_by_symbol, ranking=ranking
        )
        assert [c.symbol for c in candidates] == ["AAA", "ZZZ"]
        assert candidates[0].metrics["score"] == pytest.approx(
            candidates[1].metrics["score"]
        )
        assert [c.rank for c in candidates] == [1, 2]

    def test_custom_score_weights_change_the_score(self, settings, monkeypatch):
        # REQ-030: a strategy overriding score_weights (summing to 1.0) uses
        # those weights, not the defaults.
        metrics_by_symbol = {"AAA": _metrics(rsi14=0.0)}
        ranking = {
            "score_weights": {
                "rsi_pullback": 1.0,
                "trend_quality": 0.0,
                "liquidity": 0.0,
            }
        }
        candidates = _score_pipeline(
            settings, monkeypatch, metrics_by_symbol, ranking=ranking
        )

        # rsi14=0 -> rsi_pullback=1.0; weight 1.0 -> score == 1.0 regardless
        # of trend_quality/liquidity.
        assert candidates[0].metrics["score"] == pytest.approx(1.0)
        assert candidates[0].metrics["score_rsi_pullback"] == pytest.approx(1.0)
        assert candidates[0].metrics["score_trend_quality"] == pytest.approx(0.0)
        assert candidates[0].metrics["score_liquidity"] == pytest.approx(0.0)

    def test_score_fields_present_in_candidate_metrics(self, settings, monkeypatch):
        # REQ-006 minimal check: score fields land in Candidate.metrics,
        # which `record_candidates` serializes verbatim.
        metrics_by_symbol = {"AAPL": _metrics(rsi14=30.0, sma50=110.0)}
        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)
        assert {
            "score",
            "score_rsi_pullback",
            "score_trend_quality",
            "score_liquidity",
        } <= candidates[0].metrics.keys()

    def test_empty_candidate_set_produces_no_error(self, settings, monkeypatch):
        candidates = _score_pipeline(settings, monkeypatch, {})
        assert candidates == []


def _metrics(
    *,
    rsi14: float,
    avg_volume: float = 10.0,
    sma50: float = 100.0,
    sma200: float = 100.0,
) -> dict[str, float]:
    return {
        "rsi14": rsi14,
        "atr14": 1.0,
        "avg_volume": avg_volume,
        "close": 100.0,
        "sma50": sma50,
        "sma200": sma200,
    }


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


def test_fresh_process_registers_builtin_components_without_test_side_effects():
    code = """
from swing_copilot.config import Settings
from swing_copilot.screening.pipeline import ScreeningPipeline

ScreeningPipeline(
    {"strategies": {"default": {
        "filters_all": ["volume_min"],
        "signals_all": ["trend_sma"],
        "candidate_limit": 10,
    }}},
    market_store=None,
    settings=Settings(),
)
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and static test code
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
