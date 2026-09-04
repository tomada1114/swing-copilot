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

## Skills

`.claude/skills/` holds two kinds of skill. **Workflow skills** drive a procedure
end to end: `create-pr`, `smart-commit`, `merge-dependabot`, `swing-daily`,
`swing-retro`, `swing-research`, `swing-deepdive`, and the three analysis skills
(`analyze-news`, `analyze-filings`, `interpret-screening`). **Knowledge skills**
own a body of convention and carry the reasoning, boundary cases, and
anti-patterns this file states only as bare rules — load the matching one before
changing that layer.

| Touching | Load |
|---|---|
| `src/**/*.py`, `scripts/**/*.py` | `writing-python` |
| an error class or a CLI exit code | `designing-errors` |
| `__all__` or a `copilot-*` entry point | `public-api-contract` |
| any test | `writing-tests`, `placing-tests` |
| anything reading market data as of a date | `enforcing-point-in-time` |
| `storage/**`, `research/**` | `writing-storage-code` |
| `data/**`, `text/**` | `writing-external-adapters` |
| `analysis/**` | `guarding-analysis-boundary` |
| `regime/**` | `writing-regime-gates` |
| `report/**` | `rendering-reports` |
| `retro/**`, `tracking/**` | `scoring-verdict-outcomes` |
| `backtest/**` | `writing-backtests` |
| `risk/**`, `screening/**` ranking | `checking-risk-math` |
| `config.py`, `pipeline/**` | `wiring-the-pipeline` |
| CI workflows, `.claude/hooks/**`, `justfile`, gate settings | `changing-gates` |
| `pyproject.toml` dependencies | `managing-dependencies` |
| `docs/**`, `README.md` | `updating-docs` |
| a GitHub issue | `triaging-issues` |
| whether a change is breaking | `release-impact` |
| a `SKILL.md` | `authoring-skills` |
| `just data-pull` / `data-push` | `operating-shared-data` |
| a red or unexpectedly green `swing-daily` job | `diagnosing-daily-runs` |
| finishing any change | `reviewing-changes` |

## Non-negotiable Behavioral Invariants

Every rule below is enforceable as written and holds whether or not a skill is
loaded. The reasoning, boundary cases, code patterns, and the anti-patterns a
reviewer must reject live in the skill named beside each group.

### Time and point-in-time visibility — `enforcing-point-in-time`

- Every screening, risk, report, and backtest calculation receives an explicit
  `as_of`. Price rows require `date <= as_of`; filings/fundamentals require
  `filed_at <= as_of`; universe snapshots require `snapshot_date <= as_of`.
- The boundary is inclusive; test a row immediately before, exactly at, and
  immediately after the cutoff.
- Domain logic and adapters never call `date.today()` or `datetime.now()`. Use
  the injected `Clock`; wall time is metadata, never a substitute for `as_of`.

### Storage, correction, and atomicity — `writing-storage-code`

- A logical multi-row DuckDB write is one transaction: all rows commit or all
  roll back. Tests inject a failure after at least one successful statement.
- Natural-key reruns must incorporate corrected input; `ON CONFLICT DO NOTHING`
  is wrong wherever correction is expected.
- Daily price bars are the one deliberate exception: stored rows are raw
  (as-traded, unadjusted) and immutable. A re-fetched row within 0.5% of the
  stored value replaces it; a larger deviation, or a mixed-adjustment-basis
  signature, quarantines that symbol's batch fail-closed instead (nothing
  written, existing rows untouched).
- Splits/dividends adjust prices only on read, as of the caller's `as_of`;
  dividends are recorded but never applied to price.
- A snapshot replacement must also remove members absent from the replacement.
- Parquet/report replacement goes through `io_atomic`: a temporary file in the
  destination directory and `os.replace`; a failure preserves the previous
  destination and cleans up temporary artifacts.
- Ad-hoc reads of the shared DuckDB file go through `swing_copilot.research`
  (read-only, one short-lived connection per query). Never hold a connection
  across think-time, and never re-implement the sector as-of join by hand —
  `v_symbol_sector_asof` is the single blessed implementation.

### Quantitative correctness — `writing-backtests`, `checking-risk-math`, `wiring-the-pipeline`

- Backtests apply adverse slippage and commission on both entry and exit,
  including forced liquidation. Tests assert hand-calculated cash/equity, stop
  versus max-hold precedence, residual benchmark cash, and final liquidation.
- The production risk path is account-independent by design: it carries no
  correlation and no account-concentration rule, and reintroducing one is a
  regression that `tests/risk/test_checks.py::TestTradePlan::test_public_plan_has_no_account_or_correlation_constraints`
  exists to catch.
