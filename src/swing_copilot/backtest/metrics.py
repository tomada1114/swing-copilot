"""Risk-adjusted metric calculations for `BacktestResult` (P2-07, roadmap §5 P2-07).

Pure functions over an already-produced trade log and equity curve, kept
separate from `engine.py`'s simulation loop so each metric is independently
unit-testable against hand-calculated fixtures.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

    from swing_copilot.backtest.engine import Trade
    from swing_copilot.config import BacktestConfig

_TRADING_DAYS_PER_YEAR = 252
# roadmap §5 P2-07: "日次リターンが1件以下ならNone" -- need at least 2 daily
# returns (i.e. 3 equity points) for a defined sample standard deviation.
_MIN_RETURNS_FOR_SHARPE = 2

MAX_HOLD_REASON = "max_hold"
# Every reason `engine._settle_exit` can stamp on a Trade. Kept here, next to
# the breakdown that reports it, so a new exit rule fails this list loudly
# instead of silently vanishing from the report.
EXIT_REASONS = ("stop", MAX_HOLD_REASON, "end_of_backtest")

LOOKAHEAD_SUSPICION_WARNING = (
    "ルックアヘッド疑い（勝率が極端に高い、または最大ドローダウンが極小）"
)

# Issue #184: why a ranked candidate did not become a position. The first six
# labels are the production gates `backtest/policy.py` wraps (never
# reimplements); the rest are the engine's own mechanical constraints. Kept
# here, next to the breakdown that reports them, for the same reason as
# `EXIT_REASONS`: a newly added block reason must fail this list loudly rather
# than silently vanish from the report.
ENTRY_BLOCK_REGIME = "regime"
ENTRY_BLOCK_CIRCUIT_BREAKER = "circuit_breaker"
ENTRY_BLOCK_PORTFOLIO_HEAT = "portfolio_heat"
ENTRY_BLOCK_EARNINGS = "earnings"
ENTRY_BLOCK_SECTOR = "sector"
ENTRY_BLOCK_NOT_CALCULABLE = "not_calculable"
ENTRY_BLOCK_MAX_CONCURRENT = "max_concurrent"
ENTRY_BLOCK_ALREADY_HELD = "already_held"
ENTRY_BLOCK_MISSING_DATA = "missing_data"
ENTRY_BLOCK_INVALID_STOP = "invalid_stop"
ENTRY_BLOCK_ZERO_SHARES = "zero_shares"
ENTRY_BLOCK_INSUFFICIENT_CASH = "insufficient_cash"

ENTRY_BLOCK_REASONS = (
    ENTRY_BLOCK_REGIME,
    ENTRY_BLOCK_CIRCUIT_BREAKER,
    ENTRY_BLOCK_PORTFOLIO_HEAT,
    ENTRY_BLOCK_EARNINGS,
    ENTRY_BLOCK_SECTOR,
    ENTRY_BLOCK_NOT_CALCULABLE,
    ENTRY_BLOCK_MAX_CONCURRENT,
    ENTRY_BLOCK_ALREADY_HELD,
    ENTRY_BLOCK_MISSING_DATA,
    ENTRY_BLOCK_INVALID_STOP,
    ENTRY_BLOCK_ZERO_SHARES,
    ENTRY_BLOCK_INSUFFICIENT_CASH,
)


def compute_sharpe(equity_curve: tuple[tuple[date, float], ...]) -> float | None:
    """Annualized Sharpe ratio (rf=0, sqrt(252)) from daily equity returns.

    Returns:
        None when there's at most one daily return, or when returns have
        zero variance (an undefined ratio; avoids division by zero).
    """
    values = [equity for _, equity in equity_curve]
    returns = [
        values[i] / values[i - 1] - 1
        for i in range(1, len(values))
        if values[i - 1] != 0
    ]
    if len(returns) < _MIN_RETURNS_FOR_SHARPE:
        return None
    stdev = statistics.stdev(returns)
    if stdev == 0:
        return None
    return statistics.fmean(returns) / stdev * math.sqrt(_TRADING_DAYS_PER_YEAR)


def compute_max_drawdown_pct(equity_curve: tuple[tuple[date, float], ...]) -> float:
    """Largest peak-to-trough decline as a fraction (e.g. 0.15 == 15%)."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0][1]
    max_drawdown = 0.0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return max_drawdown


