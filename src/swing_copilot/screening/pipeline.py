"""Compose registered Filters/Signals per `strategies.yaml` (FR-04, FR-05, NFR-07).

`ScreeningPipeline` never imports a concrete Filter/Signal class by name —
only the registry populated by `@register_filter`/`@register_signal` — so a
new strategy module needs only a one-line addition to `strategies.yaml`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from swing_copilot.config import ScoreWeights, StrategiesConfig
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
    TruncatedCandidate,
)
from swing_copilot.screening.execution import EXECUTION_BUCKETS, execution_bucket
from swing_copilot.screening.indicators import (
    percentile_ranks,
    symbol_window,
)
from swing_copilot.screening.rejection_classifier import (
    RejectionPlan,
    classify_rejections,
)
from swing_copilot.screening.technical_signals import MINERVINI_CRITERIA_TOTAL

if TYPE_CHECKING:
    from collections.abc import Mapping

    from swing_copilot.config import (
        ExecutionStateConfig,
        Settings,
        StrategySpec,
    )
    from swing_copilot.screening.base import Filter, ScreeningInput, Signal, SignalHit
    from swing_copilot.storage.market_store import MarketStore

_RSI_WINDOW = 14
#: Window of the `atr14` ranking metric. Deliberately *not* unified with
#: `settings.trade_plan.exit_atr_period` (Issue #194): that key is the exit
#: side's smoothing period and may be swept on its own, while this one feeds
#: ranking and -- through `risk/checks.py` -- the entry stop distance, so
#: moving it would silently redefine the score and the position size.
_ATR_WINDOW = 14
_AVG_VOLUME_WINDOW = 20
_SMA_SHORT_WINDOW = 50
_SMA_LONG_WINDOW = 200

#: Calendar days of price history any caller must read back from `as_of` to
#: give `ranking_metrics` a full `_SMA_LONG_WINDOW` warmup. Public and owned
#: here (not by `pipeline/daily.py`) because the requirement comes from the
#: screening indicators, and read-only screening diagnostics must be able to
#: read the same window without importing the daily orchestration module.
#: Unrelated to `edgar.py`'s own fundamentals-fetch lookback constant.
#: Since Issue #186 this is the *floor*: a strategy whose signals declare a
#: longer `required_bars` extends it via `price_history_lookback_days`.
PRICE_HISTORY_LOOKBACK_DAYS = 400

#: Calendar days reserved per required trading bar. Two-to-one is the margin
#: the long-standing 400-day constant already gave the 200-bar ranking SMA;
#: it absorbs weekends, holidays, and ordinary data gaps.
_CALENDAR_DAYS_PER_BAR = 2

# Composite ranking score (P1-01, roadmap §5): normalization width for the
# trend_quality component's (sma50/sma200 - 1) ratio.
_TREND_QUALITY_NORMALIZATION = 0.10
# Full marks for the atr_pct component: an ATR14 of 6% of price. Chosen well
# above the S&P 500 median (~3.1%) so the component still discriminates among
# the genuinely volatile names rather than saturating across the universe.
_ATR_PCT_NORMALIZATION = 0.06
#: Denominator of the `criteria_met` component: the Minervini trend template's
#: conditions (P5-21), shared with the signal that counts them so the two
#: cannot normalize against different totals.
_MINERVINI_CRITERIA_TOTAL = float(MINERVINI_CRITERIA_TOTAL)
#: Denominator of the `rs_percentile` component: `minervini_rs_percentile` is
#: recorded on a 0-100 scale.
_RS_PERCENTILE_SCALE = 100.0
_DAMAGED_MAX_D = -3.0
_FAIR_MAX_D = 2.0
_EXTENDED_MAX_D = 4.0

#: `_score_rows`'s per-component contributions, in weight-declaration order.
#: Derived from `ScoreWeights` so a component can never be weighted without
#: also being broken out (Issue #251); `tests/screening/test_pipeline.py`
#: pins the resulting key set.
_SCORE_COMPONENT_KEYS = tuple(f"score_{name}" for name in ScoreWeights.model_fields)


@dataclass(frozen=True, slots=True)
class _BuildOutcome:
    """`_build_candidates`'s full output, shared by both public entry points."""

    candidates: list[Candidate]
    #: Symbols with valid ranking metrics, before `candidate_limit` truncation.
    rankable_symbols: set[str]
    hits_by_signal: list[list[SignalHit]]
    truncated: list[TruncatedCandidate]