- Strategy configuration is parsed into strict typed values before external I/O.
  Reject unknown fields/keys, empty required signals, invalid limits, and ranking
  rules that violate deterministic ordering.

### External boundaries — `writing-external-adapters`

- External calls have explicit timeouts, bounded retryable exceptions, total
  attempt ceilings, and deterministic backoff tests. Rate limiting applies to
  every attempt. Do not retry validation/programming errors.
- The default pytest suite is offline. The autouse socket guard must remain in
  place; inject fakes at external ports.
- The suite must not write operator-owned data. `output_dir` and other
  repo-relative defaults resolve to real directories, so every filesystem test
  passes an isolated path, and the autouse `reports/` and `data/` guards must
  remain in place alongside the socket guard. A test that trips a guard is a bug
  in the test, never a reason to weaken the guard.
- Never log secrets. Redact exception and audit fields.

### Skill-based analysis safety — `guarding-analysis-boundary`

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
- Enforce CON-03 centrally at ingest, over every user-visible text field, before
  anything reaches a report. Skill instructions alone are insufficient; a
  violating symbol is withheld fail-closed, per symbol, with no retry.

## Test and Review Discipline

Depth: `writing-tests` (what one test asserts and how it fakes the world),
`placing-tests` (where it goes and which gate runs it), `reviewing-changes` (the
pre-completion routing).

- Test behavior and contracts, including happy path, boundaries, partial
  failure, rollback, recovery, and cache reuse. Coverage is a floor, not proof.
- An expected value comes from outside the code under test — a hand-worked
  number, a literal, the spec — never recomputed the way the implementation
  computes it.
- Calling a fake/mock can be asserted when the call itself is the contract,
  such as retry/rate limits, skipped steps, or proving no network/API call.
- Keep implementation, its regression test, and required canonical-doc update
  in the same logical commit.
- Before claiming completion, route the diff's changed paths through
  `reviewing-changes`, which names the review each layer owes and the skill that
  holds it.

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

Depth: `diagnosing-daily-runs` (reading a run's outcome — the exit codes, the
`PREFLIGHT_ABORT[...]` tags, the outcome file, and what to re-dispatch),
`operating-shared-data` (pull, push, and the shared `generation` lock).

The daily analysis loop runs unattended on weekdays in CI: it fires the day
*after* the US session it analyzes, so the weekday mask must include Saturday to
cover Friday's session — a Monday-through-Friday mask would miss it. That is the
only scheduled trigger, plus a manual dispatch for an out-of-band run. Nothing is
retried automatically: a failed or skipped day is re-dispatched by hand, and the
pipeline's preflight check makes the gap visible in the next run.

The job pulls before analysis and pushes back only on success, so a failed day
leaves the remote on the previous generation. `copilot-retro collect` — the only
path that fills `verdicts` (design decision D2: `copilot-ingest-analysis` never
touches the DB) — runs inside that same job, after the analysis and before the
push, so a day's verdicts ride out with everything else instead of being lost
when the runner is discarded.

`copilot-daily` exits `2` on a preflight abort and puts a machine-readable
`PREFLIGHT_ABORT[<reason>]:` tag on the first line of stderr. Never assume exit 2
means "already ran": `same_day_rerun` and `no_trading_day` are legitimate stops
that keep the job green, while `price_fetch_failed` means the price prefetch
itself raised, so whether any session had even closed could not be determined —
a genuine failure that must not be reported as a clean day.
`scripts/check_daily_complete.py` whitelists exactly the first two reasons, so an
unrecognized or missing reason fails closed the same way.

### Working with the data locally

The canonical `data/` and the canonical daily run archive under
`reports/<run_date>/<run_id>/` live in a private object-storage bucket, not in
any working copy. Both trees share one manifest, one `generation`, and one
push/pull commit.

- Read-only work (ad-hoc research, a read-only dashboard): pull the remote copy
  first, then read the local copy; a status check confirms whether it still
  matches the remote.
- Anything that writes (a retrospective run, a live daily run): pull → work →
  push, in one sitting. The monotonic `generation` field in the shared manifest
  is the only concurrent-write guard, covering `data/` and `reports/` together —
  so do not leave a pulled copy unpushed, and do not start a local write while
  the scheduled run holds the generation.
- Never open the shared DuckDB file as a read-write connection for exploration,
  and never hold any connection across think-time. The file lock is exclusive
  between a read-write process and everything else, so a held handle fails the
  next pull/push. Sync always moves the file as bytes, never through DuckDB.
