"""Deterministic multi-symbol portfolio simulator (FR-10).

Reuses `risk.position_sizing.calc_position_size` for sizing and
`screening.indicators.wilder_atr` for the trailing stop, per
`docs/04_detailed_design.md` 2.1 #5 ("reuse the same logic, don't
reimplement it for backtesting"). Candidate generation itself is injected
(`candidates_fn`) rather than hardcoded to `ScreeningPipeline`, so the fill/
stop/hold mechanics here can be unit-tested in isolation while
`backtest/runner.py` wires in the real production `ScreeningPipeline` for
actual use — both paths share this one engine.

Per-day order of operations (never looks past the current day's own bars):
1. Fill entries queued from the previous day's candidates, at today's open.
2. Check today's exits (gap/stop/max-hold) for already-open positions.
3. Update trailing stops after today's close (effective from tomorrow).
4. Generate today's candidates and queue them for tomorrow's fill.
5. Record today's closing equity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from swing_copilot.backtest import metrics
from swing_copilot.risk.position_sizing import calc_position_size
from swing_copilot.screening.indicators import symbol_bars, wilder_atr

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    import pandas as pd

    from swing_copilot.config import Settings
    from swing_copilot.screening.base import Candidate

SURVIVORSHIP_BIAS_NOTE = (
    "This backtest applies one S&P 500 constituent snapshot to the entire "
    "period. It does not reconstruct day-by-day index membership; when "
    "historical membership is unavailable, the current universe is used. "
    "Removed or delisted symbols may be absent, overstating historical "
    "performance (survivorship bias)."
)
_ATR_PERIOD = 14


@dataclass(frozen=True, slots=True)
class Trade:
    """One closed round-trip trade."""

    symbol: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    shares: int
    exit_reason: str  # "stop" | "max_hold" | "end_of_backtest"
    # Stop price at entry fill time, before any later trailing-stop update
    # (P2-07's R-multiple is against the risk actually taken at entry, not
    # today's trailed stop). None only if never recorded.
    initial_stop_price: float | None = None
    # Total round-trip commission in USD. Slippage is already reflected in
    # entry_price/exit_price; commission is tracked separately so every
    # trade-level metric reconciles to the cash ledger.
    commission_usd: float = 0.0

    @property
    def pnl(self) -> float:
        """Realized profit/loss after both entry and exit commission, in USD."""
        return (self.exit_price - self.entry_price) * self.shares - self.commission_usd


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Full backtest outcome."""

    trades: tuple[Trade, ...]
    equity_curve: tuple[tuple[date, float], ...]
    benchmark_curve: tuple[tuple[date, float], ...]
    final_equity: float
    benchmark_final_equity: float
    trade_count: int
    sharpe: float | None
    max_drawdown_pct: float
    win_rate: float | None
    profit_factor: float | None
    expectancy_per_trade: float | None
    avg_r_multiple: float | None
    warnings: tuple[str, ...]
    survivorship_bias_note: str = SURVIVORSHIP_BIAS_NOTE


@dataclass(slots=True)
class _OpenPosition:
    symbol: str
    entry_date: date
    entry_price: float
    shares: int
    stop_price: float
    initial_stop_price: float
    entry_commission_usd: float
    days_held: int = 0


@dataclass(slots=True)
class _SimState:
    """Mutable simulation state threaded through one engine run."""

    cash: float
    open_positions: dict[str, _OpenPosition] = field(default_factory=dict)
    closed_trades: list[Trade] = field(default_factory=list)
    benchmark_shares: int = 0
    benchmark_cash: float = 0.0
    benchmark_initialized: bool = False


def _bar(bars: pd.DataFrame, symbol: str, day: date) -> dict[str, float] | None:
    rows = bars[(bars["symbol"] == symbol) & (bars["date"] == day)]
    if rows.empty:
        return None
    row = rows.iloc[0]
    return {
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    }


def _latest_bar(
    bars: pd.DataFrame, symbol: str, as_of: date
) -> dict[str, float] | None:
    """Return the newest available bar on or before an inclusive cutoff."""
    rows = bars[(bars["symbol"] == symbol) & (bars["date"] <= as_of)]
    if rows.empty:
        return None
    row = rows.sort_values("date").iloc[-1]
    return {
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    }


