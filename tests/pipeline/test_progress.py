"""Contracts for terminal progress reporting."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from swing_copilot.pipeline.progress import NullProgressReporter, ProgressReporter


def test_non_tty_prints_only_completed_step_in_a_deterministic_format() -> None:
    output = StringIO()
    reporter = ProgressReporter(Console(file=output, force_terminal=False))

    reporter.step_started(3, 8, "3_screening")
    reporter.substep(4, 10, "fundamentals")
    reporter.step_finished(3, 8, "3_screening", "ok", 2.1)

    assert output.getvalue() == "[3/8] 3_screening ok (2.1s)\n"


def test_tty_progress_smoke_does_not_raise() -> None:
    reporter = ProgressReporter(Console(file=StringIO(), force_terminal=True))

    reporter.step_started(2, 8, "2_fundamentals")
    reporter.substep(1, 3, "fundamentals")
    reporter.step_finished(2, 8, "2_fundamentals", "ok", 0.1)


def test_null_reporter_is_silent() -> None:
    reporter = NullProgressReporter()

    reporter.step_started(1, 8, "1_prices")
    reporter.substep(1, 3, "fundamentals")
    reporter.step_finished(1, 8, "1_prices", "ok", 0.1)
