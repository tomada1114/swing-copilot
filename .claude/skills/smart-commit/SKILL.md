---
name: smart-commit
description: >
  Analyze working tree changes, group them into logical atomic commits with
  Conventional Commits messages, and push. Handles staging, sensitive file
  exclusion, and uv.lock bundling automatically. Use PROACTIVELY when:
  commit, git commit, save changes, commit and push, stage changes,
  push my changes, commit this work, ship it.
---

# Smart Commit Workflow

All commit messages must be written in English.

## Dynamic Context

Current branch:
!git rev-parse --abbrev-ref HEAD

Working tree status:
!git status --short

Recent commit style:
!git log --oneline -5

## Branch Guard

**Never commit directly to `main`.** If the current branch is `main`, abort
immediately and tell the user to create a feature branch first. The pre-commit
hook also enforces this, but check early to avoid wasted work.

## Step 1: Analyze Changes

Review staged and unstaged changes to understand what was modified.

```bash
git status
git diff          # unstaged changes
git diff --cached # staged changes
```

If changes are already staged, prioritize those — the user has expressed intent
about what to commit. If nothing is staged, treat all modified/untracked files
as candidates.

## Step 2: Sensitive File Check

Never commit files that could contain secrets:

- `.env`, `.env.*` (environment variables)
- `**/credentials.json`, `**/secrets.json`, `**/private-key.*`
- Files matching `*password*`, `*secret*`, `*key*.pem`

If any are detected among the candidates, **exclude them** and warn the user.
Everything else — config files, source code, docs, test files — should be
committed. Prefer committing work-in-progress over leaving it uncommitted.

## Step 3: Group Changes

Analyze the changes and group them into logical, atomic commits. Each commit
should be independently meaningful. The goal is to tell a clear story of what
happened through the commit history.

**Grouping rules:**

- **Tests** (`tests/`): prefix `test:`
  - New tests → `test: add ...`
  - Fixed tests → `test: fix ...`

- **Documentation** (`.md`, `docs/`): prefix `docs:`

- **Configuration** (`.json`, `.yml`, `.claude/`): prefix `chore:`

- **Dependencies** (`pyproject.toml` dependency changes): prefix `chore:`
  - Always include `uv.lock` in the same commit

- **Build system** (`pyproject.toml` build/tool config, `justfile`): prefix `build:`

- **Source code** (`src/`):
  - New feature → `feat:`
  - Bug fix → `fix:`
  - Refactor → `refactor:`
  - Performance → `perf:`

- **CI/CD** (`.github/`): prefix `ci:`

**Special cases:**
- `uv.lock` changes must be in the same commit as the `pyproject.toml`
  dependency change that caused them
- A source file and its corresponding test file can go in the same commit
  when they represent a single logical change (use `feat:` or `fix:` prefix)

If all changes are closely related, a single commit is fine. Don't split
artificially — three related one-line changes are better as one commit than
three separate commits.

## Step 4: Create Commits

For each group, stage the relevant files and commit:

```bash
git add <file1> <file2> ...
git commit -m "$(cat <<'EOF'
<type>(<optional-scope>): <short summary>
EOF
)"
```

**Commit message format:**
- Conventional Commits: `<type>(<optional-scope>): <short summary>`
- Types: `feat`, `fix`, `docs`, `refactor`, `test`, `ci`, `chore`, `perf`, `build`
- Summary: imperative mood, lowercase start, no period at end
- Under 72 characters
- Focus on *what* changed, not *how*

**Examples:**

```
feat(core): add JSON export support
fix: handle empty input without raising TypeError
test: add parametrized tests for edge cases
docs: update API reference for new export function
chore: configure .claude/rules for path-scoped linting
```

Stage specific files by name — avoid `git add .` or `git add -A` which can
accidentally include sensitive files or unrelated changes.

## Step 5: Push (if requested)

Only push if the user explicitly asked to push (e.g., "commit and push",
"ship it"). If the user only said "commit", skip this step — they may want
to make more commits before pushing, or use the `create-pr` skill which
handles push on its own.

```bash
git push
# or if no upstream:
git push -u origin <current-branch>
```

## Step 6: Verify

Show the final state to confirm everything is clean:

```bash
git status
git log --oneline -<number-of-new-commits>
```

Report any remaining uncommitted files and explain why they were excluded
(should only be sensitive files).

## Pre-commit Hook Interaction

This project has pre-commit hooks (ruff, ruff-format, mypy, typos, etc.) that
run automatically on `git commit`. The skill does NOT duplicate these checks —
the hooks handle code quality, while the skill handles commit workflow.

**When a hook fails:**

1. The commit did NOT happen — staged files remain staged but uncommitted
2. Some hooks (ruff --fix, trailing-whitespace, end-of-file-fixer) auto-fix
   files. These fixes are unstaged modifications after the failed commit
3. Re-stage the auto-fixed files: `git add <fixed-files>`
4. Create a NEW commit (same message is fine) — never `--amend`
5. Never use `--no-verify` to skip hooks — fix the underlying issue instead

**Common hook failures and fixes:**
- `ruff`: fix the lint error reported, or the hook may have auto-fixed it
- `mypy`: fix the type error in source code
- `typos`: fix the typo, or add the word to `typos.toml` if it's a false positive

## Notes

- When in doubt about grouping, fewer larger commits are better than many tiny ones
