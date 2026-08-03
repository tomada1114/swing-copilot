"""Compose registered Filters/Signals per `strategies.yaml` (FR-04, FR-05, NFR-07).

`ScreeningPipeline` never imports a concrete Filter/Signal class by name —
only the registry populated by `@register_filter`/`@register_signal` — so a
new strategy module needs only a one-line addition to `strategies.yaml`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from swing_copilot.config import StrategiesConfig
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - registers built-ins
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - registers built-ins
)
from swing_copilot.screening.base import (
    FILTER_REGISTRY,
    SIGNAL_REGISTRY,
    Candidate,
    ScreeningResult,
)
from swing_copilot.screening.indicators import (
    percentile_ranks,
    sma,
    symbol_bars,
    wilder_atr,
    wilder_rsi,
)
from swing_copilot.screening.rejection_classifier import (
    RejectionPlan,
    classify_rejections,
)

if TYPE_CHECKING:
    from swing_copilot.config import ExecutionStateConfig, ScoreWeights, Settings
    from swing_copilot.screening.base import ScreeningInput, SignalHit
    from swing_copilot.storage.market_store import MarketStore

_RSI_WINDOW = 14
_ATR_WINDOW = 14
_AVG_VOLUME_WINDOW = 20
_SMA_SHORT_WINDOW = 50
_SMA_LONG_WINDOW = 200

# Composite ranking score (P1-01, roadmap §5): normalization width for the
# trend_quality component's (sma50/sma200 - 1) ratio.
_TREND_QUALITY_NORMALIZATION = 0.10
# Full marks for the atr_pct component: an ATR14 of 6% of price. Chosen well
# above the S&P 500 median (~3.1%) so the component still discriminates among
# the genuinely volatile names rather than saturating across the universe.
_ATR_PCT_NORMALIZATION = 0.06
_DAMAGED_MAX_D = -3.0
_FAIR_MAX_D = 2.0
_EXTENDED_MAX_D = 4.0


class ScreeningPipeline:
    """Runs Filter -> Signal -> deterministic Candidate ranking for one strategy."""

    def __init__(
        self,
        strategies_config: StrategiesConfig | dict[str, Any],
        market_store: MarketStore | None,
        settings: Settings,
        strategy_key: str = "default",
    ) -> None:
        """Create the pipeline for one named strategy.

        Args:
            strategies_config: Parsed `strategies.yaml` (`{"strategies": {...}}`).
            market_store: Kept for parity with the documented signature; not
                queried directly here (all data arrives via `ScreeningInput`).
            settings: Loaded application settings, passed through to every
                registered Filter/Signal's constructor.
            strategy_key: Which `strategies.yaml` entry to run.

        Raises:
            KeyError: `strategy_key`, or one of its filter/signal keys, is
                not registered.
        """
        self._market_store = market_store
        self.strategy_key = strategy_key
        typed_config = (
            strategies_config
            if isinstance(strategies_config, StrategiesConfig)
            else StrategiesConfig.model_validate(strategies_config)
        )
        spec = typed_config.strategies[strategy_key]

        self._filters = [FILTER_REGISTRY[key](settings) for key in spec.filters_all]
        self._signals = [
            cast("Any", SIGNAL_REGISTRY[key])(
                settings, min_criteria=spec.minervini.min_criteria
            )
            if key == "minervini_stage2" and spec.minervini is not None
            else SIGNAL_REGISTRY[key](settings)
            for key in spec.signals_all
        ]
        self._candidate_limit = spec.candidate_limit
        self._rsi_threshold = settings.technical_signals.pullback.rsi_threshold
        self._score_weights: ScoreWeights = spec.ranking.score_weights
        self._settings = settings
        self._execution_config = settings.technical_signals.execution

    def run(self, data: ScreeningInput) -> list[Candidate]:
        """Run the two-stage screen and return a ranked, capped candidate list.

        Args:
            data: Point-in-time screening input.

        Returns:
            At most `candidate_limit` candidates, ranked by descending
            composite score (`score = sum(weight_i * component_i)`, P1-01),
            with symbol ascending as the deterministic tiebreak (REQ-010).
        """
        # Deliberately not `run_with_rejections(data).candidates`: classifying
        # why every *rejected* symbol was rejected is report-facing work whose
        # result this method discards, and it cannot influence the candidates
        # (they are already decided by `_build_candidates`). Paying for it here
        # made it roughly half the cost of a backtest, which calls this once
        # per simulated day. `run_with_rejections` is unchanged for the daily
        # path that actually renders the reasons.
        return self._build_candidates(data)[0]

    def run_with_rejections(self, data: ScreeningInput) -> ScreeningResult:
        """Run the two-stage screen and also classify every rejected symbol.

        Args:
            data: Point-in-time screening input.

        Returns:
            `candidates` identical to `run()`'s output; `rejections` covers
            every universe symbol that did not pass every configured Filter
            and every configured Signal (P1-02, roadmap §5). See
            `rejection_classifier.classify_rejections` for the exact
            priority order and its one intentional gap (candidate_limit
            truncation is not itself a rejection reason).
        """
        candidates, rankable_symbols, hits_by_signal = self._build_candidates(data)
        rejections = classify_rejections(
            data,
            self._settings,
            candidate_symbols=rankable_symbols,
            plan=RejectionPlan(
                filter_order=tuple(filter_.name for filter_ in self._filters),
                signal_order=tuple(signal.name for signal in self._signals),
                hits_by_signal=tuple(tuple(hits) for hits in hits_by_signal),
            ),
        )
        return ScreeningResult(candidates=candidates, rejections=rejections)

    def _build_candidates(
        self, data: ScreeningInput
    ) -> tuple[list[Candidate], set[str], list[list[SignalHit]]]:
        """Shared filter->signal->rank body for `run()`/`run_with_rejections()`.

        Returns:
            The ranked, capped candidate list; the pre-limit set of symbols
            with valid ranking metrics (used by the rejection classifier);
            and each signal's raw hits, in configured order.
        """
        filtered = {member.symbol for member in data.universe}
        for filter_ in self._filters:
            filtered &= filter_.apply(data)

        hits_by_signal = [signal.evaluate(data, filtered) for signal in self._signals]
        candidate_symbols = filtered
        for hits in hits_by_signal:
            candidate_symbols &= {hit.symbol for hit in hits}
        if not self._signals:
            candidate_symbols = set()

        rows = []
        for symbol in candidate_symbols:
            ranking_metrics = self._ranking_metrics(data, symbol)
            if ranking_metrics is None:
                continue
            signal_names = tuple(
                sorted(
                    {
                        hit.signal_name
                        for hits in hits_by_signal
                        for hit in hits
                        if hit.symbol == symbol
                    }
                )
            )
            metrics: dict[str, float] = {}
            for hits in hits_by_signal:
                for hit in hits:
                    if hit.symbol == symbol:
                        metrics.update(hit.metrics)
            metrics.update(ranking_metrics)
            rows.append((symbol, signal_names, metrics))

        rankable_symbols = {symbol for symbol, _signal_names, _metrics in rows}
        self._score_rows(rows)
        classified_rows = [
            (
                symbol,
                signal_names,
                metrics,
                _execution_state(_execution_distance(metrics), self._execution_config),
                _execution_distance(metrics),
            )
            for symbol, signal_names, metrics in rows
        ]
        classified_rows.sort(
            key=lambda row: _state_sort_key(row[3], row[2]["score"], row[0])
        )
        limited = classified_rows[: self._candidate_limit]
        candidates = [
            Candidate(
                symbol=symbol,
                as_of=data.as_of,
                signal_names=signal_names,
                metrics=metrics,
                rank=index + 1,
                execution_state=execution_state,
                execution_distance=execution_distance,
            )
            for index, (
                symbol,
                signal_names,
                metrics,
                execution_state,
                execution_distance,
            ) in enumerate(limited)
        ]
        return candidates, rankable_symbols, hits_by_signal

    def _score_rows(
        self, rows: list[tuple[str, tuple[str, ...], dict[str, float]]]
    ) -> None:
        """Compute and store the composite score and its breakdown, in place.

        `liquidity` is each row's `avg_volume` percentile within `rows` (the
        current candidate set, not the full universe): ascending by
        `avg_volume`, lowest gets 0.0 and highest gets 1.0. A single-row set
        gets the fixed midpoint 0.5 (no population to rank against).

        `atr_pct` is deliberately *not* a within-set percentile: with a
        candidate set of roughly five names, a percentile would reproduce the
        same small-population noise `liquidity` already suffers from. It is
        normalized against a fixed ATR% instead, so the same volatility always
        earns the same component value across runs.
        """
        weights = self._score_weights
        rsi_threshold = self._rsi_threshold
        liquidity_by_symbol = percentile_ranks(
            {symbol: metrics["avg_volume"] for symbol, _names, metrics in rows}
        )
        for symbol, _signal_names, metrics in rows:
            liquidity = liquidity_by_symbol[symbol]
            rsi_pullback = _clamp01((rsi_threshold - metrics["rsi14"]) / rsi_threshold)
            trend_quality = _clamp01(
                (metrics["sma50"] / metrics["sma200"] - 1)
                / _TREND_QUALITY_NORMALIZATION
            )
            atr_pct = _clamp01(
                (metrics["atr14"] / metrics["close"]) / _ATR_PCT_NORMALIZATION
            )
            score_rsi_pullback = weights.rsi_pullback * rsi_pullback
            score_trend_quality = weights.trend_quality * trend_quality
            score_liquidity = weights.liquidity * liquidity
            score_atr_pct = weights.atr_pct * atr_pct
            metrics.update(
                {
                    "score": (
                        score_rsi_pullback
                        + score_trend_quality
                        + score_liquidity
                        + score_atr_pct
                    ),
                    "score_rsi_pullback": score_rsi_pullback,
                    "score_trend_quality": score_trend_quality,
                    "score_liquidity": score_liquidity,
                    "score_atr_pct": score_atr_pct,
                }
            )

    @staticmethod
    def _ranking_metrics(data: ScreeningInput, symbol: str) -> dict[str, float] | None:
        """Compute rsi14/atr14/avg_volume/sma50/sma200 from bars, or None if unavailable.

        Computed independently of whichever signals happen to be configured,
        so ranking and report metrics are always available and consistent
        (docs/04_detailed_design.md 2.1 #4). A symbol with any NaN metric
        (e.g. insufficient history) is dropped from the candidate set.
        """
        series = symbol_bars(data.bars, symbol, data.as_of)
        if series is None or len(series) < max(
            _RSI_WINDOW, _ATR_WINDOW, _AVG_VOLUME_WINDOW
        ):
            return None

        rsi14 = wilder_rsi(series["close"], _RSI_WINDOW).iloc[-1]
        atr14 = wilder_atr(
            series["high"], series["low"], series["close"], _ATR_WINDOW
        ).iloc[-1]
        avg_volume = series["volume"].tail(_AVG_VOLUME_WINDOW).mean()
        close = series["close"].iloc[-1]
        sma50 = sma(series["close"], _SMA_SHORT_WINDOW).iloc[-1]
        sma200 = sma(series["close"], _SMA_LONG_WINDOW).iloc[-1]
        if (
            pd.isna(rsi14)
            or pd.isna(atr14)
            or pd.isna(avg_volume)
            or pd.isna(close)
            or pd.isna(sma50)
            or pd.isna(sma200)
        ):
            return None
        return {
            "rsi14": float(rsi14),
            "atr14": float(atr14),
            "avg_volume": float(avg_volume),
            "close": float(close),
            "sma50": float(sma50),
            "sma200": float(sma200),
        }


def _execution_distance(metrics: dict[str, float]) -> float | None:
    """Return `(close - sma50) / atr14`, or `None` for missing/invalid inputs."""
    close = metrics.get("close")
    sma50 = metrics.get("sma50")
    atr14 = metrics.get("atr14")
    if close is None or sma50 is None or atr14 is None or atr14 <= 0.0:
        return None
    return (close - sma50) / atr14


def _execution_state(
    distance: float | None, config: ExecutionStateConfig | None = None
) -> str:
    """Classify P5-23's ATR-normalized entry timing state."""
    if distance is None:
        return "UNKNOWN"
    damaged_max_d = config.damaged_max_d if config else _DAMAGED_MAX_D
    fair_max_d = config.fair_max_d if config else _FAIR_MAX_D
    extended_max_d = config.extended_max_d if config else _EXTENDED_MAX_D
    if distance < damaged_max_d:
        return "DAMAGED"
    if distance < 0.0:
        return "PULLBACK_ZONE"
    if distance < fair_max_d:
        return "FAIR"
    if distance < extended_max_d:
        return "EXTENDED"
    return "OVEREXTENDED"


def _execution_bucket(state: str) -> str:
    """Map an execution state to its user-facing P5-23 bucket."""
    if state in {"PULLBACK_ZONE", "FAIR"}:
        return "即検討可"
    if state == "EXTENDED":
        return "様子見"
    return "見送り"


def _state_sort_key(state: str, score: float, symbol: str) -> tuple[int, float, str]:
    """State cap first, then the established score/symbol ordering."""
    bucket_order = {"即検討可": 0, "様子見": 1, "見送り": 2}
    return bucket_order[_execution_bucket(state)], -score, symbol


def _clamp01(value: float) -> float:
    """Clamp `value` into `[0, 1]`."""
    return max(0.0, min(1.0, value))
