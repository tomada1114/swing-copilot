"""Thin `runs`-row seeding helper built on `StateStore.insert_run()` (Issue #398).

Eleven test modules used to hand-write `INSERT INTO runs (...)` straight
against `state_store._database`, each carrying its own `# noqa: SLF001` for
reaching into the private attribute. Issue #395 added
`StateStore.insert_run()` precisely so a test could seed a fully specified
historical `runs` row through the public API instead; `seed_run()` here is
just a caller-convenience wrapper around it (default `status`/`mode`/
`config_hash`, and a `started_at` default so most call sites don't need to
compute one).
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import TYPE_CHECKING
from uuid import UUID

from swing_copilot.models import RunMode, RunStatus

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.storage.state_store import StateStore

#: A placeholder fingerprint. No test seeded through `seed_run()` asserts on
#: the literal value, so callers rarely need to override it.
_DEFAULT_CONFIG_HASH = "cfg"


def seed_run(  # noqa: PLR0913
    state_store: StateStore,
    run_id: UUID | str,
    run_date: date,
    *,
    status: RunStatus | str = RunStatus.SUCCESS,
    mode: RunMode | str = RunMode.LIVE,
    config_hash: str = _DEFAULT_CONFIG_HASH,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    """Seed a minimal `runs` row via `StateStore.insert_run()`.

    Args:
        state_store: Store to seed into; its schema must already be
            initialized.
        run_id: The row's identity, as a `UUID` or its string form.
        run_date: Evaluation market date for the seeded run.
        status: Lifecycle status; defaults to a finished, successful run.
        mode: `live` or `dry_run`; defaults to `live`.
        config_hash: Fingerprint stored on the row.
        started_at: When the run started; defaults to `run_date` at 18:00
            UTC, mirroring an ordinary same-day run.
        finished_at: When the run finished, or `None` for a still-running row.
    """
    resolved_run_id = run_id if isinstance(run_id, UUID) else UUID(run_id)
    resolved_status = status if isinstance(status, RunStatus) else RunStatus(status)
    resolved_mode = mode if isinstance(mode, RunMode) else RunMode(mode)
    resolved_started_at = (
        started_at
        if started_at is not None
        else datetime.combine(run_date, time(18, 0), tzinfo=UTC)
    )
    state_store.insert_run(
        resolved_run_id,
        run_date,
        resolved_mode,
        config_hash,
        status=resolved_status,
        started_at=resolved_started_at,
        finished_at=finished_at,
    )