def build_strategy_components(
    spec: StrategySpec, settings: Settings
) -> tuple[list[Filter], list[Signal]]:
    """Instantiate one strategy's configured Filters and Signals, in configured order.

    Shared by `ScreeningPipeline` and `filter_matrix.evaluate_filter_matrix`, so
    the registry lookup and `minervini_stage2`'s strategy-specific
    `min_criteria` wiring exist in exactly one place: a diagnostic that
    composed its own components could silently measure a different strategy
    than the one the daily run screens with.

    Args:
        spec: One validated `strategies.yaml` entry.
        settings: Loaded application settings, passed to every component.

    Returns:
        The configured filters and signals, each in `strategies.yaml` order.

    Raises:
        KeyError: A configured filter/signal key is not registered.
    """
    filters = [FILTER_REGISTRY[key](settings) for key in spec.filters_all]
    signals = [
        cast("Any", SIGNAL_REGISTRY[key])(
            settings, min_criteria=spec.minervini.min_criteria
        )
        if key == "minervini_stage2" and spec.minervini is not None
        else SIGNAL_REGISTRY[key](settings)
        for key in spec.signals_all
    ]
    return filters, signals


def price_history_lookback_days(required_bars: int) -> int:
    """Calendar days of history a caller must read back to cover `required_bars`.

    Args:
        required_bars: Trading bars the screening run needs at one `as_of`,
            normally `ScreeningPipeline.required_bars`.

    Returns:
        At least `PRICE_HISTORY_LOOKBACK_DAYS`, so no caller ever reads a
        shorter window than the pre-#186 constant supplied.
    """
    return max(PRICE_HISTORY_LOOKBACK_DAYS, required_bars * _CALENDAR_DAYS_PER_BAR)


