# CI Failure Modes on Dependabot PRs

Read this when the survey reports `checks=FAILING`. Diagnose before deciding —
most failures here are mechanical, not regressions.

Pull the real error first:

```bash
gh run list --branch <branch> --limit 1 --json databaseId -q '.[0].databaseId' \
  | xargs -I{} gh run view {} --log-failed
```

## F1 — Stale `uv.lock` (dominant cause for pip PRs)

**Symptom:** every job fails at `uv sync --group dev --locked`, exit code 1.
`Lint & Type Check` and all `Test (Python 3.x)` jobs fail identically. The PR
touches `pyproject.toml` only.

**Cause:** Dependabot's pip updater edits the `pyproject.toml` constraint but
does not regenerate `uv.lock`. CI uses `--locked`, which refuses to proceed when
the lockfile disagrees with the manifest.

**This is not a regression.** The bump itself is untested, not broken.

**Fix:** the PR cannot be merged as-is. Take it through the combined-PR path
(SKILL.md Step 4b) and run `uv lock` there. Only after the lock is regenerated
does CI actually test the new version — so treat the combined PR's CI run as the
first real signal for these bumps.

## F2 — Genuine tooling regression

**Symptom:** `uv sync` succeeds; a later step fails — `ruff check`, `ruff format
--check`, or `mypy`.

**Cause:** the new tool version added a rule, changed a default, or tightened
inference. Common with ruff and mypy bumps.

**Fix:** mechanical fixes (apply the new lint, add a missing annotation) belong
on the branch. If the new version demands a real design decision or a config
change with tradeoffs, hold the PR and report what it wants.

Distinguish from F1 by *which step* failed. F1 fails before any project code
runs; F2 fails inside a check.

## F3 — Test failure under a new runtime dependency

**Symptom:** lint passes, `pytest` fails, or coverage drops below
`--cov-fail-under=80`.

**Fix:** this is a real signal. Read the failure. Hold the PR and report it —
do not chase coverage by editing tests to accommodate a dependency you have not
decided to accept.

## F4 — `Build & Smoke Test` only

**Symptom:** lint and tests pass; the wheel build or the smoke import fails.

**Cause:** usually a build-backend or packaging-metadata interaction. Reproduce
with `just smoke` locally — it is much faster than iterating in CI.

## F5 — Merge state `BEHIND` or `DIRTY`

Not a CI failure. `BEHIND` means main moved; `DIRTY` means a real conflict.

```bash
gh pr comment <number> --body "@dependabot rebase"
```

Dependabot rebases within a minute or two, then checks re-run. If it conflicts
repeatedly, fold the PR into the combined branch instead and resolve there.

## F6 — Check never reports

**Symptom:** `checks=PENDING` that never resolves, or `checks=NONE`.

**Cause:** workflow concurrency cancellation, or a workflow whose triggers do
not fire for fork-based bot PRs.

**Fix:** re-run with `gh run rerun <run-id>`. Never merge a PR whose checks
never actually ran — a missing check is not a passing check.
