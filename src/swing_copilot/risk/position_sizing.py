"""Position sizing: the tighter of a per-trade risk cap and a per-symbol cap.

Simulator-only (Issue #385). This module is called from exactly two places:
`backtest/engine.py`, which sizes positions against a backtest-only notional
account, and this module's own tests. The production daily path
(`risk/checks.py`, FR-06) never sizes a position or reports a share count --
Issue #348 removed reader-account-dependent sizing from the public product
because the code has no way to know a reader's actual account, and Issue #352
finished removing account-dependent fields from the exported analysis input.
Do not call this from `risk/checks.py` or any other production path; a public
run must never assume a reader's account size or holdings.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True, slots=True)
class PositionSizeResult:
    """Sizing breakdown (P1-03): which cap produced the final share count.

    `shares` is the floored minimum of the two intermediate values. The
    breakdown is kept because pre-#348 `risk_assessments` rows recorded it;
    `RiskChecker` no longer derives `binding_constraint` from it (Issue #385:
    the only caller is `backtest/engine.py`).
    """

    shares_by_risk: int
    shares_by_position_cap: int
    shares: int


def calc_position_size(
    account_equity: float,
    entry_price: float,
    stop_price: float,
    max_position_pct: float,
    max_trade_risk_pct: float,
) -> PositionSizeResult:
    """Return the largest share count satisfying both risk caps.

    Args:
        account_equity: Total account equity in USD.
        entry_price: Planned entry price.
        stop_price: Planned stop price (must be below `entry_price`).
        max_position_pct: Max fraction of equity in one symbol.
        max_trade_risk_pct: Max fraction of equity at risk on one trade,
            measured by the entry-to-stop distance.

    Returns:
        The risk-based and position-cap-based share counts plus their
        floored minimum, satisfying both caps.

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
    # P1-04 (Issue #13, REQ-001/002): exact fractions.Fraction floor division
    # instead of float division + int() truncation. Fraction(float) captures
    # each input's exact binary value (not a re-rounded decimal string), and
    # Fraction.__floordiv__ is exact integer floor division, so
    # `shares * risk_per_share <= risk_budget` holds algebraically for every
    # input, including extreme ones (e.g. account_equity=1e12, or
    # max_trade_risk_pct as small as 0.0001%) where float division can round
    # the quotient past an integer boundary.
    risk_budget = Fraction(account_equity) * Fraction(max_trade_risk_pct)
    risk_based_shares = risk_budget // Fraction(risk_per_share)

    position_budget = Fraction(account_equity) * Fraction(max_position_pct)
    position_based_shares = position_budget // Fraction(entry_price)
    return PositionSizeResult(
        shares_by_risk=risk_based_shares,
        shares_by_position_cap=position_based_shares,
        shares=min(risk_based_shares, position_based_shares),
    )
