"""Domain error to `SystemExit` conversion, shared by the console scripts.

Every `copilot-*` entry point used to hand-write the same three lines: catch
the domain error this step can raise, put its message where the operator (or
the calling skill) will see it, and exit with this command's code. The
duplication mattered because the *convention* is a contract — an exit code the
`swing-daily` skill branches on, a message a human reads — and eleven copies of
a contract drift.

`run_cli()` owns the conversion; an `ExitPolicy` states the convention. The
commands themselves stay separate: each is a stable, skill-facing entry point,
and consolidating them is explicitly not the goal (Issue #193).

This module imports nothing from `swing_copilot`: which exceptions are caught,
and how their message reads, belong to the calling CLI.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def _write_stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    """How one CLI step turns a caught domain error into `SystemExit`.

    Attributes:
        errors: Exception types this step converts. Anything else propagates,
            so a programming error still surfaces as a traceback.
        code: Exit status. `None` means "raise `SystemExit(message)`", the
            argparse convention: the interpreter prints the message to stderr
            and exits `1`.
        format_message: Renders the operator-facing message. Defaults to the
            exception's own text.
        report: Where the message goes when `code` is set. Defaults to stderr;
            pass a logger method for the commands that report through logging.
    """

    errors: tuple[type[Exception], ...]
    code: int | None = None
    format_message: Callable[[Exception], str] = str
    report: Callable[[str], None] = field(default=_write_stderr)


def run_cli[T](body: Callable[[], T], policy: ExitPolicy) -> T:
    """Run one CLI step, converting its domain errors under `policy`.

    Args:
        body: The step to run. Its return value is passed through, so a value
            the rest of `main()` needs survives the conversion.
        policy: The command's exit-code and message convention.

    Returns:
        Whatever `body` returned.

    Raises:
        SystemExit: `body` raised one of `policy.errors`.
    """
    try:
        return body()
    except policy.errors as exc:
        message = policy.format_message(exc)
        if policy.code is None:
            raise SystemExit(message) from exc
        policy.report(message)
        raise SystemExit(policy.code) from exc
