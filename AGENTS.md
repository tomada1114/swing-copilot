# Project Guide

## Overview

`swing-copilot` is a local Python batch application for US-equity decision
support. It collects point-in-time market/fundamental/text data, screens a
configured strategy, checks portfolio risk, exports a strict analysis-input
document for Claude Code skills to interpret, ingests their sourced answer, and
produces reports. It never calls a model API itself, and it never places
orders; a human makes every buy/sell decision.

The project uses Python 3.14+, uv, hatchling, a strict `src/` layout, ruff,
mypy strict, pytest, DuckDB, and Parquet.

## Quick Reference

```bash
just install     # Install all dependency groups and git hooks
just fmt         # Apply ruff fixes and formatting (mutating)
just lint        # Ruff lint/format check + mypy strict
just test        # Pytest (parallel, `-n auto`) with line and branch coverage >= 95%
just test-changed # Only the tests this diff can affect, + >=90% coverage on changed files
just dashboard   # Serve the read-only decision-history dashboard (http://127.0.0.1:8787)
just docs        # Serve docs locally
just docs-check  # Build MkDocs with --strict
just build       # Build distribution packages
just smoke       # Build and verify the wheel in a temp environment
just check       # Apply formatting, then run lint and tests
just verify      # Fast pre-PR gate: lint, docs-check, test-changed (diff-scoped)
just verify-full # Full non-mutating release gate: lint, docs-check, smoke, test
```

Without Just, use the corresponding `uv run` commands in `justfile`. During
development, run the narrowest relevant pytest target first; run `just verify`
before a PR (CI runs the full gate on every PR regardless, including the
repo-wide 95% coverage floor and the wheel smoke test — `just verify`
deliberately does not duplicate those locally; a selection gap that lets
something through locally and fails in CI is fixed as a rule-table gap in
`scripts/diff_gate.py`, in the same PR). Run `just verify-full` before a
release or a direct-to-main completion claim.

## Architecture

```text
src/swing_copilot/
├── clock.py             # The only wall-clock boundary
├── config.py            # Strict settings and strategy validation
├── models.py            # Shared domain values
├── exceptions.py        # SwingCopilotError base hierarchy
├── strict_model.py      # extra="forbid" pydantic base for skill-boundary schemas
├── io_atomic.py         # Dependency-free atomic file replacement (the only place)
├── retry.py, ratelimit.py  # External-call retry / throttle primitives
├── cli_support.py       # Domain error -> exit code conversion shared by every CLI
├── documents.py         # Text / JSON document readers, boundary-typed failures
├── universe.py, universe_sampling.py  # S&P 500 membership resolution and deterministic --limit sampling
├── data/                # Market/fundamental external adapters
├── text/                # Untrusted text-source adapters
├── screening/           # Pure indicators, filters, signals, ranking
├── regime/              # Market gate (SMA200 / distribution days / FTD) state
├── risk/                # Position sizing, concentration, correlation
├── backtest/            # Deterministic point-in-time simulator
├── analysis/            # Skill boundary: export, strict schemas, provenance, safety
├── report/              # Markdown / terminal / Discord rendering, history CLI
├── storage/             # DuckDB/Parquet repositories and transactions
├── research/            # Read-only DataFrame accessors for notebooks/ad-hoc SQL
├── retro/               # Verdict retrospective loop (collect / evaluate / export / prepare / ingest)
├── tracking/            # Virtual ledger that scores verdict outcomes
├── dashboard/           # Read-only decision-history web UI (just dashboard)
└── pipeline/            # Composition root and imperative orchestration
```

Shared primitives (`io_atomic`, `exceptions.SwingCopilotError`, `strict_model.StrictModel`,
`cli_support.run_cli`) are each implemented exactly once; reuse them instead of
reimplementing atomic writes, the exception hierarchy, strict schema
configuration, or CLI exit-code conversion (`tests/test_quality_contracts.py`
enforces this mechanically; see `docs/reference.md`).

- Keep the public API small and export deliberate additions via
  `swing_copilot.__init__.__all__`.
- Keep the functional core deterministic. External I/O, clock access, storage,
  and orchestration belong at explicit boundaries.
- Use Protocols only for volatile or failure-prone boundaries, not every
  internal class.
- Update `docs/reference.md` and README examples when the public API changes.

## Sources of Truth and Conflict Handling

