"""Issue #190 DoD: one closed set, one win rate, whichever ledger holds it.

Two ledgers close round trips in this codebase -- the backtest simulator's
`Trade` and the verdict tracker's `VerdictPosition` -- and each used to carry
its own copy of "what counts as a win". This test drives the *same* three
round trips through both and asserts they agree, so a future edit to either
fails here instead of producing two reports that quietly disagree. (A third
ledger, the paper journal's `Position`, was part of this agreement until the
real-trade record feature was removed in 2026-08.)

It lives at the top level rather than under `tests/backtest` or
`tests/tracking` because the contract it defends belongs to neither: it is
precisely the agreement *between* them.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import pytest

from swing_copilot.backtest.engine import Trade
from swing_copilot.backtest.metrics import compute_win_rate
from swing_copilot.retro.aggregate import (
    ALL_RECOMMENDATIONS,
    compute_tracked_performance,
)
from swing_copilot.storage.tracking_records import (
    CLOSED,
    PROCEED,
    VerdictPosition,
    VerdictPositionMark,
)

ENTRY_DATE = date(2027, 3, 1)
EXIT_DATE = date(2027, 3, 10)
ENTRY_PRICE = 100.0
STOP_PRICE = 90.0

#: One winner, one exactly-flat trade, one loser. The flat one is the part
#: that used to be able to drift: it is neutral -- counted in the denominator,
#: excluded from the win numerator -- so the agreed answer is 1/3, not 1/2.
EXIT_PRICES = (110.0, 100.0, 95.0)
EXPECTED_WIN_RATE = 1 / 3


def test_the_simulator_rates_the_set_one_in_three() -> None:
    trades = tuple(
        Trade(
            symbol=f"S{index}",
            entry_date=ENTRY_DATE,
            entry_price=ENTRY_PRICE,
            exit_date=EXIT_DATE,
            exit_price=exit_price,
            shares=1,
            exit_reason="stop",
            initial_stop_price=STOP_PRICE,
        )
        for index, exit_price in enumerate(EXIT_PRICES)
    )

    assert compute_win_rate(trades) == pytest.approx(EXPECTED_WIN_RATE)


def test_the_tracking_ledger_rates_the_same_set_identically() -> None:
    positions = tuple(
        _tracked(f"S{index}", exit_price)
        for index, exit_price in enumerate(EXIT_PRICES)
    )
    marks = {
        (position.run_id, position.symbol): VerdictPositionMark(
            run_id=position.run_id,
            symbol=position.symbol,
            as_of_date=ENTRY_DATE,
            close=ENTRY_PRICE,
            stop_price=STOP_PRICE,
            unrealized_return_pct=0.0,
        )
        for position in positions
    }

    pooled = next(
        row
        for row in compute_tracked_performance(positions, marks)
        if row.recommendation == ALL_RECOMMENDATIONS
    )

    assert pooled.closed_count == len(EXIT_PRICES)
    assert pooled.win_rate == pytest.approx(EXPECTED_WIN_RATE)


def _tracked(symbol: str, exit_price: float) -> VerdictPosition:
    return VerdictPosition(
        run_id=UUID(int=abs(hash(symbol)) % (1 << 128)),
        symbol=symbol,
        strategy_key="default",
        recommendation=PROCEED,
        no_trade=False,
        entry_date=ENTRY_DATE,
        entry_price=ENTRY_PRICE,
        stop_price=STOP_PRICE,
        days_held=(EXIT_DATE - ENTRY_DATE) // timedelta(days=1),
        status=CLOSED,
        exit_date=EXIT_DATE,
        exit_price=exit_price,
        exit_reason="stop",
        realized_return_pct=(exit_price - ENTRY_PRICE) / ENTRY_PRICE * 100,
        last_marked_date=EXIT_DATE,
    )