def _atr14_as_of(bars: pd.DataFrame, symbol: str, as_of: date) -> float | None:
    series = symbol_bars(bars, symbol, as_of)
    if series is None or len(series) < _ATR_PERIOD:
        return None
    atr = wilder_atr(series["high"], series["low"], series["close"], _ATR_PERIOD).iloc[
        -1
    ]
    return None if math.isnan(atr) else float(atr)


class BacktestEngine:
    """Runs the fixed fill/stop/hold rules over injected candidates and bars."""

    def __init__(self, settings: Settings) -> None:
        """Create the engine.

        Args:
            settings: Loaded application settings (`backtest.*`, `risk.*`).
        """
        self._backtest_config = settings.backtest
        self._max_concurrent_positions = max(1, int(1 / settings.risk.max_position_pct))
        self._max_position_pct = settings.risk.max_position_pct
        self._max_trade_risk_pct = settings.risk.max_trade_risk_pct
        # P2-09: applied on both entry and exit (incl. forced liquidation) --
        # a single computed rate so every call site stays in sync.
        self._slippage_pct = (
            settings.backtest.slippage_pct * settings.backtest.slippage_multiplier
        )

    def run(
        self,
        trading_days: list[date],
        bars: pd.DataFrame,
        candidates_fn: Callable[[date], list[Candidate]],
        initial_cash: float,
        benchmark_symbol: str = "SPY",
    ) -> BacktestResult:
        """Run the simulation over `trading_days`.

        Args:
            trading_days: Ordered market days to simulate.
            bars: Tidy OHLCV for every symbol needed, across the whole
                window (the engine only ever reads up to the current
                simulated day; no look-ahead occurs regardless of how much
                data is present).
            candidates_fn: Returns ranked candidates as of a given day's
                close (e.g. `ScreeningPipeline.run(...)`, or a test fake).
            initial_cash: Starting cash in USD.
            benchmark_symbol: Buy-and-hold comparison symbol.

        Returns:
            The full trade log, equity curves, and survivorship bias note.
        """
        if not trading_days:
            return self._build_result((), (), (), initial_cash, initial_cash)

        state = _SimState(cash=initial_cash, benchmark_cash=initial_cash)
        pending_entries: list[Candidate] = []
        equity_curve: list[tuple[date, float]] = []
        benchmark_curve: list[tuple[date, float]] = []

        for day in trading_days:
            self._fill_pending_entries(day, bars, pending_entries, state)
            self._process_exits(day, bars, state)
            self._update_trailing_stops(day, bars, state)
            pending_entries = candidates_fn(day)

            if not state.benchmark_initialized:
                benchmark_bar = _bar(bars, benchmark_symbol, day)
                if benchmark_bar is not None:
                    state.benchmark_shares = int(initial_cash / benchmark_bar["close"])
                    state.benchmark_cash -= (
                        state.benchmark_shares * benchmark_bar["close"]
                    )
                    state.benchmark_initialized = True

            equity_curve.append(
                (day, state.cash + self._mark_to_market(state, bars, day))
            )
            benchmark_bar = _latest_bar(bars, benchmark_symbol, day)
            benchmark_curve.append(
                (
                    day,
                    state.benchmark_cash
                    + state.benchmark_shares * benchmark_bar["close"]
                    if benchmark_bar is not None
                    else initial_cash,
                )
            )

        self._liquidate_remaining(trading_days[-1], bars, state)
        equity_curve[-1] = (trading_days[-1], state.cash)

        return self._build_result(
            tuple(state.closed_trades),
            tuple(equity_curve),
            tuple(benchmark_curve),
            equity_curve[-1][1] if equity_curve else initial_cash,
            benchmark_curve[-1][1] if benchmark_curve else initial_cash,
        )

    def _build_result(
        self,
        trades: tuple[Trade, ...],
        equity_curve: tuple[tuple[date, float], ...],
        benchmark_curve: tuple[tuple[date, float], ...],
        final_equity: float,
        benchmark_final_equity: float,
    ) -> BacktestResult:
        win_rate = metrics.compute_win_rate(trades)
        max_drawdown_pct = metrics.compute_max_drawdown_pct(equity_curve)
        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            benchmark_curve=benchmark_curve,
            final_equity=final_equity,
            benchmark_final_equity=benchmark_final_equity,
            trade_count=len(trades),
            sharpe=metrics.compute_sharpe(equity_curve),
            max_drawdown_pct=max_drawdown_pct,
            win_rate=win_rate,
            profit_factor=metrics.compute_profit_factor(trades),
            expectancy_per_trade=metrics.compute_expectancy_per_trade(trades),
            avg_r_multiple=metrics.compute_avg_r_multiple(trades),
            warnings=metrics.compute_reliability_warnings(
                len(trades), win_rate, max_drawdown_pct, self._backtest_config
            ),
        )

    def _fill_pending_entries(
        self,
        day: date,
        bars: pd.DataFrame,
        pending_entries: list[Candidate],
        state: _SimState,
    ) -> None:
        for candidate in sorted(pending_entries, key=lambda c: c.rank):
            if candidate.symbol in state.open_positions:
                continue
            if len(state.open_positions) >= self._max_concurrent_positions:
                break
            bar = _bar(bars, candidate.symbol, day)
            atr14 = candidate.metrics.get("atr14")
            if bar is None or atr14 is None:
                continue

            entry_price = bar["open"] * (1 + self._slippage_pct)
            stop_price = entry_price - self._backtest_config.exit_atr_multiple * atr14
            try:
                shares = calc_position_size(
                    state.cash,
                    entry_price,
                    stop_price,
                    self._max_position_pct,
                    self._max_trade_risk_pct,
                ).shares
            except ValueError:
                continue
            if shares <= 0:
                continue

            entry_notional = shares * entry_price
            entry_commission = entry_notional * self._backtest_config.commission_pct
            cost = entry_notional + entry_commission
            if cost > state.cash:
                continue

            state.cash -= cost
            state.open_positions[candidate.symbol] = _OpenPosition(
                symbol=candidate.symbol,
                entry_date=day,
                entry_price=entry_price,
                shares=shares,
                stop_price=stop_price,
                initial_stop_price=stop_price,
                entry_commission_usd=entry_commission,
            )

    def _process_exits(self, day: date, bars: pd.DataFrame, state: _SimState) -> None:
        for symbol, position in list(state.open_positions.items()):
            bar = _bar(bars, symbol, day)
            if bar is None:
                continue

            exit_price: float | None = None
            exit_reason = ""
            if bar["open"] <= position.stop_price:
                exit_price, exit_reason = bar["open"], "stop"
            elif bar["low"] <= position.stop_price:
                exit_price, exit_reason = position.stop_price, "stop"
            elif position.days_held + 1 >= self._backtest_config.max_hold_days:
                exit_price, exit_reason = bar["close"], "max_hold"

            if exit_price is not None:
                self._settle_exit(state, position, day, exit_price, exit_reason)
            else:
                position.days_held += 1

    def _settle_exit(
        self,
        state: _SimState,
        position: _OpenPosition,
        exit_date: date,
        exit_price: float,
        exit_reason: str,
    ) -> None:
        execution_price = exit_price * (1 - self._slippage_pct)
        exit_notional = position.shares * execution_price
        exit_commission = exit_notional * self._backtest_config.commission_pct
        proceeds = exit_notional - exit_commission
        state.cash += proceeds
        state.closed_trades.append(
            Trade(
                symbol=position.symbol,
                entry_date=position.entry_date,
                entry_price=position.entry_price,
                exit_date=exit_date,
                exit_price=execution_price,
                shares=position.shares,
                exit_reason=exit_reason,
                initial_stop_price=position.initial_stop_price,
                commission_usd=position.entry_commission_usd + exit_commission,
            )
        )
        del state.open_positions[position.symbol]

    def _update_trailing_stops(
        self, day: date, bars: pd.DataFrame, state: _SimState
    ) -> None:
        for position in state.open_positions.values():
            bar = _bar(bars, position.symbol, day)
            atr14 = _atr14_as_of(bars, position.symbol, day)
            if bar is None or atr14 is None:
                continue
            candidate_stop = (
                bar["close"] - self._backtest_config.exit_atr_multiple * atr14
            )
            position.stop_price = max(position.stop_price, candidate_stop)

    def _mark_to_market(self, state: _SimState, bars: pd.DataFrame, day: date) -> float:
        total = 0.0
        for position in state.open_positions.values():
            bar = _latest_bar(bars, position.symbol, day)
            if bar is not None:
                total += position.shares * bar["close"]
        return total

    def _liquidate_remaining(
        self, final_day: date, bars: pd.DataFrame, state: _SimState
    ) -> None:
        for position in list(state.open_positions.values()):
            bar = _latest_bar(bars, position.symbol, final_day)
            if bar is None:
                continue
            self._settle_exit(
                state, position, final_day, bar["close"], "end_of_backtest"
            )