def compute_win_rate(trades: tuple[Trade, ...]) -> float | None:
    """Fraction of trades with pnl > 0; None when there are no trades.

    Matches `paper.journal.PaperJournal._win_rate`'s convention: pnl == 0 is
    neutral (counted in the denominator, excluded from the win numerator).
    """
    if not trades:
        return None
    wins = sum(1 for trade in trades if trade.pnl > 0)
    return wins / len(trades)


def compute_profit_factor(trades: tuple[Trade, ...]) -> float | None:
    """Gross gains / abs(gross losses); None when there are no losing trades."""
    gains = sum(trade.pnl for trade in trades if trade.pnl > 0)
    losses = sum(-trade.pnl for trade in trades if trade.pnl < 0)
    if losses == 0:
        return None
    return gains / losses


def compute_expectancy_per_trade(trades: tuple[Trade, ...]) -> float | None:
    """Mean pnl per trade; None when there are no trades."""
    if not trades:
        return None
    return sum(trade.pnl for trade in trades) / len(trades)


def _trade_r_multiple(trade: Trade) -> float | None:
    if trade.initial_stop_price is None:
        return None
    risk_per_share = trade.entry_price - trade.initial_stop_price
    if risk_per_share <= 0:
        return None
    return trade.pnl / (risk_per_share * trade.shares)


def compute_avg_r_multiple(trades: tuple[Trade, ...]) -> float | None:
    """Mean R-multiple over trades with a recorded, valid initial stop.

    Trades whose initial stop wasn't recorded (or is at/above entry, a data
    anomaly) are silently excluded from the average, not treated as zero.
    """
    values = [r for trade in trades if (r := _trade_r_multiple(trade)) is not None]
    return sum(values) / len(values) if values else None


def compute_reliability_warnings(
    trade_count: int,
    win_rate: float | None,
    max_drawdown_pct: float,
    thresholds: BacktestConfig,
) -> tuple[str, ...]:
    """Sample-size and look-ahead-bias suspicion warnings (roadmap §5 P2-07)."""
    warnings: list[str] = []
    if trade_count < thresholds.insufficient_trade_count_threshold:
        warnings.append(
            f"統計的に不十分（trade_count={trade_count}、最低"
            f"{thresholds.insufficient_trade_count_threshold}件、推奨"
            f"{thresholds.preliminary_trade_count_threshold}件以上）"
        )
    elif trade_count < thresholds.preliminary_trade_count_threshold:
        warnings.append(
            f"予備的（trade_count={trade_count}、推奨"
            f"{thresholds.preliminary_trade_count_threshold}件以上）"
        )

    is_suspicious = trade_count > 0 and (
        (win_rate is not None and win_rate > thresholds.lookahead_suspicion_win_rate)
        or max_drawdown_pct < thresholds.lookahead_suspicion_max_drawdown
    )
    if is_suspicious:
        warnings.append(LOOKAHEAD_SUSPICION_WARNING)

    return tuple(warnings)


@dataclass(frozen=True, slots=True)
class HoldingDaysStats:
    """Holding-period distribution across a trade log, in sessions."""

    median: float
    p25: float
    p75: float


def exit_reason_breakdown(trades: tuple[Trade, ...]) -> dict[str, int]:
    """Count exits per reason, always reporting every reason the engine can emit.

    Absent reasons are reported as `0` rather than omitted: "no position ever
    reached max-hold" is the interesting reading, and a missing key would
    force every caller to re-state the default. A reason outside
    `EXIT_REASONS` (a newly added exit rule) gets its own key rather than
    being dropped, so the counts always sum to `len(trades)`.

    Args:
        trades: Closed trades to tally.

    Returns:
        `{reason: count}`, covering `EXIT_REASONS` plus any reason observed.
    """
    counts = dict.fromkeys(EXIT_REASONS, 0)
    for trade in trades:
        counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
    return counts


