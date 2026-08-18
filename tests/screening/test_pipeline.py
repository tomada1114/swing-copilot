"""Tests for ScreeningPipeline: AND semantics, ranking, extensibility (FR-04, FR-05, NFR-07)."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, date, datetime
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
    RejectionReasonCode,
    RejectionStage,
    ScreeningInput,
    SignalHit,
    register_signal,
)
from swing_copilot.screening.indicators import SymbolWindow
from swing_copilot.screening.pipeline import (
    PRICE_HISTORY_LOOKBACK_DAYS,
    ScreeningPipeline,
    price_history_lookback_days,
    strategy_required_bars,
)
from swing_copilot.universe import UniverseMember
from tests.screening.conftest import FundamentalsSpec, make_bars, make_fundamentals_row

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


def _healthy_fundamentals_df(symbols: list[str]) -> pd.DataFrame:
    """Fundamentals rows that pass `ProfitablePositiveFCFEquityFilter` cleanly.

    Used by `TestRunWithRejections` so a symbol rejected only by `volume_min`
    doesn't also (correctly, but confusingly for that test's purpose) get
    classified as fundamentals-`DATA_INSUFFICIENT_HISTORY` -- the rejection
    classifier mirrors the fundamentals check unconditionally, regardless of
    whether `profitable_positive_fcf_equity` is actually in `filters_all`.
    """
    rows = []
    quarter_ends = [
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
    ]
    filed_ats = [
        datetime(2025, 4, 15, tzinfo=UTC),
        datetime(2025, 7, 15, tzinfo=UTC),
        datetime(2025, 10, 15, tzinfo=UTC),
        datetime(2026, 1, 15, tzinfo=UTC),
    ]
    for symbol in symbols:
        for i in range(4):
            spec = FundamentalsSpec(
                accession_no=f"acc-{symbol}-{i}",
                fiscal_period_end=quarter_ends[i],
                filed_at=filed_ats[i],
                net_income=10.0,
                fcf=10.0,
                equity=60.0,
                assets=100.0,
            )
            rows.append(make_fundamentals_row(symbol, spec))
    return pd.DataFrame(rows)


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
            required_bars = 1

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
            result = pipeline.run_with_rejections(data)

            assert result.candidates == []
            [rejection] = result.rejections
            assert rejection.stage is RejectionStage.DATA_QUALITY
            assert (
                rejection.reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
            )
            assert rejection.detail["ranking_metrics"] == "unavailable"
        finally:
            del SIGNAL_REGISTRY["always_hit_short_history_test_signal"]

    def test_symbol_dropped_when_a_ranking_metric_is_nan_despite_enough_history(
        self, settings, monkeypatch
    ):
        # Defensive branch: even with >= 20 days of history (normally
        # guaranteeing non-NaN rsi14/atr14/avg_volume), a data gap could
        # still produce a NaN metric — such a symbol must not become a
        # candidate rather than sorting with a NaN key.
        # `trend_sma` reads no RSI, so pinning it to NaN isolates the
        # `ranking_metrics` guard the way patching `wilder_rsi` used to
        # before the indicator moved behind `SymbolWindow` (#214).
        monkeypatch.setattr(SymbolWindow, "rsi", lambda _self, _period: float("nan"))
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

    def test_symbol_dropped_when_the_last_close_is_zero(self, settings):
        # `_score_rows`'s `atr_pct` component divides by the close, so a
        # corrupt or placeholder row would abort the whole run -- every
        # symbol's screening, not just the bad one -- instead of costing the
        # one symbol. The NaN guard alone does not cover it: 0.0 is not NaN.
        bars = _uptrend_bars("AAPL")
        bars.loc[bars.index[-1], "close"] = 0.0
        universe = (_member("AAPL"),)
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )

        pipeline = ScreeningPipeline(
            STRATEGIES_CONFIG, market_store=None, settings=settings
        )

        assert pipeline.run(data) == []

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
    """Run the pipeline with `ranking_metrics` fully controlled per symbol.

    Isolates score-computation/ranking (`run()`) from indicator plumbing
    (`ranking_metrics`), which has its own coverage elsewhere.
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
        "swing_copilot.screening.pipeline.ranking_metrics",
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
            required_bars = 1

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

    def test_equal_liquidity_receives_equal_percentile_and_symbol_tiebreak(
        self, settings, monkeypatch
    ):
        metrics_by_symbol = {
            "ZZZ": _metrics(rsi14=45.0, avg_volume=20.0),
            "AAA": _metrics(rsi14=45.0, avg_volume=20.0),
        }

        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)

        assert [candidate.symbol for candidate in candidates] == ["AAA", "ZZZ"]
        assert candidates[0].metrics["score_liquidity"] == pytest.approx(0.1)
        assert candidates[0].metrics["score"] == pytest.approx(
            candidates[1].metrics["score"]
        )

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
            "score_atr_pct",
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
    atr14: float = 1.0,
) -> dict[str, float]:
    return {
        "rsi14": rsi14,
        "atr14": atr14,
        "avg_volume": avg_volume,
        "close": 100.0,
        "sma50": sma50,
        "sma200": sma200,
    }


