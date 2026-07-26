"""Cross-cutting internal domain values (frozen dataclasses and enums).

Domain values that belong to a single module (``UniverseMember``,
``BarFetchResult``, ``Candidate``, ``SignalHit``, ``RiskAssessment``, ...) are
defined in that module instead of here. This module holds only the values
shared across module boundaries (run state, positions) per
``docs/04_detailed_design.md`` 4.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime
    from pathlib import Path
    from typing import Literal
    from uuid import UUID

    from swing_copilot.report.daily_brief import DailyBrief

    PositionStatus = Literal["open", "closed"]
else:
    PositionStatus = str


class RunMode(Enum):
    """Whether a daily batch run touches live external resources."""

    LIVE = "live"
    DRY_RUN = "dry_run"


class RunStatus(Enum):
    """Overall status of a ``runs`` row."""

    RUNNING = "running"
    SUCCESS = "success"
    DEGRADED = "degraded"
    FAILED = "failed"


class StepStatus(Enum):
    """Status of a single ``run_steps`` row."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Position:
    """A single open or closed (paper or live) trading position."""

    position_id: UUID
    symbol: str
    is_paper: bool
    entry_date: date
    entry_price: float
    shares: int
    status: PositionStatus
    stop_price: float | None = None
    close_date: date | None = None
    close_at: datetime | None = None
    close_price: float | None = None
    # P1-06: why the position was closed. Input values accepted by
    # `PaperJournal.close_position()` are exactly {stop_loss, target,
    # time_stop, manual, other}; "unknown" is a migration-only sentinel
    # backfilled onto closed rows that predate this column (never a valid
    # close_position() input). `None` means the position is still open.
    exit_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DailyRunOptions:
    """Parsed CLI options for ``uv run copilot-daily``."""

    as_of: date | None = None
    is_dry_run: bool = False
    skip_text: bool = False
    skip_llm: bool = False
    limit: int | None = None
    strategy_key: str = "default"
    log_level: str | None = None


@dataclass(frozen=True, slots=True)
class DailyRunResult:
    """Outcome of one ``run_daily()`` invocation."""

    run_id: UUID
    run_date: date
    status: RunStatus
    exit_code: int
    report_path: Path | None = None
    brief: DailyBrief | None = None
