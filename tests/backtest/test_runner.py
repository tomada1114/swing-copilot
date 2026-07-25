"""Smoke test for backtest/runner.py: wires real MarketStore + ScreeningPipeline."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from swing_copilot.backtest.runner import (
    BacktestCostOverrides,
    BacktestDependencies,
    BacktestRequest,
    run_backtest,
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.universe import UniverseMember
from tests.backtest.conftest import bar_row, bars_frame, flat_bars

STRATEGIES_CONFIG = {
    "strategies": {
        "default": {
            "filters_all": [],
            "signals_all": [],
            "candidate_limit": 10,
        }
    }
}


def _with_provider_columns(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {**row, "provider": "test", "fetched_at": pd.Timestamp("2027-01-20", tz="UTC")}
        for row in rows
    ]


@pytest.fixture
def market_store(tmp_path):
    store = MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )
    days = [date(2027, 1, 1 + i) for i in range(20)]
    rows = _with_provider_columns(
        [*flat_bars("SPY", days, 400.0), *flat_bars("AAPL", days, 100.0)]
    )
    store.write_bars(bars_frame(rows))
    return store


def test_run_backtest_produces_a_result_with_benchmark_curve(settings, market_store):
    universe = (
        UniverseMember(
            symbol="AAPL",
            company_name="Apple",
            gics_sector="Information Technology",
            source_symbol="AAPL",
        ),
    )
    deps = BacktestDependencies(
        market_store=market_store,
        universe=universe,
        settings=settings,
        strategies_config=STRATEGIES_CONFIG,
    )
    request = BacktestRequest(
        symbols=["AAPL"],
        start=date(2027, 1, 1),
        end=date(2027, 1, 20),
        initial_cash=100_000.0,
    )

    result = run_backtest(request, deps, BacktestCostOverrides())

    assert result.benchmark_curve
    assert result.final_equity > 0
    assert "survivorship" in result.survivorship_bias_note.lower()


def test_run_backtest_uses_default_overrides_when_none_given(settings, market_store):
    universe = ()
    deps = BacktestDependencies(
        market_store=market_store,
        universe=universe,
        settings=settings,
        strategies_config=STRATEGIES_CONFIG,
    )
    request = BacktestRequest(
        symbols=[], start=date(2027, 1, 1), end=date(2027, 1, 10), initial_cash=50_000.0
    )

    result = run_backtest(request, deps)

    assert result.trades == ()
    assert result.final_equity == pytest.approx(50_000.0)


def test_run_backtest_accepts_slippage_multiplier_override(settings, market_store):
    """P2-09: the override threads through without raising.

    Exact price effects are covered by the engine-level
    TestPessimisticSlippageMultiplier suite.
    """
    universe = (
        UniverseMember(
            symbol="AAPL",
            company_name="Apple",
            gics_sector="Information Technology",
            source_symbol="AAPL",
        ),
    )
    deps = BacktestDependencies(
        market_store=market_store,
        universe=universe,
        settings=settings,
        strategies_config=STRATEGIES_CONFIG,
    )
    request = BacktestRequest(
        symbols=["AAPL"],
        start=date(2027, 1, 1),
        end=date(2027, 1, 20),
        initial_cash=100_000.0,
    )

    result = run_backtest(
        request, deps, BacktestCostOverrides(slippage_multiplier=1.75)
    )

    assert result.final_equity > 0


def test_run_backtest_uses_benchmark_from_settings_when_not_overridden(
    settings, tmp_path
):
    store = MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )
    days = [date(2027, 1, 1 + i) for i in range(3)]
    store.write_bars(bars_frame(_with_provider_columns(flat_bars("QQQ", days, 300.0))))
    custom_settings = settings.model_copy(
        update={"backtest": settings.backtest.model_copy(update={"benchmark": "QQQ"})}
    )
    deps = BacktestDependencies(
        market_store=store,
        universe=(),
        settings=custom_settings,
        strategies_config=STRATEGIES_CONFIG,
    )

    result = run_backtest(BacktestRequest([], days[0], days[-1], 50_000.0), deps)

    assert len(result.benchmark_curve) == 3


def test_run_backtest_loads_pre_start_screening_warmup(settings, tmp_path):
    store = MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(230)]
    rows = []
    for index, day in enumerate(days):
        close = 100.0 + index
        rows.append(bar_row("AAPL", day, (close, close + 1, close - 1, close)))
        rows.append(bar_row("SPY", day, (400.0, 401.0, 399.0, 400.0)))
    store.write_bars(bars_frame(_with_provider_columns(rows)))
    universe = (
        UniverseMember(
            symbol="AAPL",
            company_name="Apple",
            gics_sector="Information Technology",
            source_symbol="AAPL",
        ),
    )
    strategies = {
        "strategies": {
            "warmup": {
                "filters_all": [],
                "signals_all": ["trend_sma"],
                "candidate_limit": 10,
            }
        }
    }
    deps = BacktestDependencies(store, universe, settings, strategies)

    result = run_backtest(
        BacktestRequest(
            ["AAPL"],
            start=days[-3],
            end=days[-1],
            initial_cash=100_000.0,
            strategy_key="warmup",
        ),
        deps,
    )

    assert result.trade_count == 1
    assert result.trades[0].entry_date == days[-2]
