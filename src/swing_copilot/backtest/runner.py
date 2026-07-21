"""CLI-facing backtest entry point.

Wires the real `MarketStore`/`ScreeningPipeline` into `BacktestEngine` (FR-10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from swing_copilot.backtest.engine import BacktestEngine, BacktestResult
from swing_copilot.screening.base import Candidate, ScreeningInput
from swing_copilot.screening.pipeline import ScreeningPipeline

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd

    from swing_copilot.config import Settings
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.universe import UniverseMember


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


@dataclass(frozen=True, slots=True)
class BacktestCostOverrides:
    """Cost/benchmark overrides, defaulting to `settings.yaml`'s own values."""

    commission_pct: float = 0.001
    slippage_pct: float = 0.001
    benchmark_symbol: str = "SPY"


def _trading_days(
    market_store: MarketStore, benchmark_symbol: str, start: date, end: date
) -> list[date]:
    bars = market_store.read_bars([benchmark_symbol], start, end, as_of=end)
    return sorted(bars["date"].unique().tolist())


def _read_fundamentals(market_store: MarketStore, as_of: date) -> pd.DataFrame:
    with market_store.get_connection() as conn:
        return conn.execute(
            "SELECT * FROM fundamentals WHERE filed_at <= ?", [as_of]
        ).df()


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
    benchmark_symbol = overrides.benchmark_symbol

    effective_settings = deps.settings.model_copy(
        update={
            "backtest": deps.settings.backtest.model_copy(
                update={
                    "commission_pct": overrides.commission_pct,
                    "slippage_pct": overrides.slippage_pct,
                    "benchmark": benchmark_symbol,
                }
            )
        }
    )

    trading_days = _trading_days(deps.market_store, benchmark_symbol, start, end)
    all_symbols = sorted({*request.symbols, benchmark_symbol})
    bars = deps.market_store.read_bars(all_symbols, start, end, as_of=end)
    fundamentals = _read_fundamentals(deps.market_store, end)
    pipeline = ScreeningPipeline(
        deps.strategies_config, deps.market_store, effective_settings
    )

    def candidates_fn(day: date) -> list[Candidate]:
        point_in_time_bars = bars[bars["date"] <= day]
        point_in_time_fundamentals = (
            fundamentals[fundamentals["filed_at"] <= day]
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