| Concern | Canonical source |
|---|---|
| Product requirements and constraints | `docs/01_requirements.md` |
| Public investment philosophy and analysis-rule genealogy | `docs/10_investment_philosophy.md` |
| Architecture and behavioral invariants | `docs/03_basic_design.md`, then the contract sections of `docs/04_detailed_design.md` |
| Current data/API shape | `src/swing_copilot/models.py`, `storage/schema.py`, public signatures |
| Tooling and quality commands | `justfile`, `pyproject.toml`, CI workflows |
| Current execution status | Git, fresh test output, and CI; never prose or test counts in a prompt |
| One autonomous run's instructions | `docs/goal-prompts/**` |

`docs/goal-prompts/**` are execution support and history, not an evergreen
replacement for canonical requirements/design. If canonical design and current
schema/API disagree, do not silently pick one. Preserve compatibility, record
the divergence, and update the stale canonical source or request a decision.

## Non-negotiable Behavioral Invariants

### Time and point-in-time visibility

- Every screening, risk, report, and backtest calculation receives an explicit
  `as_of`. Price rows require `date <= as_of`; filings/fundamentals require
  `filed_at <= as_of`; universe snapshots require `snapshot_date <= as_of`.
- Treat the boundary as inclusive and test a row immediately before, exactly
  at, and immediately after the cutoff.
- Domain logic and adapters must not call `date.today()` or `datetime.now()`
  directly. Use the injected `Clock`; wall time is metadata, never a substitute
  for `as_of`.

### Storage, correction, and atomicity

- Natural-key reruns must incorporate corrected input; do not use
  `ON CONFLICT DO NOTHING` where correction is expected.
- A logical multi-row DuckDB write is one transaction: all rows commit or all
  roll back. Tests inject a failure after at least one successful statement.
- A snapshot replacement must also remove members absent from the replacement.
- Parquet/report replacement uses a temporary file in the destination
  directory and `os.replace`; failure must preserve the previous destination
  and clean up temporary artifacts.
- Ad-hoc/notebook reads of the shared DuckDB file go through
  `swing_copilot.research` (read-only, one short-lived connection per query).
  Never hold a connection open across analysis think-time — DuckDB's file
  lock is exclusive between a read-write process and everything else — and
  never re-implement the sector as-of join by hand; `v_symbol_sector_asof`
  is the single blessed implementation.

### Quantitative correctness

- Correlation joins return series by trading date, not row position. Require the
  configured number of overlapping returns; duplicates, insufficient overlap,
  and constant series produce an explicit data-quality result.
- Backtests apply adverse slippage and commission on both entry and exit,
  including forced liquidation. Tests assert hand-calculated cash/equity, stop
  versus max-hold precedence, residual benchmark cash, and final liquidation.
- Strategy configuration is parsed into strict typed values before external I/O.
  Reject unknown fields/keys, empty required signals, invalid limits, and ranking
  rules that violate deterministic ordering.

### External boundaries and skill-based analysis safety

- External calls have explicit timeouts, bounded retryable exceptions, total
  attempt ceilings, and deterministic backoff tests. Rate limiting applies to
  every attempt. Do not retry validation/programming errors.
- The default pytest suite is offline. The autouse socket guard must remain in
  place; inject fakes at external ports. Live checks are separately marked and
  never part of the offline success sentinel.
- The suite must not write operator-owned data. `output_dir` and other
  repo-relative defaults resolve to real directories, so every filesystem test
  passes an isolated path. The autouse `reports/` and `data/` guards must
  remain in place alongside the socket guard. `data/` is guarded twice on
  purpose: an mtime check catches writes, and a `duckdb.connect` interception
  catches the *open* — `init_schema()` against an already initialized file
  changes no mtime, yet still takes DuckDB's exclusive file lock and can fail
  whatever the operator is doing with that file (a `just data-pull` /
  `data-push`, or a local `copilot-daily`).
- Qualitative analysis runs in a Claude Code skill, never inside this process.
  The pipeline exports `analysis_input.json` and ingests `analysis_result.json`
  via `copilot-ingest-analysis`; both directions parse under strict
  (`extra="forbid"`) schemas, so an invented or renamed field fails loudly
  instead of being silently dropped.
- Nothing a skill writes is trusted. Every fact has a non-empty, non-blank
  `source_ids` list proven to be a subset of the IDs supplied for that symbol,
  and code-owned metadata (form type, filing date, source URL) is resolved from
  the exported input rather than echoed back from the result. Deterministic
  screening, sizing, and ranking values are never rewritten by an analysis.
