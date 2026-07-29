"""Trading-calendar and forward-return primitives (P2-11 / P8-30).

Extracted from `pipeline/postmortem.py` so the retrospective mechanism
(`retro/`) can reuse exactly the same calendar and return arithmetic instead
of growing a second, silently divergent copy. Both consumers derive their
trading calendar from one benchmark symbol's own distinct bar dates -- this
repo has no dedicated trading-calendar module; `backtest/runner.py`'s
`_trading_days()` is the third mirror of the same idea.

Two directions are offered over that one calendar:

* `find_target_trading_day` looks *backward* -- "which run is exactly N
  sessions old today?" -- which is what a daily postmortem asks.
* `find_maturity_trading_day` looks *forward* -- "on which session does this
  run's N-day horizon come due?" -- which is what a batch retrospective asks
  (`docs/goal-prompts/swing-copilot-retrospective/design.md` §5.2).

They are inverses on the same calendar, and the round-trip tests in
`tests/pipeline/test_forward_returns.py` hold them to it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.storage.market_store import MarketStore

# Calendar-derivation window: wide enough to cross holidays/weekends for the
# requested horizon (the (N+5)*3 heuristic this feature's design already uses).
CALENDAR_WINDOW_PADDING_DAYS = 5
CALENDAR_WINDOW_MULTIPLIER = 3


def _calendar_window_days(horizon_days: int) -> int:
    return (horizon_days + CALENDAR_WINDOW_PADDING_DAYS) * CALENDAR_WINDOW_MULTIPLIER


def _trading_days(
    market_store: MarketStore,
    benchmark_symbol: str,
    start: date,
    end: date,
    as_of: date,
) -> list[date]:
    """Return the benchmark's distinct bar dates in `[start, end]`, ascending.

    `read_bars`' own `as_of` clamp is the point-in-time guard: no session
    dated after `as_of` can enter the derived calendar regardless of `end`.
    """
    bars = market_store.read_bars([benchmark_symbol], start, end, as_of)
    if bars.empty:
        return []
    return sorted(bars["date"].unique().tolist())


def find_target_trading_day(
    market_store: MarketStore, benchmark_symbol: str, as_of: date, horizon_days: int
) -> date | None:
    """Return the trading day `horizon_days` sessions before `as_of`, or `None`.

    Args:
        market_store: Bars source.
        benchmark_symbol: Reference symbol whose distinct bar dates stand in
            for the trading calendar (e.g. `settings.backtest.benchmark`).
        as_of: Today's evaluation date -- assumed to itself be the most
            recent trading day in the window.
        horizon_days: How many trading sessions back to look.

    Returns:
        `None` if there are fewer than `horizon_days + 1` distinct trading
        days in the window -- there is no way to compute this horizon yet
        (e.g. too early in the product's life, or a data gap).
    """
    start = as_of - timedelta(days=_calendar_window_days(horizon_days))
    trading_days = _trading_days(market_store, benchmark_symbol, start, as_of, as_of)
    if len(trading_days) < horizon_days + 1:
        return None
    return trading_days[-1 - horizon_days]


def find_maturity_trading_day(
    market_store: MarketStore,
    benchmark_symbol: str,
    run_date: date,
    horizon_days: int,
    *,
    as_of: date,
) -> date | None:
    """Return the session `horizon_days` after `run_date`, or `None` if not yet due.

    The retrospective runs in batches every few days, so it cannot ask "which
    run is 5 sessions old *today*"; it asks each collected run when its
    horizon came due and evaluates only the matured ones. Recording that
    maturity date (rather than the observation date) is what makes re-running
    the batch idempotent (design §5.2 / decision D7).

    Args:
        market_store: Bars source.
        benchmark_symbol: Reference symbol standing in for the calendar.
        run_date: The run's own date, which must itself be a session on that
            calendar -- otherwise "N sessions later" has no defined origin.
        horizon_days: How many trading sessions forward to look.
        as_of: Point-in-time cutoff. Sessions after it are invisible, so a
            horizon that has not matured by `as_of` yields `None` rather than
            peeking ahead.

    Returns:
        The maturity session, or `None` when the benchmark has no bars, when
        `run_date` is not itself a session (a data gap, treated as a fail-soft
        skip), or when the horizon has not matured on or before `as_of`.
    """
    end = run_date + timedelta(days=_calendar_window_days(horizon_days))
    trading_days = _trading_days(market_store, benchmark_symbol, run_date, end, as_of)
    if not trading_days or trading_days[0] != run_date:
        return None
    if len(trading_days) < horizon_days + 1:
        return None
    return trading_days[horizon_days]


def compute_forward_return(
    market_store: MarketStore, symbol: str, run_date: date, as_of: date
) -> float | None:
    """Return `(close(as_of) - close(run_date)) / close(run_date) * 100`, or `None`.

    `read_bars`' own `as_of` clamp already guarantees no bar dated after
    `as_of` is ever considered (REQ-006, look-ahead prevention) -- this is
    not re-checked here, it is structurally impossible via this call.
    `None` covers a missing close on either date, a genuine data-quality
    skip rather than an error.
    """
    bars = market_store.read_bars([symbol], run_date, as_of, as_of)
    if bars.empty:
        return None
    run_rows = bars[bars["date"] == run_date]
    as_of_rows = bars[bars["date"] == as_of]
    if run_rows.empty or as_of_rows.empty:
        return None
    run_close = float(run_rows.iloc[0]["close"])
    as_of_close = float(as_of_rows.iloc[0]["close"])
    if run_close == 0:
        return None
    return (as_of_close - run_close) / run_close * 100
