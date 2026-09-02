---
name: create-pr
description: >
  Create or update pull requests following project conventions. Runs pre-checks
  (`just verify`, the diff-scoped fast gate — CI runs the full gate on every
  PR), generates a Conventional Commits title, fills the PR template with
  summary/test plan/checklist, and verifies all checklist items pass before
  creating via gh CLI. Use PROACTIVELY when: PR creation, pull request,
  create PR, open PR, submit PR, PR update, review request.
---

# PR Creation Workflow

PR titles, bodies, and commit summaries may be written in Japanese or English.
Conventional Commit type/scope tokens and branch names remain English/ASCII.
Keep one prose language consistent within a PR.

## Dynamic Context

PR template:
!cat .github/PULL_REQUEST_TEMPLATE.md

Commits in this PR:
!git log main..HEAD --oneline

Changed files:
!git diff --stat main..HEAD

## Step 1: Pre-flight Checks

Check working tree status and whether a PR already exists for this branch.

```bash
git status --short
gh pr list --head "$(git rev-parse --abbrev-ref HEAD)" --json number,title,url
```

- If uncommitted changes exist, **abort** and prompt to commit first so the
  verified tree exactly matches the tree that will be pushed
- If a PR already exists, switch to **update mode** (`gh pr edit`) instead of creating a new one
- If on `main` branch, **abort** — PRs must come from feature branches

## Step 2: Quality Gate

Run the fast pre-PR gate. This is the prerequisite for PR creation.

```bash
just verify
```

`just verify` is non-mutating and runs `lint -> docs-check -> test-changed`
(fail-fast: the cheap gates come first). `test-changed` scopes pytest to the
tests this diff (vs the merge-base with `main`) can plausibly affect —
`scripts/diff_gate.py`'s rule table, falling back to the whole suite whenever
a path is unrecognized or a shared/build file changed — and gates the
*changed* source files at >=90% line+branch coverage.
**If any step fails, abort PR creation** and report the failure.

After it succeeds, run `git status --short` again. Any change means the pushed
branch would not match the verified state; stop and investigate.

On success, the following checklist items are verified:
- The tests this diff can affect pass, and its changed source files clear a
  90% line+branch coverage floor (`just test-changed`) — the repo-wide 95%
  floor and the wheel smoke test are CI-only concerns (`.github/workflows/ci.yml`
  runs both, plus the whole suite, on every PR); a gap the diff-scoped
  selection misses is a `scripts/diff_gate.py` rule-table fix, not something
  to chase locally with `just verify-full`
- Type checks and formatting pass (`just lint`)
- Documentation builds strictly (`just docs-check`)

## Step 3: Additional Verification

Analyze `git diff main..HEAD` to determine:

**Behavioral invariant impact:**
- `storage/**`: verify correction upserts, replacement deletion, and injected
  mid-batch rollback
- `data/**` or `text/**`: verify before/equal/after `as_of`, timeout, bounded
  retries/rate limiting, and offline execution
- `risk/**`: verify date-index alignment, minimum overlap, duplicates, and
  constant/NaN series
- `backtest/**`: verify no look-ahead, entry and exit costs, exit precedence,
  benchmark residual cash, and final liquidation equity
- `llm/**`: verify non-empty/known provenance, cache revalidation, CON-03 on all
  displayed fields, system/user separation, delimiter escaping, and redaction
- config or pipeline: verify fail-fast schema validation, fatal/fail-soft
  boundaries, and safe reruns

If a changed area lacks its applicable regression scenario, abort and report
the missing evidence instead of treating coverage percentage as sufficient.

**Public API changes:**
- Check if `__init__.py`'s `__all__` was modified
- Check if public function signatures changed
- If changes found: verify `docs/reference.md` or README was updated
  - If not updated: mark "Documentation updated" as unchecked and warn

**Breaking changes:**
- Detect deleted public functions, changed arguments, changed return types
- If found: note them explicitly in the PR Summary section

## Step 4: Generate PR Title

Generate a title in Conventional Commits format:

```
<type>(<optional-scope>): <short summary>
```

**Rules:**
- Analyze commits to select the most appropriate type
- Types: `feat`, `fix`, `docs`, `refactor`, `test`, `ci`, `chore`, `perf`, `build`
- If multiple types are mixed: use the type of the most significant change
- Keep under 70 characters
- Scope is optional (e.g., module name)

**Examples:**
- `feat: add JSON export support`
- `fix(core): handle empty input gracefully`
- `chore: add .claude/rules and post-edit hook`

## Step 5: Generate PR Body

Follow the PR template from dynamic context. Use Japanese or English according
to the language already used by the change; keep the PR internally consistent.

### Summary

Analyze commits and diff to describe the purpose and content in **1-3 lines**.
Focus on "why this change is needed" rather than "what was changed."

- Include `Closes #N` if a related issue number is known
- Explicitly note any breaking changes

### Test Plan

- If tests were added/modified: summarize what is being tested
- If no test changes: `Existing tests cover this change.` or similar

### Checklist

Fill each item based on verification results from Steps 2-3:

| Item | Criteria |
|------|----------|
| Fast local verification | `just verify` (diff-scoped) passed on the committed tree |
| Domain invariants reviewed | Every applicable changed-path scenario in Step 3 has evidence |
| Documentation updated | Required only when public API changed. No change = checked |
| No breaking changes | No breaking changes, or documented in Summary = checked |
| PR title follows Conventional Commits | Guaranteed by Step 4 |

**If any item is unchecked, abort PR creation** and report the issue.

## Step 6: Create or Update PR

```bash
# Push if not yet pushed
git push -u origin <current-branch>

# Create PR (new)
gh pr create --base main --title "<title>" --body "$(cat <<'EOF'
<body>
EOF
)"

# Or update PR (existing)
gh pr edit <number> --title "<title>" --body "$(cat <<'EOF'
<body>
EOF
)"
```

- Use HEREDOC to pass the body (preserves newlines and markdown)
- Display the PR URL after creation/update

## Notes

- This skill does NOT create commits — use the `smart-commit` skill for that
- Abort if attempting to create a PR from the `main` branch
- When a PR already exists for the current branch, update it with `gh pr edit`
