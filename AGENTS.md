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
just install   # Install all dependency groups and git hooks
just fmt       # Apply ruff fixes and formatting (mutating)
just lint      # Ruff lint/format check + mypy strict
just test      # Pytest with line and branch coverage >= 95%
just docs      # Serve docs locally
just docs-check # Build MkDocs with --strict
just build     # Build distribution packages
just smoke     # Build and verify the wheel in a temp environment
just check     # Apply formatting, then run lint and tests
just verify    # Non-mutating release gate: lint, test, docs-check, smoke
```

Without Just, use the corresponding `uv run` commands in `justfile`. During
development, run the narrowest relevant pytest target first; run `just verify`
before a PR or completion claim.

## Architecture

```text
src/swing_copilot/
├── clock.py             # The only wall-clock boundary
├── config.py            # Strict settings and strategy validation
├── models.py            # Shared domain values
├── data/                # Market/fundamental external adapters
├── text/                # Untrusted text-source adapters
├── screening/           # Pure indicators, filters, signals, ranking
├── risk/                # Position sizing, concentration, correlation
├── backtest/            # Deterministic point-in-time simulator
├── analysis/            # Skill boundary: export, strict schemas, provenance, safety
├── storage/             # DuckDB/Parquet repositories and transactions
├── research/            # Read-only DataFrame accessors for notebooks/ad-hoc SQL
└── pipeline/            # Composition root and imperative orchestration
```

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
  the operator's scheduled run.
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
  (`just verify` before a completion claim on code changes) either way.
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
