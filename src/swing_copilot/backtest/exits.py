"""Pure exit rules: trailing-stop ratchet, exit trigger, and the ATR it uses.

Extracted from `backtest/engine.py` so anything that has to reproduce the
backtest's exit behavior (e.g. tracking a virtual position forward day by day)
calls the *same* functions instead of a second, drifting copy. These are pure:
no clock, no I/O, and every point-in-time cutoff arrives as an explicit
`as_of`.

The ATR period is fixed at 14 here, matching the engine's previous hardcoded
value (`settings.backtest.exit_atr_period` is not wired up).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from swing_copilot.screening.indicators import symbol_bars, wilder_atr

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd

ATR_PERIOD = 14

ExitReason = Literal["stop", "max_hold"]


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """A triggered exit: the fill price before costs, and why it fired."""

    exit_price: float
    reason: ExitReason


def next_trailing_stop(
    *,
    current_stop: float | None,
    close: float,
    atr: float,
    exit_atr_multiple: float,
) -> float:
    """Ratchet a trailing stop after a day's close (effective from the next day).

    Args:
        current_stop: Stop in force today, or `None` when no stop exists yet
            (the candidate is then adopted unconditionally).
        close: Today's closing price.
        atr: ATR as of today.
        exit_atr_multiple: ATR multiple below the close to place the stop.

    Returns:
        The new stop, which never moves down.
    """
    candidate = close - exit_atr_multiple * atr
    if current_stop is None:
        return candidate
    return max(current_stop, candidate)


# PLR0913: one day's bar plus the holding counters, all keyword-only; wrapping
# them in an object would only add construction noise at the call sites.
def evaluate_exit(  # noqa: PLR0913
    *,
    open_price: float,
    low: float,
    close: float,
    stop_price: float | None,
    days_held: int,
    max_hold_days: int,
) -> ExitDecision | None:
    """Decide whether one day's bar closes a position, and at what price.

    A gap through the stop fills at the open, an intraday touch fills at the
    stop itself, and the stop always wins over max-hold when both trigger on
    the same day.

    Args:
        open_price: Today's open.
        low: Today's low.
        close: Today's close.
        stop_price: Stop in force today, or `None` when no stop could be
            computed (stop checks are then skipped and only max-hold applies).
        days_held: Full days already held before today.
        max_hold_days: Maximum holding period in trading days.

    Returns:
        The exit decision, or `None` to keep holding.
    """
    if stop_price is not None:
        if open_price <= stop_price:
            return ExitDecision(exit_price=open_price, reason="stop")
        if low <= stop_price:
            return ExitDecision(exit_price=stop_price, reason="stop")
    if days_held + 1 >= max_hold_days:
        return ExitDecision(exit_price=close, reason="max_hold")
    return None


def atr14_as_of(bars: pd.DataFrame, symbol: str, as_of: date) -> float | None:
    """Return the latest Wilder ATR(14) for `symbol` at an inclusive cutoff.

    Args:
        bars: Tidy OHLCV bars (`symbol, date, open, high, low, close, ...`).
        symbol: Ticker to select.
        as_of: Point-in-time cutoff (inclusive); later bars are never read.

    Returns:
        The ATR, or `None` when there are fewer than `ATR_PERIOD` bars up to
        the cutoff or the smoothed value is not a number.
    """
    series = symbol_bars(bars, symbol, as_of)
    if series is None or len(series) < ATR_PERIOD:
        return None
    atr = wilder_atr(series["high"], series["low"], series["close"], ATR_PERIOD).iloc[
        -1
    ]
    return None if math.isnan(atr) else float(atr)