- Enforce CON-03 centrally at ingest, over every user-visible text field,
  before anything reaches a report. Skill instructions alone are insufficient;
  a violating symbol is withheld fail-closed, per symbol, with no retry.
- Never log secrets. Redact exception and audit fields.

## Test and Review Discipline

- Test behavior and contracts, including happy path, boundaries, partial
  failure, rollback, recovery, and cache reuse. Coverage is a floor, not proof.
- Calling a fake/mock can be asserted when the call itself is the contract,
  such as retry/rate limits, skipped steps, or proving no network/API call.
- Keep implementation, its regression test, and required canonical-doc update
  in the same logical commit.
- Before completion, inspect changed paths and apply the matching review:
  - `storage/**`: transaction rollback, correction upsert, replacement semantics
  - `data/**` or `text/**`: as-of boundary, timeout/retry/rate limit, offline test
  - `risk/**`: date alignment, minimum sample, NaN/constant inputs
  - `backtest/**`: no look-ahead, both-side costs, exact final equity
  - `analysis/**`: strict schema boundary, provenance, CON-03, fail-closed withholding
  - config/pipeline: fail-fast validation, fatal/fail-soft boundary, rerun safety

## Git Workflow

- Small, low-risk changes (documentation, comments, typo fixes, and similar
  edits that do not change behavior) may be committed and pushed directly to
  `main`.
- Anything that changes behavior, public API, storage schema, or configuration
  semantics goes through a branch and a pull request.
- Direct-to-`main` does not relax the quality gates: run the matching checks
  (`just verify-full` before a completion claim on code changes) either way.
- This is the workflow for human/agent development. It does not apply to the
  P8 retrospective apply flow, which is required to be one proposal per PR.

## Language and Scope

- Code identifiers and public API names remain English.
- Prose may be written in Japanese or English, including documentation,
  comments/docstrings, commit summaries, PR titles, and PR bodies. Conventional
  Commit type/scope tokens remain English. Keep one language internally
  consistent within a document or PR.
- Do only what the task requires. Preserve unrelated and user-owned changes.
- Prefer editing an existing file. Create a new file only when the requested
  design genuinely needs a new module/artifact.
- Add dependencies only to the appropriate `pyproject.toml` group and commit
  `uv.lock` with dependency changes.

## Scheduled Daily Run

The daily analysis loop runs unattended on weekdays in CI: it fires the day
*after* the US session it analyzes, so the weekday mask must include Saturday
to cover Friday's session — a Monday-through-Friday mask would miss it. That
is the only scheduled trigger, plus a manual dispatch for an out-of-band run.
Nothing is retried automatically: a failed or skipped day is re-dispatched by
hand, and the pipeline's preflight check makes the gap visible in the next
run.

The canonical `data/` — and, since Issue #370, the canonical daily run archive
under `reports/<run_date>/<run_id>/` (and `<run_id>.md`) — lives in a private
object-storage bucket, not in any working copy. Both trees share one manifest,
one `generation`, and one push/pull commit; there is no separate reports-side
counter. `reports/latest.md` and everything under `reports/backtests/`,
`reports/dry_run/`, `reports/assets/`, and `reports/retro/` stay local/derived
and are never synced. The scheduled run pulls before analysis and pushes back
only on success, so a failed day leaves the remote on the previous generation.
`copilot-retro collect` — the only path that fills `verdicts` (design decision
D2: `copilot-ingest-analysis` never touches the DB) — now runs inside that
same CI job, after the analysis and before the push, so a day's verdicts ride
out with everything else instead of being lost when the runner is discarded.

`reports/<run_date>/<run_id>/` is retained forever and grows without bound
(Issue #370 made it R2-canonical, on purpose — retention/deletion was
considered and rejected in Issue #373); what does not scale is a *fresh*
CI runner re-fetching all of it every weekday. The scheduled job's `pull`
therefore passes `--reports-window 10`: only the `reports/` keys belonging to
the 10 most recent *run dates* (never calendar days, so a holiday or a missed
run cannot shrink it below 10 actual runs) are fetched; `data/` is always
pulled in full. The window actually used is recorded in the shared state
file, and the following `push` derives its behavior from that record rather
than from a repeated flag: `reports/`'s garbage collection is suppressed
entirely (an out-of-window key is expected to be locally absent, not
deleted), and `--reports-append-only`'s guard only checks keys the window did
fetch. The accepted trade-off is that CI's own `copilot-retro collect` only
ever sees the last 10 run dates; a correction to an older archive still needs
an operator's local full `pull` (no window) → `collect` → `push` (without
`--reports-append-only`) to be re-collected and re-published.

