# Project Guide

## Overview

`swing-copilot` is a local Python batch application for US-equity decision
support. It collects point-in-time market/fundamental/text data, screens a
configured strategy, checks portfolio risk, optionally asks an LLM for sourced
analysis, and produces reports. It never places orders; a human makes every
buy/sell decision.

The project uses Python 3.12+, uv, hatchling, a strict `src/` layout, ruff,
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
├── llm/                 # Gateway, schemas, provenance, safety checks
├── storage/             # DuckDB/Parquet repositories and transactions
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

### External boundaries and LLM safety

- External calls have explicit timeouts, bounded retryable exceptions, total
  attempt ceilings, and deterministic backoff tests. Rate limiting applies to
  every attempt. Do not retry validation/programming errors.
- The default pytest suite is offline. The autouse socket guard must remain in
  place; inject fakes at external ports. Live checks are separately marked and
  never part of the offline success sentinel.
- Keep LLM system instructions and user/untrusted content in separate API
  fields. Delimit and escape untrusted news/filing content; hash the complete
  system+user prompt for caching and audit.
- Every fact has a non-empty, non-blank `source_ids` list that is a subset of
  the supplied IDs. Revalidate cached output against the current request.
- Enforce CON-03 centrally before caching or rendering every user-visible LLM
  text field. Prompt instructions alone are insufficient; violations degrade
  safely without a retry.
- Never log secrets. Redact prompt, response, exception, and audit fields.

## Test and Review Discipline

- Test behavior and contracts, including happy path, boundaries, partial
  failure, rollback, recovery, and cache reuse. Coverage is a floor, not proof.
- Calling a fake/mock can be asserted when the call itself is the contract,
  such as retry/rate limits, budget skips, or proving no network/API call.
- Keep implementation, its regression test, and required canonical-doc update
  in the same logical commit.
- Before completion, inspect changed paths and apply the matching review:
  - `storage/**`: transaction rollback, correction upsert, replacement semantics
  - `data/**` or `text/**`: as-of boundary, timeout/retry/rate limit, offline test
  - `risk/**`: date alignment, minimum sample, NaN/constant inputs
  - `backtest/**`: no look-ahead, both-side costs, exact final equity
  - `llm/**`: provenance, cache revalidation, CON-03, prompt separation/redaction
  - config/pipeline: fail-fast validation, fatal/fail-soft boundary, rerun safety

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
