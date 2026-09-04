---
name: reviewing-changes
description: >
  Covers pre-completion review routing: given a diff's changed paths, which review
  applies and which skill owns it, plus the checks every diff owes regardless of path
  (matching implementation/test/doc, gate ladder, execution evidence). Use before
  claiming a task complete, before opening a PR, when deciding whether `just verify` or
  `just verify-full` is the right gate, or when triaging which specialist skill a diff
  under `src/swing_copilot/**` or `scripts/**` needs.
---

# Reviewing Changes

**Owns:** routing — given the set of changed paths, which review applies and which skill
holds it. **Does not own:** any individual review's content (each named skill owns its
own), PR mechanics (`create-pr`), or commit grouping (`smart-commit`).

## Path → review → owner

| Changed path | Re-verify | Owning skill |
| --- | --- | --- |
| `storage/**` | Transaction rollback (all-or-nothing), correction upsert vs. immutable rows, snapshot-replacement deletion | `writing-storage-code` |
| `data/**` or `text/**` | `as_of`/`filed_at` boundary, external-call timeout/retry/rate limit, offline test (socket guard) | `writing-external-adapters` |
| `screening/**` | `as_of` threaded through every calculation, no look-ahead in indicators or signals; for `screening/pipeline.py`'s scoring and sort key, the deterministic tie-break and weight validation too | `enforcing-point-in-time`, plus `checking-risk-math` for ranking |
| `regime/**` | Rolling-window count boundaries and expiry, FTD state transitions, `determine_exposure`'s unknown-cannot-loosen mapping, no look-ahead in gate state | `writing-regime-gates` |
| `risk/**` | Symbol-intrinsic trade plan only — no share count, concentration, or correlation returns to `risk/checks.py`; `Fraction`-exact sizing invariant in the simulator-only `position_sizing.py`; NaN / non-positive / degenerate-ATR handled as an explicit named branch, never propagated | `checking-risk-math` |
| `backtest/**` | No look-ahead, adverse slippage + commission on both entry and exit (forced liquidation included), exact hand-calculated final equity | `writing-backtests` |
| `analysis/**` | Strict (`extra="forbid"`) schema boundary, `source_ids` provenance, CON-03 enforcement, fail-closed withholding | `guarding-analysis-boundary` |
| `config.py` or `pipeline/**` | Fail-fast validation, fatal vs. fail-soft boundary, rerun safety | `wiring-the-pipeline` |
| `report/**` | A withheld or degraded candidate still renders visibly as such, no imperative wording invented in a template, values formatted rather than recomputed | `rendering-reports` |
| `dashboard/**` | Read-only, one-connection-per-query discipline; it carries its own `src/swing_copilot/dashboard/AGENTS.md` | that `AGENTS.md`, plus `writing-storage-code` |
| `retro/**` or `tracking/**` | Ledger accounting (entry price reconciled to the same day's bar close, split re-basing, rebuild reconstructs rather than patches), idempotent re-collection, audit columns that let an evaluation be reproduced | `scoring-verdict-outcomes` (the retro *procedure* is `swing-retro`'s) |
| `scripts/**` | Same conventions as `src/**/*.py`; `scripts/data_sync.py` and `scripts/check_daily_complete.py` each carry their own operational contract | `writing-python`, plus `operating-shared-data` / `diagnosing-daily-runs` for those two files |
| `.github/workflows/**` or `.claude/hooks/**` | A gate may narrow what a tool checks; it may never invent its own rule, and is never weakened just to pass | `changing-gates` |
| `pyproject.toml` | Correct dependency group, license/maintenance check before adding, `exclude-newer` cadence, `uv.lock` committed alongside | `managing-dependencies` |
| `docs/**` | Which `docs/NN_*.md` is canonical for the claim, code examples still valid against the current API | `updating-docs` |
| `tests/**` | Correct placement/mirroring, which gate (`test-changed` vs. full suite) actually exercises it | `placing-tests` |

A diff touching more than one row gets every matching review — routing does not collapse
to "pick the closest one."

## Every diff, regardless of path

AGENTS.md's standing rules apply to every diff; the two that are most often *missed at
review time* rather than disobeyed on purpose:

- The same-logical-commit rule bites hardest on the doc half. A PR that fixes
  `storage/market_store.py` without a test is obvious; one that changes behavior
  `docs/03_basic_design.md` describes and leaves the prose stale passes every gate
  silently. `updating-docs` decides whether a doc surface is actually owed.
- "Never weaken a gate" includes the shapes that do not look like weakening: narrowing a
  test's assertion instead of fixing the bug, adding a `per-file-ignores` entry so a new
  file escapes a rule, or moving a helper into `tests/support/` so the diff selector
  degrades to `ALL` and hides which tests actually cover it. If a gate genuinely is
  wrong, that is a `changing-gates` change argued on its own.

## Gate ladder

Narrowest relevant `pytest` target first, during development. `just verify` (diff-scoped:
lint, `docs-check`, `test-changed`) before opening a PR — CI still runs the full gate
regardless. `just verify-full` (lint, `docs-check`, `smoke`, the whole suite at the
repo-wide 95% floor) before a release or a direct-to-main completion claim. **REQUIRED:**
`placing-tests` for which command actually exercises a given test and which coverage
floor applies to it.

## Execution status has one source

Fresh test output and CI are the only evidence a check passed — never prose, and never a
test count carried over from an earlier prompt or an earlier run of this same diff. A
claim of "tests pass" that does not point at output from this session's own run is not
verified; re-run the gate.
