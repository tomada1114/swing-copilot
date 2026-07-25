"""CLI-facing backtest entry point.

Wires the real `MarketStore`/`ScreeningPipeline` into `BacktestEngine` (FR-10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from swing_copilot.backtest.engine import BacktestEngine, BacktestResult
from swing_copilot.screening.base import Candidate, ScreeningInput
from swing_copilot.screening.pipeline import ScreeningPipeline

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.config import Settings
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.universe import UniverseMember

# The longest production screening feature currently uses 325 trading bars
# (VCP). Two calendar years comfortably covers that window across weekends,
# holidays, and ordinary data gaps without allowing a trade before start.
_SCREENING_WARMUP_CALENDAR_DAYS = 730


@dataclass(frozen=True, slots=True)
class BacktestDependencies:
    """Real collaborators `run_backtest` composes together."""

    market_store: MarketStore
    universe: tuple[UniverseMember, ...]
    settings: Settings
    strategies_config: dict[str, Any]  # Any: arbitrary-depth parsed YAML


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    """What to backtest: universe, window, and starting cash."""

    symbols: list[str]
    start: date
    end: date
    initial_cash: float
    strategy_key: str = "default"


@dataclass(frozen=True, slots=True)
class BacktestCostOverrides:
    """Cost/benchmark overrides, defaulting to `settings.yaml`'s own values."""

    commission_pct: float | None = None
    slippage_pct: float | None = None
    benchmark_symbol: str | None = None
    slippage_multiplier: float | None = None
    exit_atr_multiple: float | None = None  # P2-10: sensitivity grid parameter
    max_hold_days: int | None = None  # P2-10: sensitivity grid parameter


def _trading_days(
    market_store: MarketStore, benchmark_symbol: str, start: date, end: date
) -> list[date]:
    bars = market_store.read_bars([benchmark_symbol], start, end, as_of=end)
    return sorted(bars["date"].unique().tolist())


def run_backtest(
    request: BacktestRequest,
    deps: BacktestDependencies,
    overrides: BacktestCostOverrides | None = None,
) -> BacktestResult:
    """Run a deterministic multi-symbol backtest using production screening logic.

    Args:
        request: What to backtest (symbols, window, starting cash).
        deps: Real collaborators (store, universe, settings, strategies).
        overrides: Cost/benchmark overrides; defaults to `settings.backtest`'s
            own commission/slippage and `"SPY"`.

    Returns:
        The full trade log, equity curves, and survivorship bias note.
    """
    overrides = overrides or BacktestCostOverrides()
    start, end = request.start, request.end
    benchmark_symbol = overrides.benchmark_symbol or deps.settings.backtest.benchmark
    commission_pct = (
        overrides.commission_pct
        if overrides.commission_pct is not None
        else deps.settings.backtest.commission_pct
    )
    slippage_pct = (
        overrides.slippage_pct
        if overrides.slippage_pct is not None
        else deps.settings.backtest.slippage_pct
    )
    slippage_multiplier = (
        overrides.slippage_multiplier
        if overrides.slippage_multiplier is not None
        else deps.settings.backtest.slippage_multiplier
    )
    exit_atr_multiple = (
        overrides.exit_atr_multiple
        if overrides.exit_atr_multiple is not None
        else deps.settings.backtest.exit_atr_multiple
    )
    max_hold_days = (
        overrides.max_hold_days
        if overrides.max_hold_days is not None
        else deps.settings.backtest.max_hold_days
    )

    effective_settings = deps.settings.model_copy(
        update={
            "backtest": deps.settings.backtest.model_copy(
                update={
                    "commission_pct": commission_pct,
                    "slippage_pct": slippage_pct,
                    "slippage_multiplier": slippage_multiplier,
                    "exit_atr_multiple": exit_atr_multiple,
                    "max_hold_days": max_hold_days,
                    "benchmark": benchmark_symbol,
                }
            )
        }
    )

    trading_days = _trading_days(deps.market_store, benchmark_symbol, start, end)
    all_symbols = sorted({*request.symbols, benchmark_symbol})
    bars_start = start - timedelta(days=_SCREENING_WARMUP_CALENDAR_DAYS)
    bars = deps.market_store.read_bars(all_symbols, bars_start, end, as_of=end)
    fundamentals = deps.market_store.read_fundamentals(end)
    pipeline = ScreeningPipeline(
        deps.strategies_config,
        deps.market_store,
        effective_settings,
        request.strategy_key,
    )

    def candidates_fn(day: date) -> list[Candidate]:
        point_in_time_bars = bars[bars["date"] <= day]
        # `filed_at` is TIMESTAMPTZ; a bare `date` can't be compared against
        # it directly (pandas raises TypeError). Match
        # `screening/fundamental_filters.py`'s end-of-day-UTC cutoff idiom for
        # an inclusive as-of boundary.
        day_cutoff = datetime.combine(day, time.max, tzinfo=UTC)
        point_in_time_fundamentals = (
            fundamentals[fundamentals["filed_at"] <= day_cutoff]
            if not fundamentals.empty
            else fundamentals
        )
        data = ScreeningInput(
            as_of=day,
            universe=deps.universe,
            fundamentals=point_in_time_fundamentals,
            bars=point_in_time_bars,
        )
        return pipeline.run(data)

    engine = BacktestEngine(effective_settings)
    return engine.run(
        trading_days, bars, candidates_fn, request.initial_cash, benchmark_symbol
    )