_ATR_PCT_RANKING = {
    "score_weights": {
        "rsi_pullback": 0.3,
        "trend_quality": 0.3,
        "liquidity": 0.2,
        "atr_pct": 0.2,
    }
}


@pytest.mark.usefixtures("score_test_signal")
class TestAtrPctScoreComponent:
    """The volatility ranking component added to counter the low-vol bias."""

    @pytest.fixture
    def score_test_signal(self):
        @register_signal("score_test_signal")
        class _AlwaysHitSignal:
            name = "score_test_signal"
            required_bars = 1

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

    def test_default_weight_is_zero_so_existing_scores_are_unchanged(
        self, settings, monkeypatch
    ):
        # A 3%-ATR name under the repository's default weights must score
        # exactly as it did before the component existed.
        metrics_by_symbol = {"AAPL": _metrics(rsi14=30.0, sma50=110.0, atr14=3.0)}
        candidates = _score_pipeline(settings, monkeypatch, metrics_by_symbol)

        aapl = candidates[0]
        expected_rsi_pullback = (45.0 - 30.0) / 45.0
        expected_score = 0.5 * expected_rsi_pullback + 0.3 * 1.0 + 0.2 * 0.5
        assert aapl.metrics["score"] == pytest.approx(expected_score)
        assert aapl.metrics["score_atr_pct"] == pytest.approx(0.0)

    def test_score_matches_hand_calculation_with_the_component_weighted(
        self, settings, monkeypatch
    ):
        # atr14=3.0 on close=100.0 -> ATR% 3%, half of the 6% full-marks
        # normalization -> raw component 0.5.
        metrics_by_symbol = {"AAPL": _metrics(rsi14=30.0, sma50=110.0, atr14=3.0)}
        candidates = _score_pipeline(
            settings, monkeypatch, metrics_by_symbol, ranking=_ATR_PCT_RANKING
        )

        aapl = candidates[0]
        expected_rsi_pullback = (45.0 - 30.0) / 45.0
        expected_atr_pct = (3.0 / 100.0) / 0.06
        expected_score = (
            0.3 * expected_rsi_pullback + 0.3 * 1.0 + 0.2 * 0.5 + 0.2 * expected_atr_pct
        )
        assert aapl.metrics["score"] == pytest.approx(expected_score)
        assert aapl.metrics["score_atr_pct"] == pytest.approx(0.2 * expected_atr_pct)

    def test_the_component_saturates_at_the_full_marks_volatility(
        self, settings, monkeypatch
    ):
        # ATR% 9% is above the 6% normalization, so the component clamps to
        # 1.0 rather than scoring above full marks.
        metrics_by_symbol = {"AAPL": _metrics(rsi14=30.0, atr14=9.0)}
        candidates = _score_pipeline(
            settings, monkeypatch, metrics_by_symbol, ranking=_ATR_PCT_RANKING
        )

        assert candidates[0].metrics["score_atr_pct"] == pytest.approx(0.2)

    def test_a_higher_volatility_name_outranks_an_otherwise_identical_one(
        self, settings, monkeypatch
    ):
        metrics_by_symbol = {
            "CALM": _metrics(rsi14=30.0, atr14=1.0),
            "WILD": _metrics(rsi14=30.0, atr14=4.0),
        }
        candidates = _score_pipeline(
            settings, monkeypatch, metrics_by_symbol, ranking=_ATR_PCT_RANKING
        )

        assert [candidate.symbol for candidate in candidates] == ["WILD", "CALM"]