def max_hold_binding_rate(trades: tuple[Trade, ...]) -> float | None:
    """Share of exits that fired because max-hold elapsed, not because of a stop.

    A near-zero rate means the configured `max_hold_days` is not binding, so
    tuning it cannot change the result — the question the exit instrumentation
    exists to answer.

    Args:
        trades: Closed trades to tally.

    Returns:
        The fraction in `[0, 1]`, or `None` when there are no trades.
    """
    if not trades:
        return None
    binding = sum(1 for trade in trades if trade.exit_reason == MAX_HOLD_REASON)
    return binding / len(trades)


def holding_days_stats(trades: tuple[Trade, ...]) -> HoldingDaysStats | None:
    """Median and quartiles of realized holding periods.

    Args:
        trades: Closed trades to summarize.

    Returns:
        The distribution, or `None` when there are no trades. With a single
        trade every quantile collapses onto that trade's holding period.
    """
    if not trades:
        return None
    held = sorted(float(trade.days_held) for trade in trades)
    if len(held) == 1:
        return HoldingDaysStats(median=held[0], p25=held[0], p75=held[0])
    p25, median, p75 = statistics.quantiles(held, n=4, method="inclusive")
    return HoldingDaysStats(median=median, p25=p25, p75=p75)


@dataclass(frozen=True, slots=True)
class DailyExposure:
    """One simulated session's closing capital deployment (Issue #184).

    Recorded after the day's fills and exits, marked at that day's close.
    `equity_usd` is cash plus the marked position value, so `invested_usd /
    equity_usd` is the fraction of the account actually at work — the number
    that says whether a weak result came from picking badly or from never
    deploying the capital.
    """

    day: date
    invested_usd: float
    equity_usd: float
    open_position_count: int


def entry_block_breakdown(tally: Mapping[str, int]) -> dict[str, int]:
    """Count blocked entries per reason, always reporting every known reason.

    Absent reasons are reported as `0` rather than omitted, mirroring
    `exit_reason_breakdown`: "the regime gate never fired" is exactly the
    reading the instrumentation exists to supply, and a missing key would
    force every caller to restate the default. A reason outside
    `ENTRY_BLOCK_REASONS` still gets its own key rather than being dropped.

    Args:
        tally: Raw `{reason: count}` accumulated by the engine.

    Returns:
        `{reason: count}`, covering `ENTRY_BLOCK_REASONS` plus any observed.
    """
    counts = dict.fromkeys(ENTRY_BLOCK_REASONS, 0)
    for reason, count in tally.items():
        counts[reason] = counts.get(reason, 0) + count
    return counts


def compute_avg_invested_pct(exposure: tuple[DailyExposure, ...]) -> float | None:
    """Mean fraction of equity held in positions across the simulated sessions.

    Sessions whose equity is not positive are excluded rather than treated as
    zero exposure (the ratio is undefined there).

    Args:
        exposure: One record per simulated session.

    Returns:
        The mean in `[0, 1]`-ish terms (>1 is possible only with leverage,
        which this simulator never takes), or `None` when no session has
        positive equity.
    """
    ratios = [
        record.invested_usd / record.equity_usd
        for record in exposure
        if record.equity_usd > 0
    ]
    if not ratios:
        return None
    return statistics.fmean(ratios)


def compute_max_concurrent_reached(exposure: tuple[DailyExposure, ...]) -> int:
    """Largest number of simultaneously open positions observed at any close.

    Args:
        exposure: One record per simulated session.

    Returns:
        The maximum open-position count, or `0` when nothing was simulated.
    """
    return max((record.open_position_count for record in exposure), default=0)