- A fresh worktree has no `data/`, `reports/`, `.env`, or virtualenv of its own.
  Install dependencies there, copy `.env` in by hand (it holds credentials and is
  untracked), and pull the data and report-archive history fresh — never by
  copying or symlinking another checkout's `data/` or `reports/`.
- `--reports-window` is a CI-only flag: a local working copy always pulls
  `reports/` in full, which is also what lets it recover a windowed CI runner's
  blind spot.

## Reading the Accumulated Data

Depth: `swing-research` (the workflow for answering a question from the history),
`writing-storage-code` (the read-only accessors and the blessed views),
`operating-shared-data` (getting a current copy first).

Ad-hoc analysis of the DuckDB history (verdict outcomes, score breakdowns,
tracking ledger, regimes, rejections) goes through the read-only research
accessor module — one connection per query, joined views included. Never open a
raw read-write DuckDB connection against the shared file for exploration, and
never hold any connection across think-time: the file's lock is exclusive between
a read-write process and everything else, so a held connection fails the next
pull/push and strands the local copy on a stale generation. Improvement work
discovered while analyzing follows the architecture review's principles: no
config changes on point estimates alone; route proposals through issues or the
retrospective loop.

## Conventions: src/**/*.py, scripts/**/*.py

Depth: `writing-python` (module and function style, typing, the size triggers,
docstrings, the performance and Pythonic idioms), `designing-errors` (the
exception hierarchy and how a domain error becomes an exit code),
`public-api-contract` (what may be exported).

- Avoid `Any`; where it is unavoidable, the line carries a comment saying why.
- `@dataclass(frozen=True, slots=True)` for internal value objects; pydantic only
  at serialization/deserialization boundaries; `TypedDict` for structured dict
  shapes; `Protocol` for volatile or failure-prone boundaries, not every internal
  class.
- Google-style docstrings (Args/Returns/Raises) on all public functions,
  documenting *why*, not what the type signature already says.
- Every specific error derives from `SwingCopilotError`. Catch the most specific
  exception possible, use `logging.exception()` in catch blocks rather than
  `logger.error(str(e))`, never swallow an exception silently, and never use
  exceptions for control flow.
- Context managers for all resource management. `enum.Enum` for fixed sets of
  values. `UPPER_SNAKE_CASE` named constants instead of magic numbers or strings.
- Treat 300-line modules and 40-line functions as review triggers, not absolute
  rules. Prefer 3 or fewer parameters; group related ones in a dataclass or
  `TypedDict`.
- Ruff's bandit (`S`) rules are never silenced with a bare `noqa`; an accepted
  suppression states its justification inline.
- Sanitize file paths to prevent directory traversal (`pathlib.Path.resolve()`,
  then check the prefix).
- Private helpers are prefixed with `_`; reserve `__` for avoiding conflicts in
  subclass hierarchies. Boolean names use `is_`/`has_`/`can_`/`should_`.

## Conventions: pyproject.toml

Depth: `managing-dependencies` (whether a package may enter at all, the two-layer
cooldown, and the `exclude-newer` bump procedure), `changing-gates` (the ruff,
mypy, and coverage tables).

- Runtime dependencies go under `[project] dependencies`; dev, docs, and ops
  dependencies under their `[dependency-groups]` group.
- Use version ranges (`>=X.Y`) for runtime dependencies — never pin exact
  versions in a library.
- Before adding a dependency: verify active maintenance, a compatible license
  (MIT/BSD/Apache), and a minimal transitive footprint. Record the judgment in
  the PR.
- Run `uv sync --all-groups` after modifying dependencies, and commit `uv.lock`
  in the same commit.
- NEVER remove existing ruff rules without explicit user approval.
- NEVER lower the line+branch coverage threshold (currently 95%).
- `[tool.uv] exclude-newer` is a supply-chain cooldown. Move it forward to
  roughly today-minus-14-days whenever dependencies are updated, and at least
  monthly regardless, then regenerate `uv.lock` and commit both files together.

## Conventions: docs/**/*.md, README.md, CONTRIBUTING.md, CHANGELOG.md

Depth: `updating-docs` (which surface a change lands on, and the canonical-source
conflict rule), `release-impact` (whether it needs a CHANGELOG entry, and at
which semver level).

- Document non-obvious behavior, architecture decisions, and trade-offs. Do NOT
  document what is obvious from the code or already expressed by the type system.
- Code examples in docs must be valid Python that works with the current API.
- `just docs-check` (`mkdocs build --strict`) fails on warnings, so a new page
  must be reachable from the nav and every link must resolve.
- Use admonitions (note, warning, tip) for important callouts in MkDocs pages.
