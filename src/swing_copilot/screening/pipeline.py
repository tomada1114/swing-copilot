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
from swing_copilot.screening.indicators import symbol_bars, wilder_atr, wilder_rsi

if TYPE_CHECKING:
    from swing_copilot.config import Settings
    from swing_copilot.screening.base import ScreeningInput
    from swing_copilot.storage.market_store import MarketStore

_RSI_WINDOW = 14
_ATR_WINDOW = 14
_AVG_VOLUME_WINDOW = 20


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

    def run(self, data: ScreeningInput) -> list[Candidate]:
        """Run the two-stage screen and return a ranked, capped candidate list.

        Args:
            data: Point-in-time screening input.

        Returns:
            At most `candidate_limit` candidates, ranked by
            `(rsi14 asc, avg_volume desc, symbol asc)`.
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

        rows.sort(key=lambda row: (row[2]["rsi14"], -row[2]["avg_volume"], row[0]))
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

    @staticmethod
    def _ranking_metrics(data: ScreeningInput, symbol: str) -> dict[str, float] | None:
        """Compute rsi14/atr14/avg_volume directly from bars, or None if unavailable.

        Computed independently of whichever signals happen to be configured,
        so ranking and report metrics are always available and consistent
        (docs/04_detailed_design.md 2.1 #4).
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
        if pd.isna(rsi14) or pd.isna(atr14) or pd.isna(avg_volume) or pd.isna(close):
            return None
        return {
            "rsi14": float(rsi14),
            "atr14": float(atr14),
            "avg_volume": float(avg_volume),
            "close": float(close),
        }
