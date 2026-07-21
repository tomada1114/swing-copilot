"""Position sizing: the tighter of a per-trade risk cap and a per-symbol cap (FR-06)."""

from __future__ import annotations


def calc_position_size(
    account_equity: float,
    entry_price: float,
    stop_price: float,
    max_position_pct: float,
    max_trade_risk_pct: float,
) -> int:
    """Return the largest share count satisfying both risk caps.

    Args:
        account_equity: Total account equity in USD.
        entry_price: Planned entry price.
        stop_price: Planned stop price (must be below `entry_price`).
        max_position_pct: Max fraction of equity in one symbol.
        max_trade_risk_pct: Max fraction of equity at risk on one trade,
            measured by the entry-to-stop distance.

    Returns:
        The floored share count satisfying both caps.

    Raises:
        ValueError: `entry_price` is not positive, or `stop_price` is at or
            above `entry_price` (not a valid long-position stop distance).
    """
    if entry_price <= 0:
        msg = f"entry_price must be positive, got {entry_price}"
        raise ValueError(msg)
    if stop_price >= entry_price:
        msg = f"stop_price ({stop_price}) must be below entry_price ({entry_price})"
        raise ValueError(msg)

    risk_per_share = entry_price - stop_price
    risk_based_shares = (account_equity * max_trade_risk_pct) / risk_per_share
    position_based_shares = (account_equity * max_position_pct) / entry_price
    return int(min(risk_based_shares, position_based_shares))
