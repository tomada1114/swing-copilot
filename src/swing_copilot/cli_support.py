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

This module also owns `configure_cli_logging()`: stderr logging setup shared
by every `copilot-*` entry point that talks to an authenticated external
boundary and can therefore carry a secret into a `logger.exception` traceback
(`copilot-daily` and, since Issue #381, `copilot-retro`'s `export`/`prepare`,
via `EdgarClient`/`FinnhubNewsClient`). AGENTS.md: "Never log secrets. Redact
exception and audit fields." A CLI with no such boundary (e.g. `analysis/cli.py`,
`analysis/verify_cli.py`) configures its own plain `logging.basicConfig` instead
— it has nothing to redact, so sharing this module's `SecretRedactionFilter`
would buy it nothing.

At runtime this module imports nothing from `swing_copilot` beyond `Secrets`
(under `TYPE_CHECKING` only, for `configure_cli_logging`'s type hint): which
exceptions `run_cli` catches, and how their message reads, belong to the
calling CLI; which secrets get redacted belongs to it too — the caller passes
its own `Secrets` in.
"""

from __future__ import annotations

import logging
import sys
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from swing_copilot.config import Secrets


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


#: `--log-level` choices shared by every entry point that exposes the flag.
LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


class SecretRedactionFilter(logging.Filter):
    """Replace configured secret values before a log record reaches stderr."""

    def __init__(self, secrets: Iterable[str | None]) -> None:
        """Build the filter from every configured (non-`None`) secret value.

        Args:
            secrets: Candidate secret values; `None`/empty entries are
                dropped. Sorted longest-first so a secret that is a prefix of
                another is never redacted only partially.
        """
        super().__init__()
        self._secrets = tuple(
            sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        )

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact every configured secret from the record in place.

        Args:
            record: The log record about to reach a handler.

        Returns:
            Always `True` -- this filter only redacts, it never suppresses.
        """
        if not self._secrets:
            return True

        record.msg = self._redact(record.getMessage())
        record.args = ()
        if record.exc_info is not None:
            formatted = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self._redact(formatted)
            record.exc_info = None
        return True

    def _redact(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted


def configure_cli_logging(secrets: Secrets, *, level: str | None = None) -> None:
    """Configure stderr logging levels and redaction for configured secrets.

    Without this, every `logger.exception(...)` in the calling package falls
    through to `logging.lastResort`: WARNING-and-above only, unformatted, and
    -- more importantly for a CLI that authenticates to an external API --
    with no redaction, so a secret embedded in an exception's message or
    traceback (e.g. an API key `httpx` puts in a URL-carrying
    `HTTPStatusError`) would reach stderr verbatim.

    Args:
        secrets: The loaded `Secrets`. `finnhub_api_key`, `fred_api_key`, and
            `discord_webhook_url` are redacted from every record this call's
            handlers see, when configured; a CLI that never reads one of them
            (e.g. `copilot-retro` never touches FRED or Discord) simply never
            has it to redact. `edgar_identity` is deliberately excluded: per
            SEC EDGAR's fair-access policy it is a contact identity meant to
            appear in outgoing requests, not a bearer credential, so it is not
            treated as a secret here.
        level: An explicit `--log-level` name, or `None` for the default
            split: `WARNING` for the root logger (third-party noise), `INFO`
            for the `swing_copilot` logger (this project's own progress).
    """
    root_level = LOG_LEVELS[level] if level is not None else logging.WARNING
    application_level = LOG_LEVELS[level] if level is not None else logging.INFO
    logging.basicConfig(
        level=root_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(root_level)
    logging.getLogger("swing_copilot").setLevel(application_level)
    redaction_filter = SecretRedactionFilter(
        (secrets.finnhub_api_key, secrets.fred_api_key, secrets.discord_webhook_url)
    )
    for handler in logging.root.handlers:
        handler.addFilter(redaction_filter)
