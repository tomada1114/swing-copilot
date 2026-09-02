"""Persistence for the retrospective's own narrations (Issue #189).

Until this table existed, a `failure_class` survived only inside
`reports/retro/<as_of>/retro_report.md` -- a gitignored artifact -- so design
§8.1's L2 qualitative gate ("the same `failure_class` five times across the
last three retrospectives") could not be evaluated by anything but memory.
`copilot-retro ingest` now writes each verified narration here, and
`copilot-retro export` reads the cross-tab straight back out, which is what
turns the gate from something the skill counts into something it merely reads.

One retrospective is one logical write: the session row and every narration it
verified commit together or not at all, and re-ingesting the same `retro_as_of`
*replaces* its narrations rather than merging into them -- a corrected result
that drops a symbol must not leave the old reading behind (AGENTS.md's
snapshot-replacement invariant).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from swing_copilot.storage.database import fetch_records
from swing_copilot.storage.json_guard import dumps_safe

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date, datetime
    from uuid import UUID

    from swing_copilot.storage.database import Database


@dataclass(frozen=True, slots=True)
class RetroSessionRecord:
    """One ingested retrospective's identity and size."""

    retro_as_of: date
    window_start: date
    #: The dossier's `input_digest`, so a session can be traced back to the
    #: exact evidence set it answered.
    input_digest: str
    #: Wall-clock provenance carried over from the dossier, never a cutoff.
    generated_at: datetime
    outcome_count: int
    proposal_count: int


@dataclass(frozen=True, slots=True)
class RetroNarrationRecord:
    """One verified re-reading of one surprise symbol.

    `run_id`/`symbol` are resolved from the exported dossier, not echoed back
    from the skill's answer: code-owned metadata is never taken on trust from
    an untrusted result.
    """

    retro_as_of: date
    surprise_id: str
    run_id: UUID
    symbol: str
    failure_class: str
    narrative: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FailureClassCount:
    """How often one `failure_class` recurred across a run of sessions."""

    failure_class: str
    #: Narrations carrying this class across the sessions counted.
    count: int
    #: How many distinct retrospectives contributed at least one of them.
    session_count: int


@dataclass(frozen=True, slots=True)
class FailureClassHistory:
    """The trailing cross-tab design §8.1's L2 gate is decided on."""

    #: The `retro_as_of` values counted, newest first.
    sessions: tuple[date, ...]
    counts: tuple[FailureClassCount, ...]


_UPSERT_SESSION = """
INSERT INTO retro_sessions (
    retro_as_of, window_start, input_digest, generated_at,
    outcome_count, proposal_count
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (retro_as_of) DO UPDATE SET
    window_start   = EXCLUDED.window_start,
    input_digest   = EXCLUDED.input_digest,
    generated_at   = EXCLUDED.generated_at,
    outcome_count  = EXCLUDED.outcome_count,
    proposal_count = EXCLUDED.proposal_count
"""

#: The trailing sessions are selected inside the query rather than
#: interpolated as an `IN (?, ?, ?)` list, so the statement stays static.
_COUNT_FAILURE_CLASSES = """
SELECT failure_class, count(*), count(DISTINCT retro_as_of)
FROM retro_narrations
WHERE retro_as_of IN (
    SELECT retro_as_of FROM retro_sessions
    WHERE retro_as_of <= ?
    ORDER BY retro_as_of DESC
    LIMIT ?
)
GROUP BY failure_class
ORDER BY count(*) DESC, failure_class
"""

