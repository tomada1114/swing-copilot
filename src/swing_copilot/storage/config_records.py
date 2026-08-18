"""The `config_versions` ledger: what a `runs.config_hash` stood for (#189).

`runs.config_hash` is a one-way fingerprint of the whole effective run
configuration. Until this table existed, editing `config/settings.yaml` made
every earlier run's settings unrecoverable, so "did the numbers move because
the configuration moved" was permanently unanswerable -- and, unlike a metric,
it cannot be recomputed later from data that was never written down.

The daily runner upserts one row per configuration it observes.
`sections_json` holds only `config.CONFIG_SNAPSHOT_SECTIONS`, and
`snapshot_hash` is their digest, so two configurations differing solely in
delivery plumbing share a `snapshot_hash` and stay one comparison group.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from swing_copilot.storage.json_guard import dumps_safe

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date
    from uuid import UUID

    from swing_copilot.storage.database import Database


@dataclass(frozen=True, slots=True)
class ConfigVersionRecord:
    """One observed configuration, keyed by its `runs.config_hash`."""

    config_hash: str
    first_seen_run_date: date
    #: Digest of `sections` alone: the proposal-relevant subset.
    snapshot_hash: str
    # Any: the dumped settings sections are arbitrary JSON values.
    sections: Mapping[str, Any]


#: `first_seen_run_date` moves only backwards. A rerun of an older `run_date`
#: under an unchanged configuration is a correction of when that configuration
#: was first seen, whereas today's run seeing it again is not -- so `least` is
#: the correction, and `DO NOTHING` would silently keep the wrong first date.
#: The payload columns are refreshed rather than left alone so a row written by
#: an earlier snapshot definition is corrected in place.
_UPSERT_CONFIG_VERSION = """
INSERT INTO config_versions (
    config_hash, first_seen_run_date, snapshot_hash, sections_json
) VALUES (?, ?, ?, ?)
ON CONFLICT (config_hash) DO UPDATE SET
    first_seen_run_date = least(
        config_versions.first_seen_run_date, EXCLUDED.first_seen_run_date
    ),
    snapshot_hash = EXCLUDED.snapshot_hash,
    sections_json = EXCLUDED.sections_json
"""


def upsert_config_version(database: Database, record: ConfigVersionRecord) -> None:
    """Record the configuration a run executed under, correcting in place.

    Args:
        database: Shared DuckDB connection owner.
        record: The observed configuration.
    """
    with database.connect() as conn:
        conn.execute(
            _UPSERT_CONFIG_VERSION,
            [
                record.config_hash,
                record.first_seen_run_date,
                record.snapshot_hash,
                dumps_safe(dict(record.sections)),
            ],
        )


def get_config_versions(database: Database) -> tuple[ConfigVersionRecord, ...]:
    """Read the whole ledger, oldest configuration first."""
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT config_hash, first_seen_run_date, snapshot_hash, sections_json "
            "FROM config_versions ORDER BY first_seen_run_date, config_hash"
        ).fetchall()
    return tuple(
        ConfigVersionRecord(
            config_hash=row[0],
            first_seen_run_date=row[1],
            snapshot_hash=row[2],
            sections=json.loads(row[3]),
        )
        for row in rows
    )


def get_run_config_hashes(database: Database, as_of: date) -> dict[UUID, str]:
    """Map every visible run to the configuration it executed under.

    Deliberately not windowed on the retrospective's own dates: an outcome
    matures 5 or 20 sessions *after* the run it judges, so the runs behind one
    window's outcomes sit well before that window's start. The cutoff is still
    point-in-time -- a run dated after `as_of` was not visible then and is
    excluded, boundary inclusive.

    Args:
        database: Shared DuckDB connection owner.
        as_of: Inclusive upper bound on `run_date`.

    Returns:
        `run_id` to `config_hash` for every run at or before the cutoff.
    """
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT run_id, config_hash FROM runs WHERE run_date <= ?", [as_of]
        ).fetchall()
    return {row[0]: row[1] for row in rows}
