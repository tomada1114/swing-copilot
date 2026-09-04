---
name: designing-errors
description: >
  Covers the `SwingCopilotError` hierarchy, when a new subclass earns its place, the
  `PreflightAbort`/`PreflightAbortReason` closed vocabulary and its exit-code-2
  convention, `cli_support.run_cli`/`ExitPolicy`, `logging.exception()` vs
  `logger.error(str(e))`, and `configure_cli_logging`'s secret redaction. Use when adding
  a `*Error` class, raising or catching `SwingCopilotError`, wiring a new `copilot-*`
  entry point's error handling, deciding a `PREFLIGHT_ABORT[...]` reason, or reviewing a
  `noqa: N818` naming deviation.
---

# Designing Errors

**Owns:** the shape of the error hierarchy and how a domain error becomes a process
exit. **Does not own:** general module style (`writing-python`), which CLI a new entry
point becomes or its console-script contract (`public-api-contract`), how a caller
diagnoses a failed scheduled run after the fact (`diagnosing-daily-runs`).

## Every error derives from `SwingCopilotError`

`src/swing_copilot/exceptions.py` defines the one package-level base; every `*Error` in
`src/` and `scripts/` derives from it, including through multi-level inheritance —
mechanically enforced by `tests/test_quality_contracts.py`'s
`test_every_error_class_derives_from_the_package_base`, which walks the AST class graph
of every module under `src/` and `scripts/` and fails on a class ending in `Error` whose
base chain never reaches `SwingCopilotError`. This existed as a rule before it was a
test: `LatestMarkdownUpdateError`, `DataSyncError`, and `scripts/check_daily_complete.py`'s
`IncompleteRunError` all derived straight from a builtin (`OSError`, `RuntimeError`) and
silently escaped any `except SwingCopilotError` handler written to catch every domain
failure, until Issue #394 fixed them.

A caller-visible base matters because it is what makes "catch every domain failure this
package can raise" expressible at all — `run_cli`'s `ExitPolicy.errors` tuple, a
`swing-daily` skill's error handling, or a test's `pytest.raises(SwingCopilotError)` all
depend on the base being real and universal, not "usually true".

Multiple inheritance is fine when a caller needs to catch along a builtin axis too:
`report/markdown_report.py`'s `LatestMarkdownUpdateError(SwingCopilotError, OSError)`
responds to both an existing `except OSError` and a new `except SwingCopilotError`.

## When a new subclass earns its place

A subclass is justified when some caller branches on it — a different exit code, a
different retry decision, a different user-facing message. `ConfigError` and
`StorageSchemaError` each map to distinct `ExitPolicy` handling in the CLIs that raise
them. Reusing `SwingCopilotError` directly, or an existing sibling subclass, is correct
when nothing downstream actually distinguishes the new failure from an existing one — a
subclass nobody catches by name is noise in the hierarchy, not documentation.

## `PreflightAbort`: the deliberate exception to "never use exceptions for control flow"

`exceptions.py`'s own docstring states the reasoning; do not restate it as a fresh rule —
quote it when explaining the pattern:

> Raised to intentionally abort a run before any state is recorded. `main()` converts
> this to exit code 2 — distinct from 0 (success) and 1 (failure): continuing would not
> fail, it would just be pointless (P8-117). Not named `PreflightAbortError` — this is an
> intentional control-flow signal, not a failure, and #118 (which raises the same
> exception from a later preflight condition) depends on this exact name.

Two consequences follow directly from that:

- Exit code `2` means "the run stopped on purpose before writing anything" —
  `same_day_rerun` and `no_trading_day` are legitimate stops a caller should treat as
  green; `price_fetch_failed` is exit `2` too, but is a genuine failure the caller must
  not summarize as a clean day. Never assume exit `2` alone means "already ran" — the
  stderr reason tag is what the caller actually branches on.
