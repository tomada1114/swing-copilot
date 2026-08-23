"""Pure selection and shaping for the published tracking board.

The tracking ledger contains both `proceed` positions and `skip` shadows.  The
published board is deliberately narrower: it exposes only the former, keeps
closed rows for a short business-day window, and shares this rule across the
CLI, dashboard, and daily reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from swing_copilot.risk.earnings import business_days_since

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from swing_copilot.storage.tracking_records import (
        VerdictPosition,
        VerdictPositionMark,
    )

DEFAULT_PUBLISHED_RETENTION_BUSINESS_DAYS = 5

_OPEN = "open"
_CLOSED = "closed"
_PROCEED = "proceed"


@dataclass(frozen=True, slots=True)
class TrackedRecommendation:
    """One proceed position that is visible on the published board."""

    symbol: str
    run_id: UUID
    entry_date: date
    entry_price: float
    last_close: float | None
    unrealized_return_pct: float | None
    stop_price: float | None
    status: str
    exit_date: date | None
    exit_reason: str | None
    days_held: int
    days_remaining: int | None


def position_records(
    positions: Iterable[VerdictPosition],
    latest_marks: Mapping[tuple[UUID, str], VerdictPositionMark],
) -> tuple[dict[str, object], ...]:
    """Merge position rows with their latest marks for board consumers.

    Args:
        positions: Tracking ledger rows.
        latest_marks: The latest close-based mark keyed by position identity.

    Returns:
        Mapping records accepted by :func:`build_board`.
    """
    records: list[dict[str, object]] = []
    for position in positions:
        mark = latest_marks.get((position.run_id, position.symbol))
        records.append(
            {
                "symbol": position.symbol,
                "run_id": position.run_id,
                "recommendation": position.recommendation,
                "entry_date": position.entry_date,
                "entry_price": position.entry_price,
                "last_close": None if mark is None else mark.close,
                "unrealized_return_pct": (
                    None if mark is None else mark.unrealized_return_pct
                ),
                "stop_price": position.stop_price,
                "status": position.status,
                "exit_date": position.exit_date,
                "exit_reason": position.exit_reason,
                "days_held": position.days_held,
                "max_hold_days": position.max_hold_days,
            }
        )
    return tuple(records)


def build_board(
    records: Iterable[Mapping[str, object]],
    *,
    as_of: date,
    retention_business_days: int,
) -> tuple[TrackedRecommendation, ...]:
    """Select and shape the rows visible on the published tracking board.

    Args:
        records: Position rows merged with their latest marks.  The records
            must contain the fields produced by :func:`position_records`.
        as_of: Inclusive point-in-time date used for the retention window.
        retention_business_days: Number of business days to retain a closed
            row after its exit date, including the exit date itself.

    Returns:
        Proceed rows that are open or still inside the closed-row window.

    Raises:
        ValueError: If the retention window is negative.
    """
    if retention_business_days < 0:
        msg = "retention_business_days must be >= 0"
        raise ValueError(msg)

    rows: list[TrackedRecommendation] = []
    for record in records:
        if str(record.get("recommendation", _PROCEED)) != _PROCEED:
            continue
        status = str(record.get("status", ""))
        entry_date = _required_date(record, "entry_date")
        exit_date = _optional_date(record.get("exit_date"))
        if status == _OPEN:
            visible = True
        elif status == _CLOSED and exit_date is not None:
            visible = business_days_since(as_of, exit_date) <= retention_business_days
        else:
            visible = False
        if not visible:
            continue

        max_hold_days = _int_value(record.get("max_hold_days"), default=25)
        days_held = _int_value(record.get("days_held"), default=0)
        rows.append(
            TrackedRecommendation(
                symbol=str(record["symbol"]),
                run_id=_required_uuid(record, "run_id"),
                entry_date=entry_date,
                entry_price=_required_float(record, "entry_price"),
                last_close=_optional_float(record.get("last_close")),
                unrealized_return_pct=_optional_float(
                    record.get("unrealized_return_pct")
                ),
                stop_price=_optional_float(record.get("stop_price")),
                status=status,
                exit_date=exit_date,
                exit_reason=_optional_text(record.get("exit_reason")),
                days_held=days_held,
                days_remaining=(
                    max(max_hold_days - days_held, 0) if status == _OPEN else None
                ),
            )
        )

    return tuple(sorted(rows, key=_sort_key))


def _sort_key(row: TrackedRecommendation) -> tuple[bool, int, str, str]:
    """Keep open rows first, newest activity first, then ticker order."""
    activity_date = row.entry_date if row.status == _OPEN else row.exit_date
    ordinal = 0 if activity_date is None else activity_date.toordinal()
    return (row.status != _OPEN, -ordinal, row.symbol, str(row.run_id))


def _required_uuid(record: Mapping[str, object], key: str) -> UUID:
    value = record[key]
    return value if isinstance(value, UUID) else UUID(str(value))


def _required_date(record: Mapping[str, object], key: str) -> date:
    value = _optional_date(record.get(key))
    if value is None:
        msg = f"board record is missing {key}"
        raise ValueError(msg)
    return value


def _required_float(record: Mapping[str, object], key: str) -> float:
    value = record.get(key)
    if value is None:
        msg = f"board record is missing {key}"
        raise ValueError(msg)
    return float(str(value))


def _int_value(value: object, *, default: int) -> int:
    return default if value is None else int(float(str(value)))


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    rendered = str(value)
    if rendered in {"NaT", "nan", "<NA>"}:
        return None
    return date.fromisoformat(rendered[:10])


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    numeric = float(str(value))
    return numeric if math.isfinite(numeric) else None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value)
    return None if rendered in {"nan", "<NA>"} else rendered
