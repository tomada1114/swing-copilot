---
name: placing-tests
description: >
  Decides where a new test file goes (tests/<package>/test_<module>.py
  mirroring src/swing_copilot/<package>/<module>.py), which command runs it
  (uv run pytest tests/..., just test-changed, just verify, just
  verify-full), and which coverage floor applies -- repo-wide 95%
  line+branch in just test/CI vs >=90% on changed files only in
  test-changed. Use when adding a test_*.py file, choosing a conftest.py
  tier, deciding what belongs in tests/support/, just test-changed passes
  locally but CI's full suite fails, or scripts/diff_gate.py selects the
  wrong tests for a diff.
---

# Placing Tests

**Owns:** where a test file goes, which command runs it, and which coverage
floor applies. **Does not own:** what the test itself asserts
(`writing-tests`); changing a gate's configuration — the 95%/90% numbers, the
`BUDGET_SECONDS` threshold, a rule in `scripts/diff_gate.py`'s table
(`changing-gates`).

## Location: the mirror rule

`tests/<package>/test_<module>.py` mirrors
`src/swing_copilot/<package>/<module>.py` — `tests/storage/test_market_store.py`
for `src/swing_copilot/storage/market_store.py`, `tests/risk/...` for
`src/swing_copilot/risk/...`, and so on for every package (`analysis`,
`backtest`, `dashboard`, `data`, `paper`, `pipeline`, `regime`, `report`,
`research`, `retro`, `risk`, `screening`, `storage`, `text`, `tracking`). A
top-level `src/swing_copilot/<mod>.py` with no package directory (`clock.py`,
`config.py`, `models.py`, `documents.py`, `io_atomic.py`, `strict_model.py`,
`cli_support.py`, `ratelimit.py`, `universe.py`, `universe_sampling.py`) gets a
top-level `tests/test_<mod>.py` the same way. `retry.py` is the one gap — it has
no `tests/test_retry.py`; its behavior is covered indirectly through the adapter
tests, which is a gap to close, not a pattern to copy.
`scripts/<name>.py` mirrors to `tests/test_<name>.py` (`scripts/diff_gate.py`
→ `tests/test_diff_gate.py`, `scripts/data_sync.py` → `tests/test_data_sync.py`);
a script with no dedicated test file (`scripts/smoke_test.py`) is a gap the
diff selector treats as unroutable, not a pattern to imitate.

A few `tests/test_*.py` files are deliberately cross-cutting rather than
module-mirrored: `test_quality_contracts.py` (repo-wide invariants —
`test_atomic_writers_live_in_a_dependency_zero_module` and similar),
`test_e2e_smoke.py` (the fully offline five-symbol pipeline smoke test), and
`test_package.py`. Put a genuinely cross-package invariant there rather than
awkwardly attaching it to one package's test file.

## conftest.py tiers

Three tiers, narrowest wins:

- **File-local fixture** — used by exactly one test file, defined in that
  file. `tests/storage/test_market_store.py`'s `market_store` fixture is
  never promoted to a shared conftest because nothing else needs it.
- **Package `tests/<pkg>/conftest.py`** — shared within one package.
  `tests/analysis/conftest.py` holds `write_documents` and the schema
  digests every analysis-boundary test needs; `tests/screening/conftest.py`,
  `tests/backtest/conftest.py`, `tests/retro/conftest.py`,
  `tests/tracking/conftest.py`, `tests/regime/conftest.py`, and
  `tests/dashboard/conftest.py` are the same pattern for their packages.
- **Root `tests/conftest.py`** — genuinely cross-package: `state_store` and
  `settings` fixtures, and the four autouse guards every test in the suite
  runs under (see `writing-tests`).

A fixture used by two files in the same package belongs in that package's
conftest, not root; a fixture only root actually needs stays out of a
package conftest.

## tests/support/

Plain importable helpers and fakes that are *constructed*, not injected by
pytest's fixture machinery — `tests/support/fakes.py`'s `FixedClock` /
`StubDataProvider` / `StubNewsClient` / `StubCalendarClient` /
`StubEdgarClient`, `tests/support/runs.py`'s `seed_run()` factory, and
`tests/support/script_loader.py`'s `load_script_module()` for importing a
`scripts/*.py` file under test. `tests/support/test_fakes.py` pins each
fake's shape against the real `Protocol` it stands in for, so a drift (a
dropped constructor argument, a renamed method) fails loudly instead of
silently under-testing whatever module used the stale copy.

`scripts/diff_gate.py` treats every file under `tests/support/` as
unreasonable-blast-radius (`_FORCE_ALL_PREFIXES`): any change there degrades
local selection straight to the full suite. That is a deliberate cost, not a
bug — a shared fake used across a dozen test files can't be safely narrowed
by the rule table, so put something in `tests/support/` only when it's
genuinely reused, not as a place to park a single file's helper.

## Command ladder

| Command | When | Scope |
| --- | --- | --- |
| `uv run pytest tests/<path>::test_x` | Iterating on one test | That test only |
| `just test-changed` | Before pushing | `scripts/diff_gate.py`'s selection for the current diff, plus the always-appended quality-contract tests |
| `just verify` | Before opening a PR | `lint` + `docs-check` + `test-changed` |
| `just verify-full` | Before a release, or a direct-to-main completion claim | `lint` + `docs-check` + `smoke` (wheel build) + the whole suite at the repo-wide 95% floor |

CI runs the full gate — sharded `pytest` plus a combined 95% coverage
check, spell check, `docs` strict build, wheel build/smoke, and workflow
lint (`zizmor`/`actionlint`) — on every PR regardless of what ran locally, so
`just verify` deliberately does not try to reproduce all of that; see
"What CI enforces" below.