### Working with the data locally

- Read-only work (ad-hoc research, a read-only dashboard): pull the remote
  copy first, then read the local copy; a status check confirms whether it
  still matches the remote.
- Anything that writes (a retrospective run, a live daily run): pull → work →
  push, in one sitting. The optimistic lock is a monotonic `generation` field
  in the shared manifest — the only concurrent-write guard, covering `data/`
  and `reports/` together — so do not leave a pulled copy unpushed, and do not
  start a local write while the scheduled run holds the generation.
- Never open the shared DuckDB file as a read-write connection for
  exploration, and never hold any connection across think-time. The file lock
  is exclusive between a read-write process and everything else, so a held
  handle fails the next pull/push. Sync always moves the file as bytes, never
  through DuckDB.
- A fresh worktree has no `data/`, `reports/`, `.env`, or virtualenv of its
  own. Install dependencies there, copy `.env` in by hand (it holds
  credentials and is untracked), and pull the data and report-archive history
  fresh — never by copying or symlinking another checkout's `data/` or
  `reports/`.
- `--reports-window` is a CI-only flag (`justfile`'s `data-pull` and any local
  `scripts/data_sync.py pull` never pass it): a local working copy always
  pulls `reports/` in full, which is also what lets it recover a windowed CI
  runner's blind spot (see above).

The daily pipeline's entry point exits `2` on a preflight abort, and stderr's
first line carries a machine-readable tag that the caller branches on — never
assume exit 2 means "already ran":

- `PREFLIGHT_ABORT[same_day_rerun]:` — a successful run already exists for the
  resolved run date (the schedule fires once per weekday, but a manual
  dispatch or a re-run of a completed day would otherwise write a second
  verdict set). It exits before creating a run record or report directory. An
  explicit override flag bypasses the guard for an intentional re-run. This is
  a legitimate stop: `scripts/check_daily_complete.py` passes and the job
  stays green.
