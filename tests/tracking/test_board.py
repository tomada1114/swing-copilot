"""Published tracking-board selection and point-in-time retention contracts."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from swing_copilot.storage.tracking_records import VerdictPosition, VerdictPositionMark
from swing_copilot.tracking.board import build_board, position_records

RUN_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AS_OF = date(2027, 3, 10)


def _position(  # noqa: PLR0913 - test fixture exposes independent ledger fields
    symbol: str,
    *,
    recommendation: str = "proceed",
    status: str = "open",
    exit_date: date | None = None,
    max_hold_days: int = 12,
    days_held: int = 3,
) -> VerdictPosition:
    return VerdictPosition(
        run_id=RUN_ID,
        symbol=symbol,
        strategy_key="default",
        recommendation=recommendation,
        no_trade=False,
        entry_date=date(2027, 3, 1),
        entry_price=100.0,
        stop_price=96.0,
        days_held=days_held,
        status=status,
        exit_date=exit_date,
        exit_reason="stop" if status == "closed" else None,
        max_hold_days=max_hold_days,
    )


def test_position_records_merge_the_latest_mark_and_freeze_remaining_days() -> None:
    position = _position("AAA")
    mark = VerdictPositionMark(
        run_id=RUN_ID,
        symbol="AAA",
        as_of_date=AS_OF,
        close=103.0,
        stop_price=96.0,
        unrealized_return_pct=3.0,
    )

    (row,) = build_board(
        position_records((position,), {(RUN_ID, "AAA"): mark}),
        as_of=AS_OF,
        retention_business_days=5,
    )

    assert row.symbol == "AAA"
    assert row.last_close == 103.0
    assert row.unrealized_return_pct == 3.0
    assert row.days_remaining == 9


def test_board_keeps_proceed_open_rows_first_and_filters_skip_rows() -> None:
    rows = position_records(
        (_position("BBB"), _position("AAA", recommendation="skip")), {}
    )

    board = build_board(rows, as_of=AS_OF, retention_business_days=5)

    assert tuple(row.symbol for row in board) == ("BBB",)


def test_closed_retention_is_inclusive_on_the_business_day_boundary() -> None:
    rows = position_records(
        (
            _position("SAME", status="closed", exit_date=AS_OF),
            _position("BOUNDARY", status="closed", exit_date=date(2027, 3, 3)),
            _position("EXPIRED", status="closed", exit_date=date(2027, 3, 2)),
        ),
        {},
    )

    board = build_board(rows, as_of=AS_OF, retention_business_days=5)

    assert tuple(row.symbol for row in board) == ("SAME", "BOUNDARY")
    assert all(row.days_remaining is None for row in board)


def test_negative_retention_is_rejected() -> None:
    with pytest.raises(ValueError, match="retention_business_days"):
        build_board((), as_of=AS_OF, retention_business_days=-1)
