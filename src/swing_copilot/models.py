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


class DataTier(Enum):
    """Trust tier of the market-data basis used for one run."""

    PROTOTYPE = "prototype"


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
    # P1-06 (historical): why the position was closed. The writer
    # (`PaperJournal`) was removed in 2026-08 along with real-trade
    # recording; `backtest/policy.py::as_position()` is the only remaining
    # constructor and never sets this, so it is always `None`.
    exit_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DailyRunOptions:
    """Parsed CLI options for ``uv run copilot-daily``."""

    as_of: date | None = None
    is_dry_run: bool = False
    skip_text: bool = False
    limit: int | None = None
    strategy_key: str = "default"
    log_level: str | None = None
    # P8-118: bypasses the same-day rerun guard (a prior `status='success'`
    # run already exists for the resolved `run_date`).
    allow_same_day_rerun: bool = False
    # Issue #372: where `main()` writes this run's terminal outcome (JSON,
    # outside `reports/<run_date>/<run_id>/`) so `scripts/check_daily_complete.py`
    # can tell "the pipeline never started" from "it started and legitimately
    # aborted". `None` (the default) writes nothing.
    outcome_file: Path | None = None


@dataclass(frozen=True, slots=True)
class DailyRunResult:
    """Outcome of one ``run_daily()`` invocation."""

    run_id: UUID
    run_date: date
    status: RunStatus
    exit_code: int
    report_path: Path | None = None
    brief: DailyBrief | None = None
    # Absolute path of this run's exported `analysis_input.json`, or `None`
    # when there was nothing to analyze (no candidates or no collected text).
    analysis_input_path: Path | None = None
    provider_name: str = "yfinance"
    data_tier: DataTier = DataTier.PROTOTYPE
    # Source boundaries that could not provide data for this run. This is
    # presentation/audit metadata only; per-step details remain in `run_steps`.
    missing_sources: tuple[str, ...] = ()
