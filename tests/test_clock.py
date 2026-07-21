"""Tests for the Clock port (docs/04_detailed_design.md 2.2)."""

from __future__ import annotations

from datetime import date

from swing_copilot.clock import SystemClock


def test_system_clock_returns_a_date():
    clock = SystemClock()
    assert isinstance(clock.today(), date)
    assert clock.now().tzinfo is not None