- The class keeps the name `Abort`, not `AbortError`, with
  `# noqa: N818 - named "Abort" per P8-117's design` suppressing pep8-naming's
  "exception name should end in Error" rule. This is the one accepted deviation in this
  repository; a *new* exception class still needs `...Error` unless it is genuinely
  another intentional-abort signal in the same family, and any such deviation needs the
  same documented `noqa`, not a bare one.

## `PreflightAbortReason`: a closed `Literal`, not a string

`PreflightAbortReason` is `Literal["same_day_rerun", "no_trading_day",
"price_fetch_failed"]`, not a bare `str`, because the unattended `swing-daily` skill
branches on the tag itself rather than parsing prose — `main()` prefixes stderr with the
machine-readable `PREFLIGHT_ABORT[<reason>]:` precisely so the consuming skill never has
to infer the cause. Adding, renaming, or removing a reason changes what that skill (and
`scripts/check_daily_complete.py`'s whitelist of legitimate-stop reasons) can branch on —
it is a contract change, not an internal rename. **REQUIRED:** `release-impact` for what
that obliges the same pull request to carry.

## `cli_support.run_cli` and `ExitPolicy`

`run_cli(body, policy)` is the one place a `copilot-*` entry point converts a caught
domain error into `SystemExit` — `tests/test_quality_contracts.py`'s
`test_every_console_script_converts_its_errors_through_cli_support` asserts every script
listed in `pyproject.toml`'s `[project.scripts]` imports `run_cli` from
`cli_support`, so a new console script fails this test until it is wired the same way.

`ExitPolicy(errors=..., code=..., format_message=..., report=...)`:

- `errors`: exactly the exception types this step converts. **Anything not listed
  propagates** — a programming error (an unhandled `TypeError`, an `AttributeError` from
  a bug) still surfaces as a real traceback instead of being silently mapped to an exit
  code that implies "this was expected". Never widen `errors` to `Exception` "to be
  safe" — that is exactly the swallow this convention exists to prevent.
- `code`: `None` means "raise `SystemExit(message)`", the argparse convention (prints to
  stderr, exits `1`). A concrete `int` reports through `policy.report` first, then exits
  with that code — this is the path `PreflightAbort` uses to reach exit `2`.
- `format_message`/`report`: default to the exception's own text and stderr; a command
  that reports through logging instead passes a logger method as `report`.

## Catching and logging on the failure path

- Catch the most specific exception a call site can actually distinguish, not a bare
  `except Exception` (or worse, `except SwingCopilotError` where a narrower subclass
  exists and the caller does branch on it).
- `logging.exception()` in a catch block, never `logger.error(str(e))` — `.exception()`
  auto-includes the traceback, and `str(e)` alone throws away the stack a future reader
  needs to diagnose the failure. Do this even where the message is redacted afterward;
  redaction and traceback capture are orthogonal.
- Never swallow silently. A caught exception is either handled meaningfully (a retry, a
  fail-soft skip that is itself recorded) or re-raised — not logged-and-dropped with no
  trace in the caller's return value.

## Never log secrets

`cli_support.configure_cli_logging(secrets, *, level=None)` is the shared path: it wires
`SecretRedactionFilter` to strip every configured, non-`None` secret
(`finnhub_api_key`, `fred_api_key`, `discord_webhook_url`) from a record's message *and*
its formatted traceback before either reaches stderr — sorted longest-first so a secret
that is a prefix of another is never partially redacted. Use it in any `copilot-*` entry
point that talks to an authenticated external boundary and can therefore carry a secret
into a `logger.exception` traceback (today: `copilot-daily`, and `copilot-retro`'s
`export`/`prepare`, via `EdgarClient`/`FinnhubNewsClient`).

A CLI with no such boundary (`analysis/cli.py`, `analysis/verify_cli.py`) configures its
own plain `logging.basicConfig` instead — it has nothing to redact, so sharing this
module's filter would buy it nothing. Do not wire `configure_cli_logging` into a CLI
just for consistency; wire it only where a secret can actually reach a log record.
`edgar_identity` is deliberately excluded from the redacted set: per SEC EDGAR's
fair-access policy it is a contact identity meant to appear in outgoing requests, not a
bearer credential.