def strategy_required_bars(
    strategies_config: StrategiesConfig | dict[str, Any],
    settings: Settings,
    strategy_key: str = "default",
) -> int:
    """Trading bars the named strategy needs, without keeping the pipeline.

    For callers (price fetch, prefetch) that must size their read before the
    screening step builds its own `ScreeningPipeline`.

    Args:
        strategies_config: Parsed `strategies.yaml`.
        settings: Loaded application settings.
        strategy_key: Which `strategies.yaml` entry will screen.

    Returns:
        `ScreeningPipeline.required_bars` for that strategy.

    Raises:
        KeyError: `strategy_key`, or one of its filter/signal keys, is not
            registered.
    """
    return ScreeningPipeline(
        strategies_config, None, settings, strategy_key
    ).required_bars


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

        self._filters, self._signals = build_strategy_components(spec, settings)
        self._candidate_limit = spec.candidate_limit
        self._rsi_threshold = settings.technical_signals.pullback.rsi_threshold
        self._score_weights: ScoreWeights = spec.ranking.score_weights
        self._settings = settings
        self._execution_config = settings.technical_signals.execution
        # Issue #297: `pivot_proximity`'s decay width is derived from the same
        # `chase_pivot_pct` that caps how far above the pivot a VCP hit may be
        # (`screening/vcp.py`'s `VcpThresholds.chase_pivot_pct`), rather than
        # a separately hardcoded constant. The two describe the same band --
        # the filter's admitted range and the score's dynamic range -- so
        # deriving one from the other keeps them from silently drifting apart
        # (a `config.py`-validated `float`, not I/O; passed down as a plain
        # argument the same way `vcp.py`'s own signal receives it).
        self._pivot_proximity_width = settings.technical_signals.vcp.chase_pivot_pct

    @property
    def candidate_limit(self) -> int:
        """The configured cap this run's ranking is truncated at.

        Exposed (Issue #188) so the persistence layer can size how much of
        the truncated tail to keep relative to the cut, instead of the call
        site re-reading `strategies.yaml` to find the same number.
        """
        return self._candidate_limit

    @property
    def required_bars(self) -> int:
        """Trading bars one screening run needs at a single `as_of`.

        The maximum of every configured Signal's declared `required_bars`
        and the ranking indicators' `_SMA_LONG_WINDOW`, so both the daily
        pipeline and the backtest runner size their history reads from one
        declaration instead of two divergent hardcoded lookbacks (#186).
        """
        return max(
            [_SMA_LONG_WINDOW, *(signal.required_bars for signal in self._signals)]
        )

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
        return self._build_candidates(data).candidates

    def run_with_rejections(self, data: ScreeningInput) -> ScreeningResult:
        """Run the two-stage screen and also classify every rejected symbol.

        Args:
            data: Point-in-time screening input.

        Returns:
            `candidates` identical to `run()`'s output; `rejections` covers
            every universe symbol that did not pass every configured Filter
            and every configured Signal (P1-02, roadmap §5). See
            `rejection_classifier.classify_rejections` for the exact
            priority order and its one intentional gap: `candidate_limit`
            truncation is not a rejection reason, so those symbols are
            reported separately in `truncated`.
        """
        outcome = self._build_candidates(data)
        rejections = classify_rejections(
            data,
            self._settings,
            candidate_symbols=outcome.rankable_symbols,
            plan=RejectionPlan(
                filter_order=tuple(filter_.name for filter_ in self._filters),
                signal_order=tuple(signal.name for signal in self._signals),
                hits_by_signal=tuple(tuple(hits) for hits in outcome.hits_by_signal),
            ),
        )
        return ScreeningResult(
            candidates=outcome.candidates,
            rejections=rejections,
            truncated=outcome.truncated,
            # Issue #192: flattened here rather than at the storage boundary,
            # so the hits persisted for a run are exactly the ones the
            # rejection classifier was shown.
            signal_hits=[hit for hits in outcome.hits_by_signal for hit in hits],
        )

    def _build_candidates(self, data: ScreeningInput) -> _BuildOutcome:
        """Shared filter->signal->rank body for `run()`/`run_with_rejections()`."""
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
            metrics_for_ranking = ranking_metrics(data, symbol)
            if metrics_for_ranking is None:
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
            metrics.update(metrics_for_ranking)
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
        truncated = [
            TruncatedCandidate(
                symbol=symbol,
                rank=rank,
                score=metrics["score"],
                score_breakdown={key: metrics[key] for key in _SCORE_COMPONENT_KEYS},
                execution_state=execution_state,
                execution_distance=execution_distance,
            )
            for rank, (
                symbol,
                _signal_names,
                metrics,
                execution_state,
                execution_distance,
            ) in enumerate(
                classified_rows[self._candidate_limit :],
                start=self._candidate_limit + 1,
            )
        ]
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
        return _BuildOutcome(
            candidates=candidates,
            rankable_symbols=rankable_symbols,
            hits_by_signal=hits_by_signal,
            truncated=truncated,
        )

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

        The strategy-specific components (Issue #251) read metrics only one
        signal produces, so `config.py` rejects a non-zero weight on one whose
        signal the strategy does not run. They still degrade to 0.0 for a
        candidate that legitimately lacks the metric -- a Minervini hit that
        met six conditions without a computable RS percentile, for instance --
        which is the same "weakest reading" that made condition seven fail.
        """
        weights = self._score_weights
        rsi_threshold = self._rsi_threshold
        liquidity_by_symbol = percentile_ranks(
            {symbol: metrics["avg_volume"] for symbol, _names, metrics in rows}
        )
        for symbol, _signal_names, metrics in rows:
            components = _component_values(
                metrics,
                liquidity=liquidity_by_symbol[symbol],
                rsi_threshold=rsi_threshold,
                pivot_proximity_width=self._pivot_proximity_width,
            )
            weighted = {
                f"score_{name}": getattr(weights, name) * components[name]
                for name in ScoreWeights.model_fields
            }
            metrics.update({"score": sum(weighted.values()), **weighted})


def ranking_metrics(data: ScreeningInput, symbol: str) -> dict[str, float] | None:
    """Compute rsi14/atr14/avg_volume/sma50/sma200 from bars, or None if unavailable.

    Computed independently of whichever signals happen to be configured,
    so ranking and report metrics are always available and consistent
    (docs/04_detailed_design.md 2.1 #4). A symbol with any NaN metric
    (e.g. insufficient history) is dropped from the candidate set, as is
    one whose last close is non-positive: `_score_rows` divides by it, so
    a corrupt or placeholder row would otherwise abort the entire run
    rather than costing the one bad symbol.

    Public so a diagnostic can ask "would this symbol have survived the
    pipeline's ranking gate" without re-deriving the NaN rules
    (`filter_matrix.py`).

    Args:
        data: Point-in-time screening input.
        symbol: Universe symbol to compute metrics for.

    Returns:
        The ranking metrics, or `None` when the symbol cannot be ranked.
    """
    window = symbol_window(data.bars, symbol, data.as_of)
    if window is None or window.bar_count < max(
        _RSI_WINDOW, _ATR_WINDOW, _AVG_VOLUME_WINDOW
    ):
        return None

    metrics = {
        "rsi14": window.rsi(_RSI_WINDOW),
        "atr14": window.atr(_ATR_WINDOW),
        "avg_volume": window.mean_volume(_AVG_VOLUME_WINDOW),
        "close": window.close,
        "sma50": window.sma(_SMA_SHORT_WINDOW),
        "sma200": window.sma(_SMA_LONG_WINDOW),
    }
    if any(math.isnan(value) for value in metrics.values()):
        return None
    if metrics["close"] <= 0:
        return None
    return metrics


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


def _state_sort_key(state: str, score: float, symbol: str) -> tuple[int, float, str]:
    """State cap first, then the established score/symbol ordering."""
    return EXECUTION_BUCKETS.index(execution_bucket(state)), -score, symbol


def _clamp01(value: float) -> float:
    """Clamp `value` into `[0, 1]`."""
    return max(0.0, min(1.0, value))


def _component_values(
    metrics: Mapping[str, float],
    *,
    liquidity: float,
    rsi_threshold: float,
    pivot_proximity_width: float,
) -> dict[str, float]:
    """Normalize one row's ranking components to `[0, 1]`, keyed by weight name.

    Every `ScoreWeights` field must appear here; `_SCORE_COMPONENT_KEYS` is
    derived from the same field list, so a missing entry is a `KeyError` in
    `_score_rows` rather than a silently dropped component.

    Args:
        metrics: The row's merged signal and ranking metrics.
        liquidity: The row's `avg_volume` percentile within the candidate set.
        rsi_threshold: The configured pullback threshold `rsi_pullback` scales
            against.
        pivot_proximity_width: `pivot_proximity`'s decay width, in `(0, 1]`
            (Issue #297: derived by the caller from `chase_pivot_pct`, the
            same way `screening/vcp.py` receives it -- never read from
            settings here, to keep this pure layer free of config I/O).

    Returns:
        One value per `ScoreWeights` field, before weighting.
    """
    return {
        "rsi_pullback": _clamp01((rsi_threshold - metrics["rsi14"]) / rsi_threshold),
        "trend_quality": _clamp01(
            (metrics["sma50"] / metrics["sma200"] - 1) / _TREND_QUALITY_NORMALIZATION
        ),
        "liquidity": liquidity,
        "atr_pct": _clamp01(
            (metrics["atr14"] / metrics["close"]) / _ATR_PCT_NORMALIZATION
        ),
        "pivot_proximity": _pivot_proximity(metrics, pivot_proximity_width),
        "rs_percentile": _clamp01(
            metrics.get("minervini_rs_percentile", 0.0) / _RS_PERCENTILE_SCALE
        ),
        "criteria_met": _clamp01(
            metrics.get("minervini_criteria_met", 0.0) / _MINERVINI_CRITERIA_TOTAL
        ),
    }


def _pivot_proximity(metrics: Mapping[str, float], width: float) -> float:
    """Score how close the last close sits to the VCP pivot (Issue #251).

    1.0 exactly at the pivot, falling linearly to 0.0 at `width` away on
    either side. A non-positive or absent pivot scores 0.0 rather than
    dividing by it: `vcp_pivot` is only written by the `vcp_breakout` signal,
    and a strategy weighting this component without that signal is already
    rejected in `config.py`.

    Args:
        metrics: The row's merged signal and ranking metrics.
        width: The decay width (Issue #297: the caller's `chase_pivot_pct`,
            which `config.py` constrains to `> 0.0` so this never divides by
            zero).

    Returns:
        The normalized proximity in `[0, 1]`.
    """
    pivot = metrics.get("vcp_pivot")
    if pivot is None or pivot <= 0.0:
        return 0.0
    distance = abs(metrics["close"] - pivot) / pivot
    return _clamp01(1.0 - distance / width)
