"""Tests for cross-cutting internal domain models."""

from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from swing_copilot.models import (
    DailyRunOptions,
    DailyRunResult,
    Position,
    RunMode,
    RunStatus,
    StepStatus,
)


class TestRunMode:
    def test_values(self):
        assert RunMode.LIVE.value == "live"
        assert RunMode.DRY_RUN.value == "dry_run"


class TestRunStatus:
    def test_values(self):
        assert {member.value for member in RunStatus} == {
            "running",
            "success",
            "degraded",
            "failed",
        }


class TestStepStatus:
    def test_values(self):
        assert {member.value for member in StepStatus} == {
            "success",
            "failed",
            "skipped",
        }


class TestPosition:
    def test_is_frozen(self):
        position = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=date(2026, 1, 5),
            entry_price=225.80,
            shares=10,
            stop_price=210.0,
            status="open",
            close_date=None,
            close_price=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            position.shares = 20  # type: ignore[misc]

    def test_defaults_for_open_position(self):
        position = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=date(2026, 1, 5),
            entry_price=225.80,
            shares=10,
            stop_price=210.0,
            status="open",
        )
        assert position.close_date is None
        assert position.close_price is None


class TestDailyRunOptions:
    def test_defaults(self):
        options = DailyRunOptions()
        assert options.as_of is None
        assert options.is_dry_run is False
        assert options.skip_text is False
        assert options.limit is None
        assert options.log_level is None

    def test_explicit_values(self):
        options = DailyRunOptions(
            as_of=date(2026, 7, 20),
            is_dry_run=True,
            skip_text=True,
            limit=5,
            log_level="ERROR",
        )
        assert options.as_of == date(2026, 7, 20)
        assert options.is_dry_run is True
        assert options.limit == 5
        assert options.log_level == "ERROR"


class TestDailyRunResult:
    def test_fields(self):
        run_id = uuid4()
        result = DailyRunResult(
            run_id=run_id,
            run_date=date(2026, 7, 20),
            status=RunStatus.SUCCESS,
            exit_code=0,
            report_path=None,
        )
        assert result.run_id == run_id
        assert result.exit_code == 0
        assert result.status is RunStatus.SUCCESS
        assert result.analysis_input_path is None

    def test_analysis_input_path_defaults_to_none_and_can_be_set(self):
        run_id = uuid4()
        path = Path("reports/2026-07-20/analysis_input.json")
        result = DailyRunResult(
            run_id=run_id,
            run_date=date(2026, 7, 20),
            status=RunStatus.SUCCESS,
            exit_code=0,
            analysis_input_path=path,
        )
        assert result.analysis_input_path == path