_INSERT_NARRATION = """
INSERT INTO retro_narrations (
    retro_as_of, surprise_id, run_id, symbol, failure_class,
    narrative, evidence_refs_json
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def replace_retro_session(
    database: Database,
    session: RetroSessionRecord,
    narrations: Sequence[RetroNarrationRecord],
) -> None:
    """Record one retrospective, replacing any earlier reading of that date.

    Args:
        database: Shared DuckDB connection owner.
        session: The session row to correction-upsert.
        narrations: Every verified narration of that session. An empty
            sequence still writes the session row and clears the date's
            previous narrations -- a retrospective that verified nothing is a
            fact, not a reason to leave a stale reading in place.

    Raises:
        ValueError: A narration belongs to a different `retro_as_of` than the
            session. Nothing is written in that case.
    """
    mismatched = next(
        (row for row in narrations if row.retro_as_of != session.retro_as_of), None
    )
    if mismatched is not None:
        msg = (
            "every narration must belong to the session's retro_as_of: "
            f"{mismatched.surprise_id!r} carries {mismatched.retro_as_of.isoformat()}"
        )
        raise ValueError(msg)

    with database.transaction() as conn:
        conn.execute(
            _UPSERT_SESSION,
            [
                session.retro_as_of,
                session.window_start,
                session.input_digest,
                session.generated_at,
                session.outcome_count,
                session.proposal_count,
            ],
        )
        conn.execute(
            "DELETE FROM retro_narrations WHERE retro_as_of = ?",
            [session.retro_as_of],
        )
        for narration in narrations:
            conn.execute(
                _INSERT_NARRATION,
                [
                    narration.retro_as_of,
                    narration.surprise_id,
                    str(narration.run_id),
                    narration.symbol,
                    narration.failure_class,
                    narration.narrative,
                    dumps_safe(list(narration.evidence_refs)),
                ],
            )


def get_failure_class_history(
    database: Database, as_of: date, session_limit: int
) -> FailureClassHistory:
    """Cross-tab the `failure_class` counts of the trailing retrospectives.

    Point-in-time like every other read here: only sessions ingested with
    `retro_as_of <= as_of` are counted, boundary inclusive, so re-exporting an
    old retrospective reproduces the number it saw rather than today's.

    Args:
        database: Shared DuckDB connection owner.
        as_of: The retrospective cutoff.
        session_limit: How many trailing sessions to count (design §8.1's
            "last three retrospectives").

    Returns:
        The sessions counted (newest first) and one row per `failure_class`
        that appeared in them, ordered by descending count then class name so
        the dossier is byte-reproducible.
    """
    with database.connect() as conn:
        sessions = tuple(
            row[0]
            for row in conn.execute(
                "SELECT retro_as_of FROM retro_sessions WHERE retro_as_of <= ? "
                "ORDER BY retro_as_of DESC LIMIT ?",
                [as_of, session_limit],
            ).fetchall()
        )
        if not sessions:
            return FailureClassHistory(sessions=(), counts=())
        rows = conn.execute(_COUNT_FAILURE_CLASSES, [as_of, session_limit]).fetchall()
    return FailureClassHistory(
        sessions=sessions,
        counts=tuple(
            FailureClassCount(
                failure_class=row[0], count=int(row[1]), session_count=int(row[2])
            )
            for row in rows
        ),
    )


def get_retro_narrations(
    database: Database, retro_as_of: date
) -> tuple[RetroNarrationRecord, ...]:
    """Read back one retrospective's narrations, ordered by `surprise_id`."""
    with database.connect() as conn:
        records = fetch_records(
            conn,
            "SELECT retro_as_of, surprise_id, run_id, symbol, failure_class, "
            "narrative, evidence_refs_json FROM retro_narrations "
            "WHERE retro_as_of = ? ORDER BY surprise_id",
            [retro_as_of],
        )
    return tuple(_narration(record) for record in records)


def _narration(record: Mapping[str, object]) -> RetroNarrationRecord:
    """Rebuild a `RetroNarrationRecord` from a row, read by column name."""
    return RetroNarrationRecord(
        retro_as_of=cast("date", record["retro_as_of"]),
        surprise_id=str(record["surprise_id"]),
        run_id=cast("UUID", record["run_id"]),
        symbol=str(record["symbol"]),
        failure_class=str(record["failure_class"]),
        narrative=str(record["narrative"]),
        evidence_refs=tuple(json.loads(cast("str", record["evidence_refs_json"]))),
    )
