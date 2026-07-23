"""Compose registered Filters/Signals per `strategies.yaml` (FR-04, FR-05, NFR-07).

`ScreeningPipeline` never imports a concrete Filter/Signal class by name —
only the registry populated by `@register_filter`/`@register_signal` — so a
new strategy module needs only a one-line addition to `strategies.yaml`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

from swing_copilot.config import StrategiesConfig
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - registers built-ins
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - registers built-ins
)
from swing_copilot.screening.base import FILTER_REGISTRY, SIGNAL_REGISTRY, Candidate
from swing_copilot.screening.indicators import sma, symbol_bars, wilder_atr, wilder_rsi

if TYPE_CHECKING:
    from swing_copilot.config import ScoreWeights, Settings
    from swing_copilot.screening.base import ScreeningInput
    from swing_copilot.storage.market_store import MarketStore

_RSI_WINDOW = 14
_ATR_WINDOW = 14
_AVG_VOLUME_WINDOW = 20
_SMA_SHORT_WINDOW = 50
_SMA_LONG_WINDOW = 200

# Composite ranking score (P1-01, roadmap §5): normalization width for the
# trend_quality component's (sma50/sma200 - 1) ratio.
_TREND_QUALITY_NORMALIZATION = 0.10


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
        self._signals = [SIGNAL_REGISTRY[key](settings) for key in spec.signals_all]
        self._candidate_limit = spec.candidate_limit
        self._rsi_threshold = settings.technical_signals.pullback.rsi_threshold
        self._score_weights: ScoreWeights = spec.ranking.score_weights

    def run(self, data: ScreeningInput) -> list[Candidate]:
        """Run the two-stage screen and return a ranked, capped candidate list.

        Args:
            data: Point-in-time screening input.

        Returns:
            At most `candidate_limit` candidates, ranked by descending
            composite score (`score = sum(weight_i * component_i)`, P1-01),
            with symbol ascending as the deterministic tiebreak (REQ-010).
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

        self._score_rows(rows)
        rows.sort(key=lambda row: (-row[2]["score"], row[0]))
        limited = rows[: self._candidate_limit]
        return [
            Candidate(
                symbol=symbol,
                as_of=data.as_of,
                signal_names=signal_names,
                metrics=metrics,
                rank=index + 1,
            )
            for index, (symbol, signal_names, metrics) in enumerate(limited)
        ]

    def _score_rows(
        self, rows: list[tuple[str, tuple[str, ...], dict[str, float]]]
    ) -> None:
        """Compute and store the composite score and its breakdown, in place.

        `liquidity` is each row's `avg_volume` percentile within `rows` (the
        current candidate set, not the full universe): ascending by
        `avg_volume`, lowest gets 0.0 and highest gets 1.0. A single-row set
        gets the fixed midpoint 0.5 (no population to rank against).
        """
        weights = self._score_weights
        rsi_threshold = self._rsi_threshold
        ordered = sorted(range(len(rows)), key=lambda i: rows[i][2]["avg_volume"])
        row_count = len(ordered)
        for percentile_rank, row_index in enumerate(ordered):
            metrics = rows[row_index][2]
            liquidity = 0.5 if row_count == 1 else percentile_rank / (row_count - 1)
            rsi_pullback = _clamp01((rsi_threshold - metrics["rsi14"]) / rsi_threshold)
            trend_quality = _clamp01(
                (metrics["sma50"] / metrics["sma200"] - 1)
                / _TREND_QUALITY_NORMALIZATION
            )
            score_rsi_pullback = weights.rsi_pullback * rsi_pullback
            score_trend_quality = weights.trend_quality * trend_quality
            score_liquidity = weights.liquidity * liquidity
            metrics.update(
                {
                    "score": score_rsi_pullback + score_trend_quality + score_liquidity,
                    "score_rsi_pullback": score_rsi_pullback,
                    "score_trend_quality": score_trend_quality,
                    "score_liquidity": score_liquidity,
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


def _clamp01(value: float) -> float:
    """Clamp `value` into `[0, 1]`."""
    return max(0.0, min(1.0, value))