class TestExtensibility:
    def test_new_signal_can_be_registered_without_editing_pipeline(self, settings):
        @register_signal("always_hit_test_signal")
        class _AlwaysHitSignal:
            name = "always_hit_test_signal"
            required_bars = 1

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


class TestRunWithRejections:
    """P1-02: `run_with_rejections()` shares `run()`'s candidate output."""

    def test_candidates_match_run_exactly(self, settings):
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

        via_run = pipeline.run(data)
        result = pipeline.run_with_rejections(data)

        assert result.candidates == via_run

    def test_rejects_liquidity_failure_with_the_divergence_reason_code(self, settings):
        # STRATEGIES_CONFIG: filters_all=["volume_min"], signals_all=["trend_sma"].
        # Fundamentals are healthy for both symbols so the rejection
        # classifier's (always-on) fundamentals mirror doesn't mask the
        # liquidity rejection this test targets.
        bars = pd.concat(
            [
                _uptrend_bars("PASSES", volume=2_000_000),
                _uptrend_bars("LOW_VOLUME", volume=100),
            ]
        )
        universe = (_member("PASSES"), _member("LOW_VOLUME"))
        data = ScreeningInput(
            as_of=AS_OF,
            universe=universe,
            fundamentals=_healthy_fundamentals_df(["PASSES", "LOW_VOLUME"]),
            bars=bars,
        )
        pipeline = ScreeningPipeline(
            STRATEGIES_CONFIG, market_store=None, settings=settings
        )

        result = pipeline.run_with_rejections(data)

        assert [c.symbol for c in result.candidates] == ["PASSES"]
        assert [r.symbol for r in result.rejections] == ["LOW_VOLUME"]
        assert (
            result.rejections[0].reason_code is RejectionReasonCode.FILTER_LOW_LIQUIDITY
        )

    def test_all_pass_fixture_has_zero_rejections(self, settings):
        # REQ-010 boundary at the pipeline level: all universe symbols
        # become candidates -> rejections is empty, no exception.
        bars = pd.concat(
            [
                _uptrend_bars("AAPL", volume=2_000_000),
                _uptrend_bars("MSFT", volume=2_000_000),
            ]
        )
        universe = (_member("AAPL"), _member("MSFT"))
        data = ScreeningInput(
            as_of=AS_OF, universe=universe, fundamentals=pd.DataFrame(), bars=bars
        )
        pipeline = ScreeningPipeline(
            STRATEGIES_CONFIG, market_store=None, settings=settings
        )

        result = pipeline.run_with_rejections(data)

        assert len(result.candidates) == 2
        assert result.rejections == []

    def test_no_signals_configured_produces_no_rejections(self, settings):
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

        result = pipeline.run_with_rejections(data)

        assert result.candidates == []
        assert result.rejections == []


