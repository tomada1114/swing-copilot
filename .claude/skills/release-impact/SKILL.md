---
name: release-impact
description: >
  Covers deciding whether a change is breaking and at which semver level, and how that
  lands in CHANGELOG.md and the PR title. Use when adding, removing, or renaming a name
  from swing_copilot.__init__.__all__ or a copilot-* console script; changing a
  PREFLIGHT_ABORT[<reason>] tag or the PreflightAbortReason vocabulary; adding a field to
  the strict analysis_input.json/analysis_result.json schemas; changing storage/schema.py
  or a strategy config key's meaning; changing the R2 manifest.json shape in
  scripts/data_sync.py; writing a CHANGELOG.md entry; or choosing a Conventional Commits
  PR title for check-pr-title.yml.
---

# Release Impact

**Owns:** deciding whether a change is breaking, at which semver level, and how that
lands in the PR title and `CHANGELOG.md`. **Does not own:** what may be exported in the
first place (`public-api-contract`), which doc surface gets updated (`updating-docs`),
the release workflow's mechanics beyond what an author must know.

## The public surface is wider than a Python API

This project ships a batch pipeline other automation depends on, not a library other
code imports. Each row below is a real consumer, and each is a place a change can break
silently if it is treated as an internal refactor:

| Surface | Where | What a breaking change looks like |
|---|---|---|
| `swing_copilot.__init__.__all__` | `src/swing_copilot/__init__.py` (currently `ConfigError`, `Secrets`, `Settings`, `SwingCopilotError`, `__version__`, `load_secrets`, `load_settings`, `require_secrets`) | Removing/renaming an export, narrowing an accepted input, widening a required option. **BACKGROUND:** `public-api-contract` for what qualifies for `__all__` at all. |
| `copilot-*` console scripts | `pyproject.toml`'s `[project.scripts]` (12 entries) | Renaming/removing a script; changing a flag's meaning or removing one; changing what an exit code means. `cli_support.ExitPolicy` states the convention each script converts its errors through — an exit code is a contract the `swing-daily` skill and CI branch on, not an implementation detail. |
| `PREFLIGHT_ABORT[<reason>]:` stderr tags | `exceptions.PreflightAbortReason` (closed `Literal`: `same_day_rerun`, `no_trading_day`, `price_fetch_failed`) | Renaming a reason still emitted elsewhere, removing one a consumer still checks for, or changing what condition triggers one. `scripts/check_daily_complete.py` and the `swing-daily` skill both branch on the exact tag string — a rename is a break at both ends simultaneously. |
| Strict skill-boundary schemas | `analysis/schemas.py` (`AnalysisInput`, `AnalysisResult`, and everything under them), all `StrictModel` (`extra="forbid"`) | Adding a required field the *other* side does not yet produce, renaming a field, changing a type. `extra="forbid"` is exactly what makes an invented or stale field fail loudly instead of being silently dropped — which also means it fails loudly the moment one side of the schema changes and the other has not caught up. |
| DuckDB storage schema | `storage/schema.py` | Removing or retyping a column an existing production database already has. There is no formal migration runner — `INIT_SCHEMA_STATEMENTS`' `CREATE TABLE IF NOT EXISTS` is a no-op against an existing table, so a shape change to an *existing* table needs a matching `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `ALTER_SCHEMA_STATEMENTS`, and a dropped table needs a `DROP_SCHEMA_STATEMENTS` entry — omit either and the operator's real database (and the R2 copy, and CI's fresh one) diverge from what the code now assumes. |
| Strategy/config file semantics | `config.py` (`StrictModel`-based) | Silently re-interpreting an *existing* key's meaning. `extra="forbid"` only catches an unknown key — it does nothing for a known key whose meaning quietly changed, which is the more dangerous case: the config keeps parsing, but produces different screening/risk results with no error at all. |
| R2 shared-manifest shape | `scripts/data_sync.py`'s `Manifest` (`generation`, `updated_at`, `files: dict[str, FileEntry]`) and `SyncState` | Changing the manifest's shape in a way an older `pull`/`push`, or a runner mid-cutover, cannot parse. `SyncState.reports_window: int | None = None` is the model to copy for an additive field on shared state: it defaults so a state file written before the field existed still parses, rather than breaking every working copy that pulled before the change shipped. |

## Deciding MAJOR, MINOR, or PATCH

Judge the diff against the surfaces above, not against the implementation:

| Change | Level |
|---|---|
| Remove/rename an `__all__` export or a `copilot-*` script; remove/rename a still-emitted `PREFLIGHT_ABORT` reason; add a required field to a strict schema without updating the other side in the same PR; remove/retype a storage column without an `ALTER`/`DROP` statement; re-interpret an existing config key's meaning; a manifest shape change an older `pull` cannot read | MAJOR |
| Add an `__all__` export; add a `copilot-*` script or an optional flag; add a new `PREFLIGHT_ABORT` reason; add an optional schema field both sides handle; add a storage column via `ALTER_SCHEMA_STATEMENTS`; add an optional config key | MINOR |
| Reword an error message or a CHANGELOG-visible log line; internal refactor with no surface change; a dependency bump with no surface change; a doc fix | PATCH |

When a change spans more than one row, the level is the highest row it touches. This
project is pre-`1.0.0` (`pyproject.toml`'s `version = "0.1.0"`): while it stays there, a
MAJOR-shaped change still ships as a minor-looking version bump, but it still gets
called out at MAJOR weight in the PR and CHANGELOG — the pre-1.0 period changes how the
version number looks, not whether the break is disclosed as one.

## No release needed at all

A PR with no effect on any row above — an internal refactor, a test-only change, a
CI/tooling-only change (`changing-gates` owns those files, this skill owns whether they
carry release weight: normally none), a comment or docstring fix — needs no CHANGELOG
entry. Say so explicitly in the PR body rather than leaving it silent, so a reviewer can
tell "deliberately no release impact" apart from "forgotten."

## The PR title contract

`check-pr-title.yml` runs `amannn/action-semantic-pull-request` with no `types:`
override, so it enforces that action's own default Conventional Commits type set;
this repo's own history (`git log`) shows `feat`, `fix`, `refactor`, `docs`, `test`,
`chore`, and `ci` in active use, with a scope in parentheses
(`fix(tracking): ...`, `refactor(storage): ...`) and a trailing `!` on the type when the
commit itself is breaking (`refactor(risk)!: ...`). PRs carrying the `dependencies`
label are exempted from the title check (`ignoreLabels: dependencies`) since Dependabot
titles its own PRs by its `commit-message.prefix` (`deps:`/`ci:` in
`.github/dependabot.yml`), not by this convention.

`pr-label.yml` reads the PR title's type prefix and applies exactly one label:
`feat` → `enhancement`, `fix` → `bug`, `docs` → `documentation`, `ci` → `ci` — any other
type gets no label (and the step succeeds regardless; a fork PR's read-only token makes
the label application itself best-effort). `.github/release.yml` (the changelog-config
file, not the workflow) groups a tag's auto-generated release notes into
Features/Bug Fixes/Documentation/Dependencies/CI sections by exactly those labels, which
is what `.github/workflows/release.yml`'s `softprops/action-gh-release` step
(`generate_release_notes: true`) renders on a `v*` tag push. A PR title's type is
therefore not cosmetic — it is the only signal that sorts the PR into the right release
section.

## `CHANGELOG.md`

An entry is required for a user-facing change — anything landing MINOR or MAJOR above,
and any PATCH a future operator would want to know happened (a data-format change, a
behavior fix). Word it for the operator reading it later, not for the author writing it
today: state the observable change, why, and — when one exists — the concrete follow-up
action the operator must take. The existing `[Unreleased]` entries are the model for
this: each names the affected table/CLI/file, the reason, and, where applicable, an
explicit next step in bold (e.g. an existing entry calling out that a prior data-format
change requires running a specific rebuild command before the new code can be trusted
against an existing database). A changelog entry that only says *what* changed, without
the operator-facing *so what*, is incomplete.

## Branch or direct-to-`main`

AGENTS.md's Git Workflow decides this; the useful mapping is that **every row in the
surface table above** changes behavior, public API, storage schema, or configuration
semantics, so every one of them takes a branch and a PR. Direct-to-`main` is only ever
available to a change this skill has already concluded has no release impact at all.