## Two coverage floors, and why they differ

- **Repo-wide 95% line+branch** — `just test`'s `--cov-fail-under=95`, and
  CI's `coverage report --fail-under=95` after combining all four shards.
  This is the floor that matters for a release; it sees the entire suite
  exercising the entire package.
- **≥90% on changed files only** — `scripts/diff_gate.py`'s
  `CHANGED_FILE_COVERAGE_THRESHOLD`, applied only to
  `src/swing_copilot/**` files this diff touches, measured against only the
  tests `test-changed` selected and ran.

`test-changed` deliberately does not apply the repo-wide 95% number, because
a partial run's package-wide percentage is systematically pessimistic: it
reflects only the subset of tests this diff's selection happened to run, not
the full suite's exercise of the same file. A changed file that the full
suite covers at 97% can show far lower under a narrow selection, for no
reason related to whether the diff itself is well-tested. Gating only the
changed files, at a lower bar, is what keeps that number meaningful; an
unexercised changed file is treated as 0% regardless (see
`evaluate_changed_coverage`), so this is not a laxer standard for the diff
itself — it just doesn't punish a diff for a file it didn't touch.

## How `scripts/diff_gate.py` selects

`select()` is a pure function of `(changed_paths, RepoShape)` — no git, no
filesystem — applying a first-match-wins rule table per changed path:

- `tests/**` — a `test_*.py` maps to itself; `conftest.py` maps to its
  package directory; any other helper file maps to its package directory.
- `src/swing_copilot/<pkg>/**` — maps to `tests/<pkg>` plus a one-hop
  reverse import map: every test file is AST-walked for its imports, so a
  cross-package dependency (e.g. `tests/pipeline/test_failsoft.py` importing
  `report/markdown_report.py` directly) is still selected even though it
  lives outside `tests/report/`. The map matches exact module names only,
  never resolved to an ancestor package — a bare `import swing_copilot.screening`
  would otherwise pull in every test for every file under `screening/`.
- `src/swing_copilot/<mod>.py` (top-level) — maps to `tests/test_<mod>.py`
  plus the same importer map.
- `scripts/<name>.py` — maps to `tests/test_<name>.py` if it exists.
- `.github/workflows/**` or `.claude/**` — maps to the quality-contract
  tests (`tests/test_quality_contracts.py`,
  `tests/analysis/test_skill_contract.py`).
- `docs/**`, `*.md`, and generated/data paths (`data/`, `reports/`, `dist/`,
  `site/`) select nothing extra.
- An **unrecognized path degrades to `ALL`**, fail-closed by design.

Force-all paths (`pyproject.toml`, `uv.lock`, `justfile`,
`.python-version`, `scripts/diff_gate.py` itself, `tests/conftest.py`,
`tests/__init__.py`, and everything under `tests/support/` or `config/`)
skip the rule table entirely — their blast radius can't be reasoned about
locally. A selection whose estimated cost exceeds roughly half the full
suite's runtime also degrades to `ALL`: running most of the suite through
the selector buys nothing over just running all of it. Every non-empty diff
appends the quality-contract tests; any `src/**` change also appends
`tests/test_e2e_smoke.py`.

## A new test directory must be reachable by the selector

Adding a `tests/<pkg>/` subdirectory the rule table cannot route to is a
placement problem, and `tests/test_diff_gate.py` catches it before CI does:
`test_every_real_test_file_is_reachable_from_itself` fails on a test file no
changed path can select, and
`test_every_real_source_and_script_file_classifies_without_crashing` fails when
a real `src/`/`scripts/` file produces neither a non-empty selection nor `ALL`.
If `just test-changed` is green locally but CI's full suite is red, that is a
rule-table gap, fixed in `scripts/diff_gate.py` in the same PR — never a reason
to fall back on `just verify-full` next time. **REQUIRED:** `changing-gates` for
editing that table; it carries the same review weight as touching `ci.yml`.

## What CI enforces that `just verify` deliberately skips

- **Repo-wide 95% line+branch coverage** — `just verify`'s `test-changed`
  only ever checks the changed-files 90% floor; only `just verify-full` or
  CI itself runs the full-suite gate.
- **Wheel build + smoke test** — CI's `build` job (`uv build` +
  `scripts/smoke_test.py`); `just verify-full` runs the equivalent locally
  via `just smoke`, `just verify` does not.
- **Spell check** (`crate-ci/typos` against `typos.toml`) — CI-only; no
  `just` recipe runs it locally at all.
- **Workflow lint** (`zizmor`, and `actionlint` via `pre-commit run
  --all-files actionlint`) — CI-only, same reason.

`just verify` exists to be a fast pre-PR gate scoped to the current diff;
duplicating everything CI already runs on every PR would defeat that
purpose. `docs-check` (`mkdocs build --strict`) is the one CI job `just
verify` *does* reproduce locally, since it's cheap and catches a docstring
break before the PR is even opened.

## Offline suite; a live check is never part of its success

The suite is offline by design — `tests/conftest.py`'s autouse socket
blocker fails any real network call immediately (see `writing-tests`).
`pyproject.toml` registers exactly one pytest marker, `slow`
(`-m "not slow"` to deselect); there is no `live` marker in this codebase.
A test that genuinely needs a real external call does not belong under
`tests/`'s autouse offline guard at all — `docs/01_requirements.md`'s
external-boundaries requirement calls for any live canary check to be
separated from this suite and its own success criteria, not folded in as a
differently-marked pytest test that could flip the offline run's result.
