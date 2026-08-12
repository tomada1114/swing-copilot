"""P8-124: the same-day adoption rule applied on the read side.

`collect` leaves a non-adopted same-day run's rows in place by design, so a
window read that skips this filter counts that date twice. The fixtures here
mirror the shape Issue #124's integration run hit: one `run_date` carrying two
runs, one of which `collect` announced it had skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from swing_copilot.retro.adoption import (
    UNRESOLVED_STARTED_AT,
    adopt_one_run_per_date,
    keep_adopted_rows,
)

EARLY_RUN = UUID("11111111-1111-1111-1111-111111111111")
LATE_RUN = UUID("22222222-2222-2222-2222-222222222222")
OTHER_DAY_RUN = UUID("33333333-3333-3333-3333-333333333333")
CONTESTED_DAY = date(2026, 7, 29)
QUIET_DAY = date(2026, 7, 30)
EARLY_START = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
LATE_START = datetime(2026, 7, 29, 18, 30, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Row:
    """The `run_id` / `as_of` pair every window row exposes."""

    run_id: UUID
    as_of: date
    symbol: str


class _FakeStartedAtSource:
    """Records which runs the filter actually asked about."""

    def __init__(self, started_at: dict[UUID, datetime | None]) -> None:
        self._started_at = started_at
        self.queried: list[UUID] = []

    def get_run_started_at(self, run_id: UUID) -> datetime | None:
        """Return the recorded `started_at`, remembering the lookup."""
        self.queried.append(run_id)
        return self._started_at.get(run_id)


def _resolved_source() -> _FakeStartedAtSource:
    return _FakeStartedAtSource({EARLY_RUN: EARLY_START, LATE_RUN: LATE_START})


def test_keep_adopted_rows_contested_date_drops_the_earlier_run() -> None:
    rows = (
        _Row(EARLY_RUN, CONTESTED_DAY, "AAA"),
        _Row(LATE_RUN, CONTESTED_DAY, "BBB"),
        _Row(LATE_RUN, CONTESTED_DAY, "CCC"),
    )

    kept = keep_adopted_rows(rows, _resolved_source())

    assert kept == (rows[1], rows[2])


def test_keep_adopted_rows_preserves_the_store_order() -> None:
    rows = (
        _Row(LATE_RUN, CONTESTED_DAY, "BBB"),
        _Row(OTHER_DAY_RUN, QUIET_DAY, "DDD"),
        _Row(EARLY_RUN, CONTESTED_DAY, "AAA"),
    )

    kept = keep_adopted_rows(rows, _resolved_source())

    assert [row.symbol for row in kept] == ["BBB", "DDD"]


def test_keep_adopted_rows_uncontested_window_asks_the_store_nothing() -> None:
    rows = (
        _Row(LATE_RUN, CONTESTED_DAY, "BBB"),
        _Row(OTHER_DAY_RUN, QUIET_DAY, "DDD"),
    )
    source = _resolved_source()

    kept = keep_adopted_rows(rows, source)

    assert kept == rows
    assert source.queried == []


def test_keep_adopted_rows_empty_window_returns_empty() -> None:
    source = _resolved_source()

    assert keep_adopted_rows((), source) == ()
    assert source.queried == []


def test_keep_adopted_rows_unresolved_started_at_loses_to_a_resolved_one() -> None:
    rows = (
        _Row(LATE_RUN, CONTESTED_DAY, "BBB"),
        _Row(EARLY_RUN, CONTESTED_DAY, "AAA"),
    )
    # LATE_RUN sorts later by run_id, so only `started_at` can demote it.
    source = _FakeStartedAtSource({EARLY_RUN: EARLY_START, LATE_RUN: None})

    kept = keep_adopted_rows(rows, source)

    assert kept == (rows[1],)


def test_adopt_one_run_per_date_all_unresolved_breaks_the_tie_on_run_id() -> None:
    adopted = adopt_one_run_per_date(
        ((CONTESTED_DAY, EARLY_RUN), (CONTESTED_DAY, LATE_RUN)),
        {EARLY_RUN: None, LATE_RUN: None},
    )

    assert adopted == frozenset({LATE_RUN})


def test_adopt_one_run_per_date_keeps_one_run_for_every_date() -> None:
    adopted = adopt_one_run_per_date(
        (
            (CONTESTED_DAY, EARLY_RUN),
            (CONTESTED_DAY, LATE_RUN),
            (QUIET_DAY, OTHER_DAY_RUN),
        ),
        {
            EARLY_RUN: EARLY_START,
            LATE_RUN: LATE_START,
            OTHER_DAY_RUN: datetime(2026, 7, 30, 9, 0, tzinfo=UTC),
        },
    )

    assert adopted == frozenset({LATE_RUN, OTHER_DAY_RUN})


def test_unresolved_started_at_sorts_below_every_real_timestamp() -> None:
    assert datetime(1, 1, 2, tzinfo=UTC) > UNRESOLVED_STARTED_AT
