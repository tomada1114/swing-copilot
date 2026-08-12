"""Detect runs whose qualitative analysis phase never finished (Issue #129).

`copilot-daily` runs the deterministic pipeline and stops once it has written
`reports/<run_date>/<run_id>/analysis_input.json`. A run is only *finished*
when the following `/swing-daily` skill writes `analysis_result.json` back
into the same directory. When that skill session dies partway, `runs.status`
stays `success`, the run directory still exists, and only
`analysis_result.json` is missing. A preflight that asks "does the previous
business day have a directory?" can never see this, because the directory is
created by `copilot-daily` itself.

The primary signal here is therefore the filesystem, not the `verdicts` table.
`verdicts` rows are written by `copilot-retro collect`, which archives run N's
verdicts during run N+1's daily execution -- so a *finished* newest run always
has zero `verdicts` rows, and a row-count predicate would flag it every time.

Being unfinished does not always mean something is recoverable, either. The
`reports/` tree also holds the leftovers of a same-day double start (the shape
Issue #118 now blocks at the door) and runs whose deterministic pipeline never
completed at all. `IncompleteRunKind` separates those, and only the actionable
kinds drive the CLI's non-zero exit.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.retro.collect import find_run_directories
from swing_copilot.storage.history_queries import get_run_statuses

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path
    from uuid import UUID

    from swing_copilot.retro.collect import RunDirectory
    from swing_copilot.storage.database import Database
    from swing_copilot.storage.history_queries import RunStatusRow

#: Exit code of `copilot-history incomplete` when at least one actionable
#: unfinished run exists. Distinct from 0 (nothing found) and from argparse's
#: 2 (usage error), so a preflight can branch on the status alone instead of
#: parsing the rendered table.
ANALYSIS_INCOMPLETE_EXIT_CODE = 3

#: `runs.status` values for which the deterministic pipeline did reach the
#: end, so the analysis phase's result should exist. `failed` and `running`
#: stopped earlier than that.
_ANALYSIS_EXPECTED_STATUSES = frozenset({"success", "degraded"})

#: Sort fallback for a run whose `started_at` cannot be resolved; it sorts as
#: the oldest, so resolvable runs of the same date are listed first.
_UNRESOLVED_STARTED_AT = datetime.min.replace(tzinfo=UTC)


class IncompleteRunKind(enum.Enum):
    """Why one run directory has no `analysis_result.json`."""

    #: The deterministic pipeline succeeded but no analysis result exists.
    #: This is Issue #129's actual failure mode.
    ANALYSIS_MISSING = "analysis_missing"
    #: Another run on the same `run_date` does have an analysis result
    #: (the same-day double start Issue #118 guards against).
    SAME_DAY_SUPERSEDED = "same_day_superseded"
    #: `runs.status` is `failed` or `running`: the run stopped before the
    #: analysis phase, which `runs.status` already reports on its own.
    PIPELINE_UNFINISHED = "pipeline_unfinished"
    #: A run directory exists under `reports/` with no matching `runs` row,
    #: meaning the database and the archive tree have diverged.
    RUN_ROW_MISSING = "run_row_missing"


_ACTIONABLE_KINDS = frozenset(
    {IncompleteRunKind.ANALYSIS_MISSING, IncompleteRunKind.RUN_ROW_MISSING}
)


@dataclass(frozen=True, slots=True)
class IncompleteRun:
    """One run archive that has `analysis_input.json` but no result."""

    run_date: date
    run_id: UUID
    path: Path
    kind: IncompleteRunKind
    run_status: str | None
    started_at: datetime | None
    completed_sibling_run_id: UUID | None

    @property
    def is_actionable(self) -> bool:
        """Whether this gap is worth re-running the analysis phase for.

        `SAME_DAY_SUPERSEDED` is not a gap at all -- that date's analysis
        survives in the sibling run -- and `PIPELINE_UNFINISHED` is already
        visible through `runs.status`. Both are still listed, but neither
        raises the exit code: a signal that can never go green again is
        operational noise rather than a warning.
        """
        return self.kind in _ACTIONABLE_KINDS


def find_incomplete_runs(
    database: Database, reports_root: Path, *, since: date | None = None
) -> tuple[IncompleteRun, ...]:
    """Scan `reports_root` for runs whose analysis phase never finished.

    Args:
        database: Shared DuckDB connection owner, read only.
        reports_root: The daily pipeline's output directory (`reports/`). A
            missing path yields an empty tuple rather than raising.
        since: When given, only `run_date`s on or after this date are
            considered. This is what lets a preflight ask about the previous
            business day alone, instead of re-reporting an old gap that can
            no longer be filled.

    Returns:
        Newest `run_date` first; within a date, latest `started_at` first
        (an unresolvable one sorts as the oldest), with the greater `run_id`
        string as the final tie-break. A directory without
        `analysis_input.json` is never included: that run never reached the
        analysis phase, and its failure is recorded in `runs.status` instead.
    """
    completed_by_date: dict[date, UUID] = {}
    candidates: list[RunDirectory] = []
    for run_directory in find_run_directories(reports_root):
        if (run_directory.path / ANALYSIS_RESULT_FILENAME).is_file():
            completed_by_date.setdefault(run_directory.run_date, run_directory.run_id)
        elif (run_directory.path / ANALYSIS_INPUT_FILENAME).is_file():
            candidates.append(run_directory)

    if since is not None:
        candidates = [run for run in candidates if run.run_date >= since]

    statuses = get_run_statuses(database, [run.run_id for run in candidates])
    incomplete = [
        _classify(run, statuses.get(run.run_id), completed_by_date.get(run.run_date))
        for run in candidates
    ]
    incomplete.sort(
        key=lambda run: (
            run.run_date,
            run.started_at or _UNRESOLVED_STARTED_AT,
            str(run.run_id),
        ),
        reverse=True,
    )
    return tuple(incomplete)


def _classify(
    run_directory: RunDirectory,
    status_row: RunStatusRow | None,
    completed_sibling_run_id: UUID | None,
) -> IncompleteRun:
    """Decide one directory's kind.

    A completed sibling wins over every other reason, because whatever else
    is true of this run, that date's analysis is not missing.
    """
    if completed_sibling_run_id is not None:
        kind = IncompleteRunKind.SAME_DAY_SUPERSEDED
    elif status_row is None:
        kind = IncompleteRunKind.RUN_ROW_MISSING
    elif status_row.status in _ANALYSIS_EXPECTED_STATUSES:
        kind = IncompleteRunKind.ANALYSIS_MISSING
    else:
        kind = IncompleteRunKind.PIPELINE_UNFINISHED
    return IncompleteRun(
        run_date=run_directory.run_date,
        run_id=run_directory.run_id,
        path=run_directory.path,
        kind=kind,
        run_status=None if status_row is None else status_row.status,
        started_at=None if status_row is None else status_row.started_at,
        completed_sibling_run_id=completed_sibling_run_id,
    )
