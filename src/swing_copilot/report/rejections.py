"""`rejections.json`: the run directory's full "what did not make it" record.

DuckDB's `screening_rejections` already holds the symbol-level detail, but the
run directory only ever kept per-reason *counts* (inside `report_context.json`),
so reading why one specific symbol disappeared meant querying the database.
Worse, a symbol that passed every filter and signal and was then cut by
`candidate_limit` was recorded nowhere at all: it is not a rejection (nothing
rejected it) and it is not a candidate.

This module writes both, side by side, as a standalone run artifact:

* `rejections` — one entry per classified `RejectionRecord`.
* `truncated_by_candidate_limit` — the near-misses the ledger cannot hold,
  because `screening_rejections.reason_code` is a closed enum guarded by a DB
  CHECK constraint and truncation is a configuration cap, not a verdict.

It is deliberately *not* part of the `analysis_input.json` /
`analysis_result.json` / `report_context.json` digest-bound audit trio: nothing
reads it back, so it carries no digest and its schema can evolve without
invalidating archived runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.analysis.export import write_json_atomically

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path
    from uuid import UUID

    from swing_copilot.screening.base import RejectionRecord, TruncatedCandidate

REJECTIONS_FILENAME = "rejections.json"
REJECTIONS_SCHEMA_VERSION = "rejections-v1"


@dataclass(frozen=True, slots=True)
class RejectionsArtifact:
    """One run's complete non-candidate record."""

    run_id: UUID
    as_of: date
    strategy_key: str
    rejections: Sequence[RejectionRecord]
    truncated: Sequence[TruncatedCandidate]


def write_rejections(artifact: RejectionsArtifact, destination_dir: Path) -> Path:
    """Write `rejections.json` into `destination_dir` via atomic replacement.

    Args:
        artifact: The run's rejections and `candidate_limit` truncations.
        destination_dir: The run's dedicated artifact directory (the same
            place `analysis_input.json` is written to).

    Returns:
        The written file's path.

    Raises:
        OSError: Writing or replacing failed; the previous file is preserved
            and the temporary artifact is removed.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / REJECTIONS_FILENAME
    write_json_atomically(destination, _payload(artifact))
    return destination


def _payload(artifact: RejectionsArtifact) -> dict[str, object]:
    """Build the deterministic JSON body.

    Rejections are sorted by symbol and truncations by rank so a rerun of the
    same `as_of` produces a byte-identical file, which is what makes two run
    directories diffable at all.
    """
    return {
        "schema_version": REJECTIONS_SCHEMA_VERSION,
        "run_id": str(artifact.run_id),
        "as_of": artifact.as_of.isoformat(),
        "strategy_key": artifact.strategy_key,
        "rejections": [
            {
                "symbol": record.symbol,
                "stage": record.stage.value,
                "reason_code": record.reason_code.value,
                "detail": dict(record.detail),
            }
            for record in sorted(artifact.rejections, key=lambda item: item.symbol)
        ],
        "truncated_by_candidate_limit": [
            {
                "symbol": item.symbol,
                "rank": item.rank,
                "score": item.score,
                "score_breakdown": dict(item.score_breakdown),
                "execution_state": item.execution_state,
                "execution_distance": item.execution_distance,
            }
            for item in sorted(artifact.truncated, key=lambda item: item.rank)
        ],
    }
