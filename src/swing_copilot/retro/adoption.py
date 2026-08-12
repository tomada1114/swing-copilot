"""Which run a `run_date` with more than one run is represented by (P8-119).

`collect` resolves same-day duplicates before writing, but deliberately leaves
a run it does not adopt untouched: "A run this scan does not adopt is never
touched -- its previously collected rows, if any, are left exactly as they
were." That makes the rule a *read-side* obligation as much as a write-side
one. A loser whose rows predate the guard (or predate #118 closing the door on
same-day reruns) stays in `verdicts` / `verdict_outcomes` forever, so any
window read that does not re-apply the rule counts that day twice.

Issue #124's integration run showed the gap concretely: `collect` reported
"2026-07-29: run a8584328... は同日の重複のため収集をスキップ", yet
`verdict_mix` still reported 10 runs / 78 verdicts because
`get_verdicts_in_window` is a plain date-range read. The loser's 4 verdicts
and 4 outcomes were in every window aggregate.

The rule itself lives here so `collect` and the window readers cannot drift:
latest `runs.started_at` wins, ties break on the greater `run_id` string for
determinism, and a `run_date` with a single candidate is adopted
unconditionally (its `started_at` is never consulted, so an unresolvable one
cannot drop it).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import date
    from uuid import UUID

#: Sorts below every real timestamp, so a run whose `started_at` cannot be
#: resolved loses to one that can rather than winning by accident.
UNRESOLVED_STARTED_AT = datetime.min.replace(tzinfo=UTC)


class DatedRun(Protocol):
    """A row that names the run it came from and the date that run covers."""

    @property
    def run_id(self) -> UUID:
        """The run that produced this row."""
        ...  # pragma: no cover

    @property
    def as_of(self) -> date:
        """The run date this row is attributed to."""
        ...  # pragma: no cover


class StartedAtSource(Protocol):
    """The `runs.started_at` lookup the tie-break needs."""

    def get_run_started_at(self, run_id: UUID) -> datetime | None:
        """Return when `run_id` started, or `None` when unresolvable."""
        ...  # pragma: no cover


def adopt_one_run_per_date(
    candidates: Iterable[tuple[date, UUID]],
    started_at_by_run_id: Mapping[UUID, datetime | None],
) -> frozenset[UUID]:
    """Return the single `run_id` to keep for each `run_date`.

    Args:
        candidates: `(run_date, run_id)` pairs, repeats allowed -- callers
            pass row-level pairs rather than pre-grouping.
        started_at_by_run_id: `runs.started_at` per candidate `run_id`;
            `None` for one that could not be resolved.

    Returns:
        Exactly one `run_id` per distinct `run_date` present in `candidates`.
    """
    by_date: dict[date, set[UUID]] = defaultdict(set)
    for run_date, run_id in candidates:
        by_date[run_date].add(run_id)
    return frozenset(
        max(
            run_ids,
            key=lambda run_id: (
                started_at_by_run_id.get(run_id) or UNRESOLVED_STARTED_AT,
                str(run_id),
            ),
        )
        for run_ids in by_date.values()
    )


def keep_adopted_rows[RowT: DatedRun](
    rows: Sequence[RowT], started_at_source: StartedAtSource
) -> tuple[RowT, ...]:
    """Drop rows belonging to a same-day run `collect` would not have adopted.

    A window with no duplicate date returns its rows unchanged and asks the
    store for nothing, so the common case costs no extra query.

    Args:
        rows: Window rows carrying `run_id` and `as_of`, in the store's order.
        started_at_source: Resolver for `runs.started_at`, consulted only for
            the runs on a date that actually has more than one.

    Returns:
        `rows` in their original order, minus every row whose run lost its
        date.
    """
    run_ids_by_date: dict[date, set[UUID]] = defaultdict(set)
    for row in rows:
        run_ids_by_date[row.as_of].add(row.run_id)
    contested = {
        run_date: run_ids
        for run_date, run_ids in run_ids_by_date.items()
        if len(run_ids) > 1
    }
    if not contested:
        return tuple(rows)

    started_at_by_run_id = {
        run_id: started_at_source.get_run_started_at(run_id)
        for run_ids in contested.values()
        for run_id in run_ids
    }
    adopted = adopt_one_run_per_date(
        (
            (run_date, run_id)
            for run_date, run_ids in contested.items()
            for run_id in run_ids
        ),
        started_at_by_run_id,
    )
    return tuple(
        row for row in rows if row.as_of not in contested or row.run_id in adopted
    )
