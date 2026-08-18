"""Issue #190 DoD: one closed set, one win rate, whichever ledger holds it.

Three ledgers close round trips in this codebase -- the backtest simulator's
`Trade`, the paper journal's `Position`, and the verdict tracker's
`VerdictPosition` -- and each used to carry its own copy of "what counts as a
win". This test drives the *same* three round trips through all three and
asserts they agree, so a future edit to any one of them fails here instead of
producing three reports that quietly disagree.

It lives at the top level rather than under `tests/backtest`, `tests/paper`,
or `tests/tracking` because the contract it defends belongs to none of them:
it is precisely the agreement *between* the three.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from swing_copilot.backtest.engine import Trade
from swing_copilot.backtest.metrics import compute_win_rate
from swing_copilot.models import Position
from swing_copilot.paper.journal import PaperJournal
from swing_copilot.retro.aggregate import (
    ALL_RECOMMENDATIONS,
    compute_tracked_performance,
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.storage.tracking_records import (
    CLOSED,
    PROCEED,
    VerdictPosition,
    VerdictPositionMark,
)

if TYPE_CHECKING:
    from pathlib import Path

ENTRY_DATE = date(2027, 3, 1)
EXIT_DATE = date(2027, 3, 10)
ENTRY_PRICE = 100.0
STOP_PRICE = 90.0

#: One winner, one exactly-flat trade, one loser. The flat one is the part
#: that used to be able to drift: it is neutral -- counted in the denominator,
#: excluded from the win numerator -- so the agreed answer is 1/3, not 1/2.
EXIT_PRICES = (110.0, 100.0, 95.0)
EXPECTED_WIN_RATE = 1 / 3


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store


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


def test_the_paper_journal_rates_the_same_set_identically(
    state_store: StateStore, tmp_path: Path
) -> None:
    for index, exit_price in enumerate(EXIT_PRICES):
        state_store.upsert_position(
            Position(
                position_id=uuid4(),
                symbol=f"S{index}",
                is_paper=True,
                entry_date=ENTRY_DATE,
                entry_price=ENTRY_PRICE,
                shares=1,
                status="closed",
                stop_price=STOP_PRICE,
                close_date=EXIT_DATE,
                close_price=exit_price,
                exit_reason="stop_loss",
            )
        )
    market_store = MarketStore(state_store.database, parquet_root=tmp_path / "bars")

    summary = PaperJournal(state_store).summarize_performance(market_store, EXIT_DATE)

    assert summary.closed_trade_count == len(EXIT_PRICES)
    assert summary.win_rate == pytest.approx(EXPECTED_WIN_RATE)


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
