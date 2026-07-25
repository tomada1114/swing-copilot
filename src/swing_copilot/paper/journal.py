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

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.storage.paper_records import TradeDecisionRecord

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

    from swing_copilot.models import Position
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore

_MIN_BARS_FOR_RETURN = 2
_VALID_DECISIONS = frozenset({"followed", "ignored", "modified"})
# P1-06/REQ-001/020: close_position() input values only. "unknown" is a
# migration-only sentinel (schema.py's ALTER_SCHEMA_STATEMENTS backfill) and
# is deliberately excluded here — never a valid close() argument.
_VALID_EXIT_REASONS = frozenset({"stop_loss", "target", "time_stop", "manual", "other"})
_UNKNOWN_BUCKET_KEY = "unknown"
_ET = ZoneInfo("America/New_York")


class PositionNotClosableError(SwingCopilotError):
    """Raised when `close_position()` cannot perform a real state transition."""


class InvalidDecisionError(SwingCopilotError):
    """Raised when `record_decision()` receives an unrecognized `decision` value."""


@dataclass(frozen=True, slots=True)
class PerformanceBreakdownRow:
    """One group's aggregate stats within a `PerformanceSummary` breakdown."""

    key: str  # an exit_reason value, a strategy_key, or "unknown" for unlinked rows
    trade_count: int
    win_rate: float | None  # None only if trade_count == 0 (never happens: a
    # group exists only because >=1 trade produced it)
    avg_pnl_usd: float | None


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    """Closed paper trades' aggregate P&L vs. a SPY buy-and-hold benchmark."""

    closed_trade_count: int
    total_pnl_usd: float  # 0.0 for zero trades (sum of empty is well-defined)
    win_rate: float | None  # None (undefined) when closed_trade_count == 0
    spy_return_pct: float | None  # None if SPY bars are insufficient for the span
    expectancy_usd: float | None  # mean pnl; None when closed_trade_count == 0
    profit_factor: float | None  # gains / abs(losses); None when there are no losses
    avg_r_multiple: float | None  # mean over trades where it was computable
    r_multiple_omitted_count: int  # trades excluded from avg_r_multiple (REQ-022)
    r_multiple_omitted_warning: str | None  # None if r_multiple_omitted_count == 0
    by_exit_reason: tuple[PerformanceBreakdownRow, ...]
    by_strategy: tuple[PerformanceBreakdownRow, ...]


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
        *,
        position_id: UUID | None = None,
    ) -> None:
        """Record a human decision on one candidate.

        Args:
            run_id: The run this decision was made against.
            symbol: Candidate symbol.
            strategy_key: Which strategy produced the candidate.
            decision: `"followed"` | `"ignored"` | `"modified"`.
            reason_memo: Optional free-text rationale.
            virtual_fill_price: Optional paper fill price if followed/modified.
            position_id: The paper position this decision resulted in, once one
                exists. Linking is a two-step workflow: record the decision
                first (before any position exists, so `position_id` is left as
                the default `None`); once a paper position is opened (via
                `StateStore.upsert_position`), re-record the same
                `(run_id, symbol, strategy_key)` natural key, this time
                passing `position_id` — the upsert updates the existing row in
                place, completing the signal-to-decision-to-fill-to-P&L link
                (FR-11).

        Upserts `trades_journal` keyed on `(run_id, symbol, strategy_key)` —
        re-recording the same key updates the row (idempotent), it does not
        insert a duplicate.

        Raises:
            InvalidDecisionError: `decision` is not one of `"followed"`,
                `"ignored"`, `"modified"` — checked fail-fast here so an
                invalid value surfaces as a domain error, not a raw DuckDB
                CHECK-constraint failure. Also raised when `position_id` is
                given together with `decision="ignored"`: an ignored
                candidate never results in a paper position, so the pairing
                is contradictory and rejected fail-fast rather than persisted
                silently.
        """
        if decision not in _VALID_DECISIONS:
            msg = (
                f"decision must be one of {sorted(_VALID_DECISIONS)}, got {decision!r}"
            )
            raise InvalidDecisionError(msg)
        if position_id is not None and decision == "ignored":
            msg = (
                "position_id cannot be set when decision='ignored' — an "
                "ignored candidate has no resulting paper position"
            )
            raise InvalidDecisionError(msg)

        self._state_store.record_trade_decision(
            TradeDecisionRecord(
                run_id=run_id,
                symbol=symbol,
                strategy_key=strategy_key,
                position_id=position_id,
                decision=decision,
                reason_memo=reason_memo,
                virtual_fill_price=virtual_fill_price,
            )
        )

    def close_position(
        self,
        position_id: UUID,
        close_date: date,
        close_price: float,
        exit_reason: str,
        *,
        closed_at: datetime | None = None,
    ) -> None:
        """Close an open paper position.

        Args:
            position_id: The position to close.
            close_date: Date the virtual exit fill occurred.
            close_price: Virtual exit fill price.
            exit_reason: Why the position was closed — required, one of
                `"stop_loss"`, `"target"`, `"time_stop"`, `"manual"`,
                `"other"` (P1-06/REQ-001/020). `"unknown"` is a
                migration-only sentinel and is never accepted here.
            closed_at: Precise timezone-aware close time. When omitted, the
                fill is deterministically recorded as 16:00 ET on `close_date`.

        Raises:
            PositionNotClosableError: `exit_reason` isn't one of the 5 valid
                values, `position_id` doesn't exist, is already closed,
                `close_date` precedes `entry_date`, or `close_price` is not
                positive — closing must be a real, valid state transition,
                not a silent no-op or a garbage fill. `exit_reason` is
                validated first, before any state is read or touched.
        """
        if exit_reason not in _VALID_EXIT_REASONS:
            msg = (
                f"exit_reason must be one of {sorted(_VALID_EXIT_REASONS)}, "
                f"got {exit_reason!r}"
            )
            raise PositionNotClosableError(msg)

        position = self._state_store.get_position(position_id)
        if position is None:
            msg = f"no position exists with position_id={position_id}"
            raise PositionNotClosableError(msg)
        if position.status == "closed":
            msg = f"position {position_id} is already closed"
            raise PositionNotClosableError(msg)
        if close_date < position.entry_date:
            msg = (
                f"close_date {close_date} precedes entry_date {position.entry_date} "
                f"for position {position_id}"
            )
            raise PositionNotClosableError(msg)
        if close_price <= 0:
            msg = f"close_price must be positive, got {close_price}"
            raise PositionNotClosableError(msg)
        if closed_at is None:
            closed_at = datetime.combine(close_date, time(16), tzinfo=_ET)
        elif closed_at.tzinfo is None:
            msg = "closed_at must be timezone-aware"
            raise PositionNotClosableError(msg)
        elif closed_at.astimezone(_ET).date() != close_date:
            msg = "closed_at Eastern date must match close_date"
            raise PositionNotClosableError(msg)

        self._state_store.upsert_position(
            replace(
                position,
                status="closed",
                close_date=close_date,
                close_at=closed_at,
                close_price=close_price,
                exit_reason=exit_reason,
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
            Aggregate P&L/win-rate/expectancy/profit_factor/R-multiple over
            every closed paper position (plus exit_reason and strategy
            breakdowns), and the SPY buy-and-hold return over the same span
            (earliest closed `entry_date` .. `as_of`), mirroring
            `backtest/engine.py`'s benchmark idea but over real paper trades
            instead of a simulation. All rate/ratio fields are `None` (not
            an exception or a misleading 0.0) when they're mathematically
            undefined for the current data (P1-06 boundary conditions).
        """
        rows = self._state_store.get_closed_positions_with_strategy(
            is_paper=True, as_of=as_of
        )
        if not rows:
            return PerformanceSummary(
                closed_trade_count=0,
                total_pnl_usd=0.0,
                win_rate=None,
                spy_return_pct=None,
                expectancy_usd=None,
                profit_factor=None,
                avg_r_multiple=None,
                r_multiple_omitted_count=0,
                r_multiple_omitted_warning=None,
                by_exit_reason=(),
                by_strategy=(),
            )

        positions = [position for position, _ in rows]
        pnls = [self._position_pnl(position) for position in positions]
        trade_count = len(pnls)
        earliest_entry = min(position.entry_date for position in positions)

        avg_r_multiple, omitted_count, warning = self._r_multiple_stats(positions, pnls)

        return PerformanceSummary(
            closed_trade_count=trade_count,
            total_pnl_usd=sum(pnls),
            win_rate=self._win_rate(pnls),
            spy_return_pct=self._spy_return_pct(market_store, earliest_entry, as_of),
            expectancy_usd=sum(pnls) / trade_count,
            profit_factor=self._profit_factor(pnls),
            avg_r_multiple=avg_r_multiple,
            r_multiple_omitted_count=omitted_count,
            r_multiple_omitted_warning=warning,
            by_exit_reason=self._breakdown_by_exit_reason(positions, pnls),
            by_strategy=self._breakdown_by_strategy(rows, pnls),
        )

    @staticmethod
    def _position_pnl(position: Position) -> float:
        # Only ever called on rows from get_closed_positions*(): status="closed"
        # is set exclusively by close_position(), which always sets close_price too.
        assert position.close_price is not None  # noqa: S101
        return (position.close_price - position.entry_price) * position.shares

    @staticmethod
    def _win_rate(pnls: list[float]) -> float | None:
        # P1-06 win/loss convention (documented once, applied everywhere
        # win_rate is computed): pnl > 0 is a win; pnl == 0 is neutral
        # (counted in the denominator, excluded from the win numerator);
        # pnl < 0 is a loss. None only when there are zero trades to rate.
        if not pnls:
            return None
        wins = sum(1 for pnl in pnls if pnl > 0)
        return wins / len(pnls)

    @staticmethod
    def _profit_factor(pnls: list[float]) -> float | None:
        # Same win/neutral/loss convention as _win_rate(): pnl == 0
        # contributes to neither the gain nor the loss sum (REQ-021/022's
        # boundary text leaves this choice to the implementation; fixed here).
        gains = sum(pnl for pnl in pnls if pnl > 0)
        losses = sum(-pnl for pnl in pnls if pnl < 0)  # positive magnitude
        if losses == 0:
            return None
        return gains / losses

    @staticmethod
    def _r_multiple(position: Position, pnl: float) -> float | None:
        # R-multiple = pnl / ((entry - stop) * shares). Omitted when stop
        # isn't recorded, and also — a defensive extension beyond the
        # issue's literal text — when entry - stop <= 0: that's a data
        # anomaly (stop at or above entry), not a legitimate zero/negative
        # risk-per-share, and would otherwise divide by zero or invert sign.
        if position.stop_price is None:
            return None
        risk_per_share = position.entry_price - position.stop_price
        if risk_per_share <= 0:
            return None
        return pnl / (risk_per_share * position.shares)

    @classmethod
    def _r_multiple_stats(
        cls, positions: list[Position], pnls: list[float]
    ) -> tuple[float | None, int, str | None]:
        computed: list[float] = []
        omitted_count = 0
        for position, pnl in zip(positions, pnls, strict=True):
            r_multiple = cls._r_multiple(position, pnl)
            if r_multiple is None:
                omitted_count += 1
            else:
                computed.append(r_multiple)
        avg_r_multiple = sum(computed) / len(computed) if computed else None
        warning = (
            f"{omitted_count}件のトレードでstop未記録のためR-multiple省略"
            if omitted_count > 0
            else None
        )
        return avg_r_multiple, omitted_count, warning

    @classmethod
    def _breakdown_by_exit_reason(
        cls, positions: list[Position], pnls: list[float]
    ) -> tuple[PerformanceBreakdownRow, ...]:
        groups: dict[str, list[float]] = defaultdict(list)
        for position, pnl in zip(positions, pnls, strict=True):
            groups[position.exit_reason or _UNKNOWN_BUCKET_KEY].append(pnl)
        return cls._rows_from_groups(groups)

    @classmethod
    def _breakdown_by_strategy(
        cls, rows: list[tuple[Position, str | None]], pnls: list[float]
    ) -> tuple[PerformanceBreakdownRow, ...]:
        groups: dict[str, list[float]] = defaultdict(list)
        for (_, strategy_key), pnl in zip(rows, pnls, strict=True):
            groups[strategy_key or _UNKNOWN_BUCKET_KEY].append(pnl)
        return cls._rows_from_groups(groups)

    @classmethod
    def _rows_from_groups(
        cls, groups: dict[str, list[float]]
    ) -> tuple[PerformanceBreakdownRow, ...]:
        return tuple(
            PerformanceBreakdownRow(
                key=key,
                trade_count=len(group_pnls),
                win_rate=cls._win_rate(group_pnls),
                avg_pnl_usd=sum(group_pnls) / len(group_pnls) if group_pnls else None,
            )
            for key, group_pnls in sorted(groups.items())
        )

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
