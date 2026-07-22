"""Paper-trading journal: decisions, position lifecycle, performance (FR-11, CON-04).

`docs/04_detailed_design.md` 3.20's `record_decision(signal_id: int, ...)` /
`close_position(position_id: int, ...)` are stale pseudocode predating the
actual schema (`storage/schema.py`): there is no `signal_id` column anywhere,
and `positions.position_id` is a `UUID`. This module follows the schema —
`(run_id, symbol, strategy_key)` as the decision's natural key and
`position_id: UUID` throughout — per
`docs/goal-prompts/swing-copilot-p2-report-paper-wrapup/decisions.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.storage.paper_records import TradeDecisionRecord

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

    from swing_copilot.models import Position
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore

_MIN_BARS_FOR_RETURN = 2


class PositionNotClosableError(SwingCopilotError):
    """Raised when `close_position()` cannot perform a real state transition."""


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    """Closed paper trades' aggregate P&L vs. a SPY buy-and-hold benchmark."""

    closed_trade_count: int
    total_pnl_usd: float
    win_rate: float  # fraction of closed trades with positive P&L; 0.0 if none closed
    spy_return_pct: float | None  # None if SPY bars are insufficient for the span


class PaperJournal:
    """Wraps `StateStore` — does not own a second connection to `positions`/`trades_journal`."""

    def __init__(self, state_store: StateStore) -> None:
        """Create the journal.

        Args:
            state_store: Repository owning `positions` and `trades_journal`.
        """
        self._state_store = state_store

    def record_decision(  # noqa: PLR0913 - exact signature from design.md 5
        self,
        run_id: UUID,
        symbol: str,
        strategy_key: str,
        decision: str,
        reason_memo: str | None,
        virtual_fill_price: float | None,
    ) -> None:
        """Record a human decision on one candidate.

        Args:
            run_id: The run this decision was made against.
            symbol: Candidate symbol.
            strategy_key: Which strategy produced the candidate.
            decision: `"followed"` | `"ignored"` | `"modified"`.
            reason_memo: Optional free-text rationale.
            virtual_fill_price: Optional paper fill price if followed/modified.

        Upserts `trades_journal` keyed on `(run_id, symbol, strategy_key)` —
        re-recording the same key updates the row (idempotent), it does not
        insert a duplicate.
        """
        self._state_store.record_trade_decision(
            TradeDecisionRecord(
                run_id=run_id,
                symbol=symbol,
                strategy_key=strategy_key,
                position_id=None,
                decision=decision,
                reason_memo=reason_memo,
                virtual_fill_price=virtual_fill_price,
            )
        )

    def close_position(
        self, position_id: UUID, close_date: date, close_price: float
    ) -> None:
        """Close an open paper position.

        Args:
            position_id: The position to close.
            close_date: Date the virtual exit fill occurred.
            close_price: Virtual exit fill price.

        Raises:
            PositionNotClosableError: `position_id` doesn't exist or is
                already closed — closing must be a real state transition,
                not a silent no-op.
        """
        position = self._state_store.get_position(position_id)
        if position is None:
            msg = f"no position exists with position_id={position_id}"
            raise PositionNotClosableError(msg)
        if position.status == "closed":
            msg = f"position {position_id} is already closed"
            raise PositionNotClosableError(msg)

        self._state_store.upsert_position(
            replace(
                position,
                status="closed",
                close_date=close_date,
                close_price=close_price,
            )
        )

    def summarize_performance(
        self, market_store: MarketStore, as_of: date
    ) -> PerformanceSummary:
        """Summarize closed paper trades' P&L vs. a SPY buy-and-hold benchmark.

        Args:
            market_store: Store to read the SPY benchmark bars from.
            as_of: Point-in-time cutoff for both the summary and the SPY span.

        Returns:
            Aggregate P&L/win-rate over every closed paper position, and the
            SPY buy-and-hold return over the same span (earliest closed
            `entry_date` .. `as_of`), mirroring `backtest/engine.py`'s
            benchmark idea but over real paper trades instead of a
            simulation. `spy_return_pct` is `None` if SPY bars are
            insufficient for the span.
        """
        closed = self._state_store.get_closed_positions(is_paper=True)
        if not closed:
            return PerformanceSummary(
                closed_trade_count=0,
                total_pnl_usd=0.0,
                win_rate=0.0,
                spy_return_pct=None,
            )

        pnls = [self._position_pnl(position) for position in closed]
        win_rate = sum(1 for pnl in pnls if pnl > 0) / len(pnls)
        earliest_entry = min(position.entry_date for position in closed)

        return PerformanceSummary(
            closed_trade_count=len(closed),
            total_pnl_usd=sum(pnls),
            win_rate=win_rate,
            spy_return_pct=self._spy_return_pct(market_store, earliest_entry, as_of),
        )

    @staticmethod
    def _position_pnl(position: Position) -> float:
        # Only ever called on rows from get_closed_positions(): status="closed"
        # is set exclusively by close_position(), which always sets close_price too.
        assert position.close_price is not None  # noqa: S101
        return (position.close_price - position.entry_price) * position.shares

    @staticmethod
    def _spy_return_pct(
        market_store: MarketStore, start: date, as_of: date
    ) -> float | None:
        bars = market_store.read_bars(["SPY"], start, as_of, as_of).sort_values("date")
        if len(bars) < _MIN_BARS_FOR_RETURN:
            return None
        first_close = float(bars.iloc[0]["close"])
        last_close = float(bars.iloc[-1]["close"])
        return (last_close - first_close) / first_close * 100