class TestCandidateLimitTruncationIsRecorded:
    """A `candidate_limit` cut leaves the symbol visible in `truncated`."""

    @staticmethod
    def _all_passing_data(count: int) -> ScreeningInput:
        symbols = [f"SYM{i:02d}" for i in range(count)]
        bars = pd.concat(
            [_uptrend_bars(symbol, base=100.0 + i) for i, symbol in enumerate(symbols)]
        )
        return ScreeningInput(
            as_of=AS_OF,
            universe=tuple(_member(symbol) for symbol in symbols),
            fundamentals=_healthy_fundamentals_df(symbols),
            bars=bars,
        )

    @staticmethod
    def _pipeline(settings: Settings, candidate_limit: int) -> ScreeningPipeline:
        return ScreeningPipeline(
            {
                "strategies": {
                    "default": {
                        "filters_all": ["volume_min"],
                        "signals_all": ["trend_sma"],
                        "candidate_limit": candidate_limit,
                    }
                }
            },
            market_store=None,
            settings=settings,
        )

    def test_symbols_cut_by_the_limit_are_reported_with_continuing_ranks(
        self, settings
    ):
        data = self._all_passing_data(15)

        result = self._pipeline(settings, 10).run_with_rejections(data)

        assert [candidate.rank for candidate in result.candidates] == list(range(1, 11))
        assert [item.rank for item in result.truncated] == [11, 12, 13, 14, 15]

    def test_a_truncated_symbol_is_neither_a_candidate_nor_a_rejection(self, settings):
        data = self._all_passing_data(15)

        result = self._pipeline(settings, 10).run_with_rejections(data)

        truncated_symbols = {item.symbol for item in result.truncated}
        assert len(truncated_symbols) == 5
        assert truncated_symbols.isdisjoint(
            candidate.symbol for candidate in result.candidates
        )
        assert truncated_symbols.isdisjoint(
            rejection.symbol for rejection in result.rejections
        )

    def test_no_truncation_when_the_candidate_set_fits_within_the_limit(self, settings):
        data = self._all_passing_data(10)

        result = self._pipeline(settings, 10).run_with_rejections(data)

        assert len(result.candidates) == 10
        assert result.truncated == []

    def test_truncated_entries_carry_the_full_score_breakdown(self, settings):
        data = self._all_passing_data(11)

        result = self._pipeline(settings, 10).run_with_rejections(data)

        item = result.truncated[0]
        assert set(item.score_breakdown) == {
            "score_rsi_pullback",
            "score_trend_quality",
            "score_liquidity",
            "score_atr_pct",
        }
        assert item.score == pytest.approx(sum(item.score_breakdown.values()))

    def test_run_ignores_truncation_and_still_returns_only_the_capped_list(
        self, settings
    ):
        data = self._all_passing_data(15)
        pipeline = self._pipeline(settings, 10)

        assert pipeline.run(data) == pipeline.run_with_rejections(data).candidates


class TestNoLookAheadFromUnslicedBars:
    """The backtest hands the pipeline bars extending past `as_of`.

    `screening/indicators.symbol_bars` is the only way screening reads price
    history and always applies the cutoff, so passing a whole frame must give
    byte-identical results to passing one pre-sliced to `as_of`. If a future
    filter or signal ever reads `data.bars` directly, this test fails instead
    of silently introducing look-ahead into every backtest.
    """

    def test_full_frame_and_pre_sliced_frame_produce_identical_candidates(
        self, settings
    ):
        as_of = date(2026, 1, 1)
        # 500 daily bars from 2025-01-01: ~365 on or before as_of (enough for
        # SMA200) and ~135 after it, so the unsliced frame genuinely contains
        # future rows the pipeline must ignore.
        closes = [100.0 + i for i in range(500)]
        bars = pd.concat(
            [
                make_bars("AAA", closes, start=date(2025, 1, 1)),
                make_bars("BBB", closes[::-1], start=date(2025, 1, 1)),
            ],
            ignore_index=True,
        )
        universe = tuple(_member(symbol) for symbol in ("AAA", "BBB"))
        pipeline = ScreeningPipeline(
            {
                "strategies": {
                    "default": {
                        "filters_all": [],
                        "signals_all": ["trend_sma"],
                        "candidate_limit": 10,
                    }
                }
            },
            market_store=None,
            settings=settings,
        )

        sliced = bars[bars["date"] <= as_of].copy()
        from_sliced = pipeline.run(
            ScreeningInput(
                as_of=as_of,
                universe=universe,
                fundamentals=pd.DataFrame(),
                bars=sliced,
            )
        )
        from_full = pipeline.run(
            ScreeningInput(
                as_of=as_of,
                universe=universe,
                fundamentals=pd.DataFrame(),
                bars=bars.copy(),
            )
        )

        assert [(c.symbol, c.rank, c.metrics) for c in from_full] == [
            (c.symbol, c.rank, c.metrics) for c in from_sliced
        ]
        assert bars["date"].max() > as_of  # the frame really did extend past as_of