- `PREFLIGHT_ABORT[no_trading_day]:` — the price prefetch succeeded, but no
  fetched bar belongs to a session that has actually closed (16:00
  America/New_York): either the prefetch came back empty, or the newest bar
  is still mid-session because the scheduled job started late — `run_date` is
  never the wall clock and never merely "the newest bar fetched", either of
  which would book a day before it closed (Issue #372). No retry follows
  automatically; the next scheduled run resolves it, or a manual dispatch
  does. This is also a legitimate stop: the job stays green.
- `PREFLIGHT_ABORT[price_fetch_failed]:` — the price prefetch itself raised
  (e.g. a data-provider outage), so whether any session had closed could not
  even be determined (Issue #372). Unlike the two reasons above, this is a
  genuine failure, not a clean day with nothing to analyze, and it must not
  be reported as one: `scripts/check_daily_complete.py` fails the job on this
  reason (its legitimate-stop check is a whitelist of exactly
  `same_day_rerun` and `no_trading_day`, so an unrecognized or missing reason
  fails closed the same way). No retry follows automatically.

`copilot-daily` also writes its own terminal outcome (`success`/`degraded`/
`failed`/`preflight_abort`, plus the abort reason when applicable) to a JSON
file outside `reports/<run_date>/<run_id>/`, whenever `--outcome-file` (or its
`COPILOT_DAILY_OUTCOME_FILE` environment fallback, which CI always sets) is
configured. This is what lets `scripts/check_daily_complete.py` tell "the
pipeline never even started" apart from "it started and legitimately found no
trading day yet", without relying on the headless analysis session to
self-report anything.

## Reading the Accumulated Data

Ad-hoc analysis of the DuckDB history (verdict outcomes, score breakdowns,
tracking ledger, regimes, rejections) goes through the read-only research
accessor module — one connection per query, joined views included. Never open
a raw read-write DuckDB connection against the shared file for exploration,
and never hold any connection across think-time: the file's lock is exclusive
between a read-write process and everything else, so a held connection fails
the next pull/push and strands the local copy on a stale generation.
Improvement work discovered while analyzing follows the architecture review's
principles: no config changes on point estimates alone; route proposals
through issues or the retrospective loop.

## Conventions: src/**/*.py, scripts/**/*.py

### Design

- Treat 300-line modules and 40-line functions as review triggers, not absolute
  correctness rules. Split only when doing so improves a real responsibility boundary
- Prefer 3 or fewer parameters; group related parameters with a dataclass or TypedDict
- Google-style docstrings (Args/Returns/Raises) on all public functions; document *why*, not what the type signature already says; don't document obvious code

### Error Handling

- Define a package-level base exception; derive all specific errors from it
- Catch the most specific exception possible
- Use `logging.exception()` in catch blocks (auto-includes traceback), never `logger.error(str(e))`
- Never swallow exceptions silently; if catching, handle meaningfully or re-raise
- Never use exceptions for control flow
- Return `None` or a sentinel only when the caller expects it; prefer raising for true errors

### Type System

- Prefer `@dataclass(frozen=True, slots=True)` for internal value objects
- Use Pydantic (`BaseModel`) only at serialization/deserialization boundaries
- Use `TypedDict` for structured dict shapes (API responses, config dicts)
- Use `Protocol` for structural subtyping instead of ABC when possible
- Avoid `Any`; when unavoidable, add a comment explaining why (e.g., `# Any: third-party lib has no stubs`)

### Performance

- Use generator expressions and `itertools` for large sequences; avoid materializing unnecessary lists
- Use `__slots__` on frequently instantiated classes (dataclass `slots=True`)
- Use `functools.lru_cache` or `functools.cache` for expensive pure functions
- Prefer `str.join()` over `+=` concatenation in loops
- Use `collections.defaultdict`, `Counter`, `deque` instead of hand-rolled equivalents
- Avoid repeated attribute lookups in tight loops; bind to local variable
- Use `dict`/`set` for O(1) membership tests instead of lists
- Lazy-import heavy optional dependencies inside functions to reduce import time

### Pythonic Patterns

- EAFP (try/except) over LBYL (if-check) when dealing with duck typing or I/O
- Use context managers (`with`) for all resource management (files, connections, locks)
- Prefer comprehensions over `map()`/`filter()` for readability
- Use `enum.Enum` for fixed sets of values instead of string constants
- Use `walrus operator` (:=) for assign-and-test when it improves clarity
- Use structural pattern matching (`match/case`) for complex dispatch
- Use `*args` unpacking and `**kwargs` deliberately; avoid passing them blindly through call chains

### Security

- Sanitize file paths to prevent directory traversal (`pathlib.Path.resolve()` then check prefix)
- Ruff's bandit rules (`S`) cover eval/exec/pickle/random misuse — do not suppress them with `noqa` without a written justification

### Constants and Naming

- Use `UPPER_SNAKE_CASE` named constants instead of magic numbers/strings
- Boolean variables/params: prefix with `is_`, `has_`, `can_`, `should_`
- Private helpers: prefix with `_`; reserve `__` (name mangling) only for avoiding conflicts in subclass hierarchies

## Conventions: pyproject.toml

- Runtime dependencies go under `[project] dependencies`
- Dev dependencies go under `[dependency-groups] dev`; docs under `[dependency-groups] docs`
- Before adding a dependency: verify active maintenance, compatible license (MIT/BSD/Apache), and minimal transitive dependencies
- Use version ranges (`>=X.Y`) for runtime dependencies -- never pin exact versions in a library
- NEVER remove existing ruff rules without explicit user approval
- NEVER lower the line+branch coverage threshold (currently 95%)
- After modifying dependencies, run `uv sync --all-groups`
- The `uv.lock` file MUST be committed alongside dependency changes

### `[tool.uv] exclude-newer`

`exclude-newer` is a supply-chain cooldown: `uv lock` and `uv sync` ignore any
package version published after the given timestamp, so a dependency cannot be
resolved until it has survived in the wild for a while. This complements the
Dependabot `cooldown.default-days` setting in `.github/dependabot.yml`, which
delays *update PRs* by the same idea — together they keep both fresh installs
and automated upgrades off packages published in the last few days.

Bump cadence: whenever dependencies are updated, move the `exclude-newer`
timestamp forward to roughly "today minus 14 days"; do this at least monthly
even if no dependency changed, so the cutoff doesn't drift too far behind.

Procedure:

1. Edit the `exclude-newer` date in `pyproject.toml`.
2. Run `uv lock` to regenerate `uv.lock` against the new cutoff.
3. Commit `pyproject.toml` and `uv.lock` together in the same commit.

## Conventions: docs/**/*.md, README.md, CONTRIBUTING.md, CHANGELOG.md

- Document non-obvious behavior, architecture decisions, and trade-offs
- Do NOT document what is obvious from the code or already expressed by the type system
- Code examples in docs must be valid Python that works with the current API
- Use admonitions (note, warning, tip) for important callouts in MkDocs pages
