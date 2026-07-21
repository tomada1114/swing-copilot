---
name: create-pr
description: >
  Create or update pull requests following project conventions. Runs pre-checks
  (just check), generates Conventional Commits title, fills PR template with
  summary/test plan/checklist, and verifies all checklist items pass before
  creating via gh CLI. Use PROACTIVELY when: PR creation, pull request,
  create PR, open PR, submit PR, PR update, review request.
---

# PR Creation Workflow

All PR titles, bodies, and commit messages MUST be written in English.

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

- If uncommitted changes exist, **abort** and prompt to commit first
  (running `just check` on uncommitted code can cause inconsistencies)
- If a PR already exists, switch to **update mode** (`gh pr edit`) instead of creating a new one
- If on `main` branch, **abort** — PRs must come from feature branches

## Step 2: Quality Gate

Run the full quality check suite. This is the prerequisite for PR creation.

```bash
just check
```

`just check` runs `fmt -> lint -> test` sequentially.
**If any step fails, abort PR creation** and report the failure.

The `fmt` phase can modify tracked files. After `just check` succeeds, run
`git status --short` again — if formatting produced changes, commit them
(e.g. `style: apply formatting`) before proceeding, otherwise the pushed
branch will not match the verified state.

On success, the following checklist items are verified:
- Tests pass (`just test`)
- Type checks pass (`just lint`)
- Code is formatted (`just fmt`)

## Step 3: Additional Verification

Analyze `git diff main..HEAD` to determine:

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

Follow the PR template from dynamic context. Write everything in English.

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
| Tests pass | `just test` passed |
| Type checks pass | `just lint` passed |
| Code is formatted | `just fmt` passed |
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