class TestRunMatchesRunWithRejections:
    def test_run_returns_exactly_the_candidates_of_run_with_rejections(self, settings):
        """`run()` skips rejection classification; it must still agree."""
        closes = [100.0 + i for i in range(400)]
        bars = pd.concat(
            [
                make_bars("AAA", closes, start=date(2025, 1, 1)),
                make_bars("BBB", closes[::-1], start=date(2025, 1, 1)),
                make_bars("CCC", [100.0] * 400, start=date(2025, 1, 1)),
            ],
            ignore_index=True,
        )
        data = ScreeningInput(
            as_of=date(2026, 1, 1),
            universe=tuple(_member(s) for s in ("AAA", "BBB", "CCC")),
            fundamentals=pd.DataFrame(),
            bars=bars,
        )
        pipeline = ScreeningPipeline(
            {
                "strategies": {
                    "default": {
                        "filters_all": [],
                        "signals_all": ["trend_sma"],
                        "candidate_limit": 10,
                    }
                }
            },
            market_store=None,
            settings=settings,
        )

        assert [(c.symbol, c.rank, c.metrics) for c in pipeline.run(data)] == [
            (c.symbol, c.rank, c.metrics)
            for c in pipeline.run_with_rejections(data).candidates
        ]


class TestRequiredBars:
    """#186: signals declare their bar needs; callers derive lookback from them."""

    @staticmethod
    def _pipeline(settings: Settings, signals: list[str]) -> ScreeningPipeline:
        return ScreeningPipeline(
            {
                "strategies": {
                    "default": {
                        "filters_all": [],
                        "signals_all": signals,
                        "candidate_limit": 10,
                    }
                }
            },
            market_store=None,
            settings=settings,
        )

    def test_required_bars_is_the_ranking_window_when_no_signal_needs_more(
        self, settings
    ):
        # trend_sma (200) and pullback_rsi (50) never exceed the SMA200
        # ranking warmup.
        pipeline = self._pipeline(settings, ["trend_sma", "pullback_rsi"])

        assert pipeline.required_bars == 200

    def test_required_bars_extends_to_the_vcp_pattern_window(self, settings):
        pipeline = self._pipeline(settings, ["vcp_breakout"])

        expected = settings.technical_signals.vcp.pattern_days_max + 60
        assert pipeline.required_bars == expected

    def test_required_bars_floors_at_ranking_window_with_no_signals(self, settings):
        assert self._pipeline(settings, []).required_bars == 200

    def test_lookback_days_keeps_the_pre_186_floor(self):
        # 200 bars is exactly what the long-standing 400-day constant served.
        assert price_history_lookback_days(200) == PRICE_HISTORY_LOOKBACK_DAYS
        assert price_history_lookback_days(1) == PRICE_HISTORY_LOOKBACK_DAYS

    def test_lookback_days_scales_past_the_floor(self, settings):
        pipeline = self._pipeline(settings, ["vcp_breakout"])

        assert price_history_lookback_days(pipeline.required_bars) == 770

    def test_strategy_required_bars_matches_the_pipeline_property(self, settings):
        config = {
            "strategies": {
                "vcp": {
                    "filters_all": [],
                    "signals_all": ["vcp_breakout"],
                    "candidate_limit": 10,
                }
            }
        }

        assert (
            strategy_required_bars(config, settings, "vcp")
            == ScreeningPipeline(config, None, settings, "vcp").required_bars
        )
