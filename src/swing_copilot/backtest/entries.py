"""Pure entry-order rules shared by risk preview and the backtest.

The production risk preview and the simulator must agree on the planned
limit price.  Keeping the arithmetic here prevents one side from silently
drifting when the setting or the ATR convention changes.  The fill helper
uses daily OHLC as an explicit approximation of a Day limit order because
intraday bars are outside this application's data contract.
"""

from __future__ import annotations


def entry_limit_price(close: float, atr14: float, multiple: float) -> float:
    """Return the planned buy-limit price from a signal-day close and ATR.

    Args:
        close: Signal-day closing price.
        atr14: Signal-day ATR14 used by the screening result.
        multiple: Non-negative ATR multiple above the close.

    Returns:
        The maximum planned fill price for the next session.
    """
    return close + multiple * atr14


def evaluate_entry_fill(
    *, open_price: float, low: float, limit_price: float, slippage_pct: float
) -> float | None:
    """Approximate a one-session buy-limit fill from a daily OHLC bar.

    A gap-down/open-at-or-below-limit fills at the open using the existing
    adverse slippage convention.  When the market opens above the limit but
    trades down to it, the order fills at the limit itself: a buy limit does
    not take adverse slippage above its cap.  The order is not carried into
    the next session.

    Args:
        open_price: Next session's opening price.
        low: Next session's low price.
        limit_price: Maximum planned fill price.
        slippage_pct: Adverse entry slippage applied to an open fill.

    Returns:
        The raw entry execution price, or `None` when the limit was not
        reached during the session.
    """
    if open_price <= limit_price:
        return open_price * (1 + slippage_pct)
    if low <= limit_price:
        return limit_price
    return None
