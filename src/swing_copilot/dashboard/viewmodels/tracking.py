"""View model for the published recommendation-tracking page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from swing_copilot.dashboard import formatting as fmt
from swing_copilot.dashboard import queries
from swing_copilot.dashboard.models import Badge, TrackingRow, TrackingView
from swing_copilot.dashboard.viewmodels import common
from swing_copilot.tracking.board import (
    TrackedRecommendation,
    build_board,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import pandas as pd


_OPEN = "open"
_CLOSED = "closed"
_STATUS_TONES = {_OPEN: "info", _CLOSED: "quiet"}
_EXIT_LABELS = {"stop": "ストップ到達で終了", "max_hold": "時間切れで終了"}


def build_tracking(
    sources: TrackingSources,
) -> TrackingView:
    """Build the proceed-only board from the shared tracking view."""
    records = common.to_records(sources.positions)
    as_of = max(
        (
            resolved
            for record in records
            if (resolved := _record_date(record)) is not None
        ),
        default=date.min,
    )
    board = build_board(
        records,
        as_of=as_of,
        retention_business_days=sources.retention_business_days,
    )
    return TrackingView(
        rows=tuple(_row(row) for row in board),
        retention_business_days=sources.retention_business_days,
    )


@dataclass(frozen=True, slots=True)
class TrackingSources:
    """Raw frame and display setting used by the tracking page."""

    positions: pd.DataFrame
    retention_business_days: int


def _record_date(record: Mapping[str, object]) -> date | None:
    return (
        fmt.as_date(record.get("last_mark_date"))
        or fmt.as_date(record.get("last_marked_date"))
        or fmt.as_date(record.get("entry_date"))
    )


def _row(row: TrackedRecommendation) -> TrackingRow:
    """Convert a board row without exposing account-specific quantities."""
    return TrackingRow(
        symbol=row.symbol,
        run_id=str(row.run_id),
        recommendation=Badge(text="proceed", tone="good"),
        entry_date=fmt.day(row.entry_date),
        entry_price=fmt.number(row.entry_price),
        last_close=fmt.number(row.last_close),
        unrealized_return=fmt.number(
            row.unrealized_return_pct, suffix="%", signed=True
        ),
        stop_price=fmt.number(row.stop_price),
        status=Badge(
            text=_status_text(row),
            tone=_STATUS_TONES.get(row.status, "quiet"),
        ),
        days_remaining=fmt.integer(row.days_remaining, key="none"),
    )


def _status_text(row: TrackedRecommendation) -> str:
    if row.status != _CLOSED:
        return "追跡中"
    label = _EXIT_LABELS.get(row.exit_reason or "", "終了")
    if row.exit_date is None:
        return label
    return f"{label}（{row.exit_date.month}/{row.exit_date.day}）"


def load_tracking(db_path: Path, retention_business_days: int) -> TrackingView:
    """Read and build the board through the dashboard query boundary."""
    return build_tracking(
        TrackingSources(
            positions=queries.tracked_positions(db_path),
            retention_business_days=retention_business_days,
        )
    )
