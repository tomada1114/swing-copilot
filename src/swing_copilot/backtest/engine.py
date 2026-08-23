"""Deterministic multi-symbol portfolio simulator (FR-10).

Reuses `risk.position_sizing.calc_position_size` for sizing and
`backtest.exits` (itself built on `screening.indicators.wilder_atr`) for the
trailing stop and exit trigger, per `docs/04_detailed_design.md` 2.1 #5
("reuse the same logic, don't reimplement it for backtesting"). Those exit
rules live in a separate pure module so other consumers apply identical
semantics. Candidate generation itself is injected (`candidates_fn`) rather
than hardcoded to `ScreeningPipeline`, so the fill/stop/hold mechanics here
can be unit-tested in isolation while
`backtest/runner.py` wires in the real production `ScreeningPipeline` for
actual use — both paths share this one engine.

Per-day order of operations (never looks past the current day's own bars):
1. Evaluate entries queued from the previous day's candidates against today's
   OHLC (the default zero-k arm keeps the next-open fill; a positive limit
   multiple can leave the Day order unfilled).
2. Check today's exits (gap/stop/max-hold) for already-open positions.
3. Update trailing stops after today's close (effective from tomorrow).
4. Generate today's candidates and queue them for tomorrow's fill.
5. Record today's closing equity.

Issue #184 changed two things about step 1. Simulator sizing is based on
*equity* (cash plus marked positions) rather than on remaining cash, and the
point-in-time entry gates are consulted through the injected `EntryPolicy` port
(`backtest/policy.py`), which wraps `risk/checks.py` rather than reimplementing
it here. Both the equity basis and the gate inputs are resolved as of the
*signal* day, never the fill day: at tomorrow's open the newest observable
fact is today's close.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from swing_copilot.backtest import metrics
from swing_copilot.backtest.entries import (
    entry_limit_price,
    evaluate_entry_fill,
    initial_stop_price,
)
from swing_copilot.backtest.exits import evaluate_exit, next_trailing_stop
from swing_copilot.backtest.metrics import (
    ENTRY_BLOCK_ALREADY_HELD,
    ENTRY_BLOCK_INSUFFICIENT_CASH,
    ENTRY_BLOCK_INVALID_STOP,
    ENTRY_BLOCK_LIMIT_NOT_REACHED,
    ENTRY_BLOCK_MAX_CONCURRENT,
    ENTRY_BLOCK_MISSING_DATA,
    ENTRY_BLOCK_NOT_CALCULABLE,
    ENTRY_BLOCK_ZERO_SHARES,
    DailyExposure,
)
from swing_copilot.backtest.policy import EntryPolicyRequest
from swing_copilot.risk.position_sizing import calc_position_size
from swing_copilot.screening.indicators import symbol_ohlc_on, symbol_window

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import date

    import pandas as pd

    from swing_copilot.backtest.policy import EntryDecision, EntryPolicy
    from swing_copilot.config import Settings
    from swing_copilot.screening.base import Candidate

SURVIVORSHIP_BIAS_NOTE = (
    "This backtest applies one S&P 500 constituent snapshot to the entire "
    "period. It does not reconstruct day-by-day index membership; when "
    "historical membership is unavailable, the current universe is used. "
    "Removed or delisted symbols may be absent, overstating historical "
    "performance (survivorship bias)."
)


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
    # Sessions the position survived, as the engine counts them: 0 means it
    # closed on the very session it was filled. Defaulted so trade fixtures
    # that only exercise P&L metrics need not restate it; the engine itself
    # always passes the real count.
    days_held: int = 0

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
    # Exit instrumentation: which rule actually closed positions, and how long
    # they were held. Answers whether `max_hold_days` binds at all -- a
    # parameter no amount of tuning can matter for if it never fires.
    exit_reason_counts: tuple[tuple[str, int], ...]
    max_hold_binding_rate: float | None
    holding_days: metrics.HoldingDaysStats | None
    # Entry instrumentation (Issue #184): which gate or constraint stopped a
    # ranked candidate from becoming a position. `entry_block_counts` counts
    # candidate-days; `entry_block_days` counts the distinct sessions on which
    # each reason fired at least once, which is the "how often was the market
    # closed to us?" reading.
    entry_block_counts: tuple[tuple[str, int], ...] = ()
    entry_block_days: tuple[tuple[str, int], ...] = ()
    # Capital deployment (Issue #184): without these, a weak return cannot be
    # split into "picked badly" and "never invested".
    avg_invested_pct: float | None = None
    max_concurrent_reached: int = 0
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
    entry_block_counts: defaultdict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    entry_block_days: defaultdict[str, set[date]] = field(
        default_factory=lambda: defaultdict(set)
    )
    daily_exposure: list[DailyExposure] = field(default_factory=list)

    def record_block(self, day: date, reason: str) -> None:
        """Record that `reason` stopped one candidate from filling on `day`."""
        self.entry_block_counts[reason] += 1
        self.entry_block_days[reason].add(day)


@dataclass(frozen=True, slots=True)
class _SignalDay:
    """The day a batch of pending candidates was screened on, with its equity.

    The two travel together because both are as-of the *same* close: the day
    the candidates were ranked is the day whose equity sizes them, and at
    tomorrow's open no newer fact is observable. Carrying the equity here is
    also what lets the fill step skip re-marking the whole book (Issue #244);
    see `_fill_pending_entries` for why that is an identity.
    """

    day: date
    equity: float


@dataclass(frozen=True, slots=True)
class _FillContext:
    """Everything one day's entry fills are evaluated against."""

    day: date
    #: The close used to calculate the candidate's planned limit. This is the
    #: signal day, never the fill day, so the gate cannot look ahead.
    signal_day: date
    bars: pd.DataFrame
    #: Equity as of the signal day's close — the sizing basis for every fill
    #: attempted on `day`, so a candidate's size never depends on which of the
    #: day's other candidates happened to fill first.
    equity_basis: float
    state: _SimState


@dataclass(frozen=True, slots=True)
class _EntryExecution:
    """Signal-day bases and the actual price for one filled entry.

    `sizing_price` is the worst-case anchor `_commit_entry` sizes against. In
    the Day-limit arm it is `max(limit_price, execution_price)`, since the
    fill's own slippage can push the execution price fractionally above the
    limit that gated it; in the no-real-limit compatibility arm it is
    `limit_price` (== the signal close) as-is, since no order was ever capped
    there and treating an overnight gap as "slippage past the limit" would
    defeat the point of anchoring to the plan the reader saw.
    """

    signal_close: float
    limit_price: float
    execution_price: float
    sizing_price: float


@dataclass(frozen=True, slots=True)
class _ResultInputs:
    """Everything `_build_result` renders, bundled to keep the arity sane."""

    trades: tuple[Trade, ...]
    equity_curve: tuple[tuple[date, float], ...]
    benchmark_curve: tuple[tuple[date, float], ...]
    final_equity: float
    benchmark_final_equity: float
    exposure: tuple[DailyExposure, ...] = ()
    entry_block_counts: Mapping[str, int] = field(default_factory=dict)
    entry_block_days: Mapping[str, set[date]] = field(default_factory=dict)


def _bar(bars: pd.DataFrame, symbol: str, day: date) -> dict[str, float] | None:
    """Return `symbol`'s bar dated exactly `day`, or `None`.

    Issue #244: this used to mask the whole frame twice per call
    (`bars["symbol"] == symbol` and `bars["date"] == day`). `date` is an object
    column, so the second mask is an element-wise Python comparison over every
    row in the frame -- ~33ms per call on a 508-symbol, 1652-session frame, of
    which the engine makes tens of thousands. `symbol_ohlc_on` answers from the
    per-symbol index already cached against this frame (Issue #214), returning
    the same row: see `_SymbolIndex.ohlc_on` for why the duplicate-row tie-break
    (`iloc[0]`, the *first* matching row) is preserved.
    """
    return symbol_ohlc_on(bars, symbol, day)


def _mark_to_market(state: _SimState, bars: pd.DataFrame, day: date) -> float:
    """Value every open position at its newest close on or before `day`.

    Reads `SymbolWindow.close` rather than `_latest_bar`, which is the same
    row: only the close is priced here, and this is the engine's most-called
    path (once per open position per day, twice over on days that fill). Going
    through the OHLC dict would build a four-key dict per position per day and
    force `open`/`high`/`low` to be materialized as float64 arrays for every
    held symbol, roughly tripling the raw-price memory each cached
    `_SymbolIndex` pins, to read one of the four values.
    """
    total = 0.0
    for position in state.open_positions.values():
        window = symbol_window(bars, position.symbol, day)
        if window is not None:
            total += position.shares * window.close
    return total


def _latest_bar(
    bars: pd.DataFrame, symbol: str, as_of: date
) -> dict[str, float] | None:
    """Return the newest available bar on or before an inclusive cutoff.

    Issue #244: the indexed lookup replaces the same full-frame masking `_bar`
    describes, plus a `sort_values("date")` of the surviving rows. `.ohlc` reads
    the last row of the `as_of` prefix; see that property for why this is the
    same row the replaced scan reached, and for the one case (a frame with a
    duplicated newest row) where the old scan had no defined answer at all.
    """
    window = symbol_window(bars, symbol, as_of)
    return None if window is None else window.ohlc


class BacktestEngine:
    """Runs the fixed fill/stop/hold rules over injected candidates and bars."""

    def __init__(
        self, settings: Settings, entry_policy: EntryPolicy | None = None
    ) -> None:
        """Create the engine.

        Args:
            settings: Loaded application settings (`backtest.*`, `risk.*`).
            entry_policy: Production entry gates to consult before each fill
                (`backtest/policy.py`). `None` runs the deterministic
                screening/sizing path alone, which is the `--policy none` arm.
        """
        self._entry_policy = entry_policy
        self._backtest_config = settings.backtest
        self._trade_plan = settings.trade_plan
        # These values are nominal simulation inputs, not production advice.
        self._max_concurrent_positions = settings.backtest.max_concurrent_positions
        self._max_position_pct = settings.backtest.sim_position_cap_pct
        self._max_trade_risk_pct = settings.backtest.sim_trade_risk_pct
        # Issue #194: the trailing stop's ATR period is configuration, not a
        # constant. It governs the *exit* side only; the entry stop keeps using
        # the screening metric `atr14` so the simulator sizes a position with
        # exactly the number `risk/checks.py` would have used in production.
        self._exit_atr_period = self._trade_plan.exit_atr_period
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
            return self._build_result(
                _ResultInputs((), (), (), initial_cash, initial_cash)
            )

        state = _SimState(cash=initial_cash, benchmark_cash=initial_cash)
        pending_entries: list[Candidate] = []
        equity_curve: list[tuple[date, float]] = []
        benchmark_curve: list[tuple[date, float]] = []
        # The previous iteration's day and its closing equity; `None` until the
        # first close is recorded, which is also when nothing can be pending.
        signal: _SignalDay | None = None

        for day in trading_days:
            self._fill_pending_entries(day, signal, bars, pending_entries, state)
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

            invested = _mark_to_market(state, bars, day)
            equity = state.cash + invested
            equity_curve.append((day, equity))
            state.daily_exposure.append(
                DailyExposure(
                    day=day,
                    invested_usd=invested,
                    equity_usd=equity,
                    open_position_count=len(state.open_positions),
                )
            )
            # The candidates just generated were screened as of this close, and
            # `equity` is this close's equity: one value, recorded once, read
            # again tomorrow morning as the sizing basis.
            signal = _SignalDay(day=day, equity=equity)
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
            _ResultInputs(
                trades=tuple(state.closed_trades),
                equity_curve=tuple(equity_curve),
                benchmark_curve=tuple(benchmark_curve),
                final_equity=equity_curve[-1][1] if equity_curve else initial_cash,
                benchmark_final_equity=(
                    benchmark_curve[-1][1] if benchmark_curve else initial_cash
                ),
                exposure=tuple(state.daily_exposure),
                entry_block_counts=dict(state.entry_block_counts),
                entry_block_days=dict(state.entry_block_days),
            )
        )

    def _build_result(self, inputs: _ResultInputs) -> BacktestResult:
        trades = inputs.trades
        equity_curve = inputs.equity_curve
        win_rate = metrics.compute_win_rate(trades)
        max_drawdown_pct = metrics.compute_max_drawdown_pct(equity_curve)
        block_counts = metrics.entry_block_breakdown(inputs.entry_block_counts)
        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            benchmark_curve=inputs.benchmark_curve,
            final_equity=inputs.final_equity,
            benchmark_final_equity=inputs.benchmark_final_equity,
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
            exit_reason_counts=tuple(metrics.exit_reason_breakdown(trades).items()),
            max_hold_binding_rate=metrics.max_hold_binding_rate(trades),
            holding_days=metrics.holding_days_stats(trades),
            entry_block_counts=tuple(block_counts.items()),
            entry_block_days=tuple(
                (reason, len(inputs.entry_block_days.get(reason, ())))
                for reason in block_counts
            ),
            avg_invested_pct=metrics.compute_avg_invested_pct(inputs.exposure),
            max_concurrent_reached=metrics.compute_max_concurrent_reached(
                inputs.exposure
            ),
        )

    def _fill_pending_entries(
        self,
        day: date,
        signal: _SignalDay | None,
        bars: pd.DataFrame,
        pending_entries: list[Candidate],
        state: _SimState,
    ) -> None:
        """Fill yesterday's candidates at today's open, subject to the gates.

        Args:
            day: The fill day.
            signal: The day `pending_entries` were screened on and that day's
                closing equity — the as-of basis for both the sizing equity and
                every gate input. `None` only on the very first simulated day,
                when nothing can be pending yet.
            bars: The whole tidy OHLCV frame.
            pending_entries: Yesterday's ranked candidates.
            state: Mutable simulation state.
        """
        if not pending_entries or signal is None:
            return
        considered: list[Candidate] = []
        for candidate in sorted(pending_entries, key=lambda c: c.rank):
            if candidate.symbol in state.open_positions:
                state.record_block(day, ENTRY_BLOCK_ALREADY_HELD)
            else:
                considered.append(candidate)
        if not considered:
            return

        # One basis for the whole day, marked at the signal day's close: the
        # fill happens at today's open, where today's close is still unknown.
        #
        # Issue #244: `signal.equity` is read here instead of recomputing
        # `state.cash + _mark_to_market(state, bars, signal.day)`. That is the
        # *same number*, not an approximation of it, and the reason is the loop
        # order. This step runs at the top of the day loop, so `signal.day` is
        # by construction the immediately preceding iteration's `day`; at the
        # end of that iteration `run` evaluated exactly that expression, wrote
        # it to the equity curve, and captured it here. Between that statement
        # and this one the loop only appends to the exposure and benchmark
        # curves, so neither `state.cash` nor `state.open_positions` has
        # changed, and `bars` is immutable for the whole run -- a recomputation
        # would therefore mark the same positions at the same closes and sum
        # them over the same dict in the same order, reproducing the carried
        # float bit for bit. Recomputing it cost one full mark-to-market per
        # open position per day, on the engine's hottest path.
        # `TestEquityBasis` pins the identity.
        context = _FillContext(
            day=day,
            signal_day=signal.day,
            bars=bars,
            equity_basis=signal.equity,
            state=state,
        )
        decisions = self._policy_decisions(signal.day, considered)

        for index, candidate in enumerate(considered):
            if len(state.open_positions) >= self._max_concurrent_positions:
                for _blocked in considered[index:]:
                    state.record_block(day, ENTRY_BLOCK_MAX_CONCURRENT)
                break
            reason = self._try_fill(context, candidate, decisions.get(candidate.symbol))
            if reason is not None:
                state.record_block(day, reason)

    def _policy_decisions(
        self,
        signal_day: date,
        candidates: list[Candidate],
    ) -> Mapping[str, EntryDecision]:
        """Consult the injected production gates once for the whole day.

        The batch call keeps the policy boundary identical to production and
        lets the policy evaluate every candidate against the same signal day.
        """
        if self._entry_policy is None:
            return {}
        return self._entry_policy.decide(
            EntryPolicyRequest(
                as_of=signal_day,
                candidates=tuple(candidates),
            )
        )

    def _try_fill(
        self,
        context: _FillContext,
        candidate: Candidate,
        decision: EntryDecision | None,
    ) -> str | None:
        """Attempt one entry; return the block reason, or `None` on a fill."""
        bar = _bar(context.bars, candidate.symbol, context.day)
        signal_bar = _latest_bar(context.bars, candidate.symbol, context.signal_day)
        atr14 = candidate.metrics.get("atr14")
        if bar is None or signal_bar is None or atr14 is None:
            return ENTRY_BLOCK_MISSING_DATA
        if decision is not None and not decision.is_allowed:
            return decision.reject_reason or ENTRY_BLOCK_NOT_CALCULABLE

        execution = self._entry_execution_price(bar, signal_bar, atr14)
        if execution is None:
            return ENTRY_BLOCK_LIMIT_NOT_REACHED
        return self._commit_entry(
            context, candidate, execution, atr14, self._max_trade_risk_pct
        )

    def _entry_execution_price(
        self, bar: dict[str, float], signal_bar: dict[str, float], atr14: float
    ) -> _EntryExecution | None:
        """Resolve signal bases and the raw price under the configured mode."""
        signal_close = signal_bar["close"]
        limit_price = entry_limit_price(
            signal_close, atr14, self._trade_plan.entry_limit_atr_multiple
        )
        if (
            self._backtest_config.entry == "next_open"
            and self._trade_plan.entry_limit_atr_multiple == 0.0
        ):
            # Zero is the compatibility/default arm: it measures the
            # historical next-open model, while a positive multiple opts into
            # the Day-limit gate and its not-reached instrumentation. No real
            # limit order is placed here, so the plan's own anchor stands as
            # the sizing basis even though the open can gap past it.
            execution_price = bar["open"] * (1 + self._slippage_pct)
            sizing_price = limit_price
        else:
            fill_price = evaluate_entry_fill(
                open_price=bar["open"],
                low=bar["low"],
                limit_price=limit_price,
                slippage_pct=self._slippage_pct,
            )
            if fill_price is None:
                return None
            execution_price = fill_price
            sizing_price = max(limit_price, execution_price)
        return _EntryExecution(
            signal_close=signal_close,
            limit_price=limit_price,
            execution_price=execution_price,
            sizing_price=sizing_price,
        )

    def _commit_entry(
        self,
        context: _FillContext,
        candidate: Candidate,
        execution: _EntryExecution,
        atr14: float,
        risk_pct: float,
    ) -> str | None:
        """Size and commit a resolved entry, returning mechanical blocks."""
        state = context.state
        if (
            not math.isfinite(execution.execution_price)
            or execution.execution_price <= 0
        ):
            # A non-positive fill (malformed OHLC) would otherwise size fine
            # off `limit_price` and then post negative cash below.
            return ENTRY_BLOCK_INVALID_STOP
        stop_price = initial_stop_price(
            execution.signal_close, atr14, self._trade_plan.exit_atr_multiple
        )
        try:
            shares = calc_position_size(
                context.equity_basis,
                execution.sizing_price,
                stop_price,
                self._max_position_pct,
                risk_pct,
            ).shares
        except ValueError:
            return ENTRY_BLOCK_INVALID_STOP
        if shares <= 0:
            return ENTRY_BLOCK_ZERO_SHARES

        entry_notional = shares * execution.execution_price
        entry_commission = entry_notional * self._backtest_config.commission_pct
        cost = entry_notional + entry_commission
        if cost > state.cash:
            return ENTRY_BLOCK_INSUFFICIENT_CASH

        state.cash -= cost
        state.open_positions[candidate.symbol] = _OpenPosition(
            symbol=candidate.symbol,
            entry_date=context.day,
            entry_price=execution.execution_price,
            shares=shares,
            stop_price=stop_price,
            initial_stop_price=stop_price,
            entry_commission_usd=entry_commission,
        )
        return None

    def _process_exits(self, day: date, bars: pd.DataFrame, state: _SimState) -> None:
        for position in list(state.open_positions.values()):
            bar = _bar(bars, position.symbol, day)
            if bar is None:
                continue

            decision = evaluate_exit(
                open_price=bar["open"],
                low=bar["low"],
                close=bar["close"],
                stop_price=position.stop_price,
                days_held=position.days_held,
                max_hold_days=self._trade_plan.max_hold_days,
            )
            if decision is not None:
                self._settle_exit(
                    state, position, day, decision.exit_price, decision.reason
                )
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
                days_held=position.days_held,
            )
        )
        del state.open_positions[position.symbol]

    def _update_trailing_stops(
        self, day: date, bars: pd.DataFrame, state: _SimState
    ) -> None:
        for position in state.open_positions.values():
            bar = _bar(bars, position.symbol, day)
            # Issue #224: `atr_as_of` re-smooths the symbol's whole history on
            # every simulated day to keep one point, so walking a position
            # forward costs O(days x rows). The window reads the same Wilder
            # ATR from a column computed once per symbol and cached on `bars`
            # (Issue #214), and reads it at the `as_of` row, so the value is
            # bit-identical to `atr_as_of(bars, symbol, day, period)` -- Wilder
            # smoothing is causal, so a row's value never depends on a later
            # one. `tests/backtest/test_exits.py` pins that equivalence.
            window = symbol_window(bars, position.symbol, day)
            atr = math.nan if window is None else window.atr(self._exit_atr_period)
            if bar is None or math.isnan(atr):
                continue
            position.stop_price = next_trailing_stop(
                current_stop=position.stop_price,
                close=bar["close"],
                atr=atr,
                exit_atr_multiple=self._trade_plan.exit_atr_multiple,
            )

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
