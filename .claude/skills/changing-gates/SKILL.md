---
name: changing-gates
description: >
  Covers editing a file that enforces rather than implements: a .github/workflows/*.yml
  CI workflow, one of the three .claude/hooks/*.py (format.py, guard.py, stop_check.py),
  a justfile recipe such as verify or verify-full, a setting inside pyproject.toml's
  [tool.ruff]/[tool.mypy]/[tool.coverage] tables, or scripts/diff_gate.py's path -> test
  rule table. Use when a new CI job or workflow is proposed, a hook's blocked-command
  list changes, a ruff rule or mypy flag is added or loosened, the 95% coverage floor or
  `CHANGED_FILE_COVERAGE_THRESHOLD` is touched, an action pin or `permissions:` block is
  edited, or `scripts/diff_gate.py`'s path -> test rule table needs a new entry.
---

# Changing Gates

**Owns:** a change to a file that enforces rather than implements — a CI workflow, a
`.claude/hooks/*.py` script, a `justfile` recipe, a `pyproject.toml` gate table, or
`scripts/diff_gate.py`'s rule table. **Does not own:** which tests exist or where they
live (`writing-tests`, `placing-tests`), whether a dependency may be added
(`managing-dependencies`), what a change breaks for consumers (`release-impact`).

## Enforcement layers, and where each one binds

Three layers enforce the same underlying rules at three different scopes — pick the
layer that matches who the rule must bind, not the one that is easiest to edit:

| Layer | Binds | Bypassed by |
|---|---|---|
| Claude Code hooks (`.claude/hooks/*.py`, wired in `.claude/settings.json`) | This session, this tool, only while Claude Code is driving | A human editing the file directly, another tool, `bypassPermissions` mode |
| `.pre-commit-config.yaml` | This checkout, at commit time, for whoever has the hooks installed (`just install`) | `git commit --no-verify` (see below), a checkout that never ran `just install` |
| `.github/workflows/ci.yml` and friends | Every author, every push and PR, unconditionally | Nothing short of editing the workflow itself |

A rule that must hold for everyone belongs in CI, not only in a hook — a hook only
protects a session that has this repo's `.claude/` config loaded. `guard.py`'s own
docstring states the reason it exists at all: Claude Code `deny` permission rules are
advisory in some versions, and hooks also fire in `bypassPermissions` mode, so the hook
is the backstop for rules the permission system cannot guarantee on its own — not a
replacement for a CI check on the same rule.

## The three hooks — what each actually blocks

- **`format.py`** (PostToolUse, `Edit|Write`): runs `ruff check --fix --no-cache` then
  `ruff format --no-cache` on the single `.py` file just edited. It runs after every
  edit, so re-running a formatter by hand afterward is redundant work, not a safety net —
  if a file still fails lint after this hook, the hook already surfaced the remaining
  violations on exit code 2 rather than silently leaving them.
- **`guard.py`** (PreToolUse, `Edit|Write|Bash`): blocks writing to `uv.lock` (regenerate
  it with `uv lock`/`uv add` instead of hand-editing), `.env`/`.env.*` (except
  `.example`/`.sample`/`.template` suffixes), and anything under `secrets/`; blocks
  `git commit --no-verify` and a plain `git push --force` (in any argument form, cluster
  or long flag) while explicitly allowing `--force-with-lease`. It parses Bash commands
  by splitting on shell control operators and inspecting each segment's argv
  independently — a block or an allow in one command never leaks into another.
- **`stop_check.py`** (Stop): when the working tree has an uncommitted `.py` file or
  `pyproject.toml` change, runs `ruff check --no-cache .`, `ruff format --check --no-cache
  .`, and `mypy src scripts tests` before the turn ends, and blocks the stop on a failure.
  This is a **lightweight feedback loop, not completion evidence** — its own docstring
  says so, it never runs tests, and it is bounded by a wall-clock `timeout` on the Stop
  hook entry in `.claude/settings.json` (a timeout skips the check rather than blocking).
  `just verify` (diff-scoped) is the actual pre-PR gate; `just verify-full` (no scoping,
  includes the wheel smoke test and the full suite) is required before a release or a
  direct-to-main completion claim. Never cite a clean `stop_check.py` run as proof a PR
  is ready.

## Hard prohibitions

- Never lower the 95% line+branch coverage threshold (`--cov-fail-under=95` in `just
  test`, `coverage report --fail-under=95` in `ci.yml`'s Coverage job). The *lower* 90%
  changed-files-only floor `test-changed` applies is not a precedent for lowering the
  repo-wide one — `placing-tests` explains why the two numbers differ.
- Never remove an existing ruff rule (`[tool.ruff.lint].select` in `pyproject.toml`)
  without explicit user approval. Adding a `per-file-ignores` entry for a narrow,
  justified case (as `tests/**` and `scripts/**` already do) is a different, smaller
  decision than dropping a rule group repo-wide.
- Never bypass a hook or weaken a check to make a run pass. When a gate blocks something
  that looks necessary, the standing answer is: fix what made the bypass look necessary,
  or ask — never find another spelling that slips past the same rule.

## Workflow-hardening rules this repo actually enforces

Verified in `.github/workflows/*.yml` and `.github/zizmor.yml`, not assumed:

- Every third-party action is pinned to a commit SHA with a `# vX.Y.Z` trailing comment
  (e.g. `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`), everywhere,
  including `swing-daily.yml`'s `anthropics/claude-code-action`.
- `permissions:` is set at the top level of every workflow and narrowed further per job
  where a job needs more (`release.yml`'s top-level `permissions: {}` with per-job
  elevation for `attestations: write`/`id-token: write`/`contents: write`); no workflow
  grants broad write access by default.
- `persist-credentials: false` is set on `actions/checkout` in every workflow except
  `docs.yml`'s deploy job, which needs the persisted token for `mkdocs gh-deploy` to
  push — and that one exception is a recorded, reasoned entry in `.github/zizmor.yml`
  (`artipacked: ignore: [docs.yml:27:9]`), not a silent gap.
- A step-output value that flows into a shell command is routed through `env:` rather
  than interpolated straight into `run:` (`swing-daily.yml`'s `STARTED_AFTER` is the
  example, with the reasoning in its own comment: routing through `env:` means the value
  can never expand into shell code).
- `zizmor .github/workflows/` and `pre-commit run --all-files actionlint` both run as CI
  jobs (`ci.yml`'s `zizmor` job), not only as local pre-commit hooks — a contributor who
  never ran `just install` still gets both checks on push. `actionlint` exists
  specifically because `zizmor` and `yaml.safe_load` both accept a workflow file GitHub
  itself rejects at parse time (a job-level `env:` referencing the `runner` context
  produced a zero-job "startup failure" that neither caught — see `ci.yml`'s comment on
  the `zizmor` job).
- A PR that removes or narrows one of the above (a pin, a `permissions` grant, a
  `persist-credentials: false`, a timeout) must state in its own body why the removed
  protection no longer applies — silence is not sufficient review for that kind of change.

## `scripts/diff_gate.py`'s rule table is a gate config

`just verify`'s `test-changed` target is not a heuristic — it is a deterministic
`path -> pytest target` rule table (`_classify*` functions in `scripts/diff_gate.py`),
widened by a one-hop import reverse map. Editing what a changed path selects (adding a
new `src/swing_copilot/<package>` directory, moving a script, changing which paths force
`ALL`) is a gate change with the same review weight as touching `ci.yml`: get it wrong
and a local `just verify` passes while CI's full-suite run fails on the same diff — and
the fix is always this rule table, in the same PR (`placing-tests` covers diagnosing that
gap). Note the existing safety net:
`.github/workflows/**` and `.claude/**` changes already route to the quality-contract
tests (`_classify_config_path`), and `scripts/diff_gate.py` itself is in `_FORCE_ALL_EXACT`
so a bug in the selector cannot hide its own regression from itself.

## `settings.json` vs `settings.local.json`

`.claude/settings.json` is committed and shared: the hook wiring above, and a permission
allowlist scoped to this project's own build/lint/test/`copilot-*` commands. Personal
preferences — model choice, output style, any extra permission one contributor wants but
the team has not agreed to — belong in `.claude/settings.local.json`, never in the
committed file. Widening `.claude/settings.json`'s allowlist is itself a gate change:
it changes what every contributor's Claude Code session may run non-interactively.
