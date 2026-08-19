# Development task runner — requires Just (https://just.systems)
# All commands also work without Just by running the uv commands directly.

# Show available recipes
default:
    @just --list

# Install dependencies and git hooks when available
install:
    uv sync --all-groups
    if git rev-parse --git-dir >/dev/null 2>&1; then uv run pre-commit install --install-hooks; else echo "Skipping pre-commit hook installation (not a Git repository)."; fi

# Alias for first-time project setup
setup: install

# Format code (lint fixes first so the formatter has the last word)
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# Run linters and type checker
lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src scripts tests

# Run tests in parallel with coverage (pytest-cov combines the workers' data)
test:
    uv run pytest -n auto --cov=swing_copilot --cov-branch --cov-report=term-missing:skip-covered --cov-fail-under=95

# Run all checks: format, lint, test
check: fmt lint test

# Serve documentation locally
docs:
    uv run mkdocs serve

# Build documentation and fail on warnings
docs-check:
    uv run mkdocs build --strict

# Build distribution packages
build:
    uv build

# Build and smoke-test the wheel in a temporary virtual environment
# (`--wheel` skips the sdist round-trip `just build` does; the smoke test only
# installs the wheel, and the sdist is still built by `just build` and release)
smoke:
    uv build --wheel
    uv run python scripts/smoke_test.py

# Non-mutating local release/PR gate (fail-fast: cheap gates before the suite)
verify: lint docs-check smoke test

# Remove build artifacts
clean:
    rm -rf dist/ build/ .mypy_cache/ .ruff_cache/ .pytest_cache/ htmlcov/ .coverage site/
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true

# Only merged branches are touched, so a worktree another session is still
# working in (open PR) is never removed; uncommitted work is kept and listed.
# Remove agent worktrees under .claude/worktrees whose PR is merged (--dry-run to preview)
worktree-clean *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    dry=0
    args="{{ARGS}}"
    for a in $args; do
      case "$a" in
        --dry-run) dry=1 ;;
        *) echo "unknown flag: $a (usage: just worktree-clean [--dry-run])" >&2; exit 2 ;;
      esac
    done

    repo_root=$(git rev-parse --show-toplevel)
    wt_root="$repo_root/.claude/worktrees"
    if [ ! -d "$wt_root" ]; then echo "no agent worktrees under $wt_root"; exit 0; fi

    default_branch=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
    default_branch=${default_branch:-main}
    git fetch --prune --quiet || true

    merged_refs=""
    open_refs=""
    if command -v gh >/dev/null 2>&1; then
      merged_refs=$(gh pr list --state merged --limit 200 --json headRefName -q '.[].headRefName' | sort -u || true)
      open_refs=$(gh pr list --state open --limit 200 --json headRefName -q '.[].headRefName' | sort -u || true)
    fi

    # Merged per gh (squash merges leave no ancestry), else contained in the
    # default branch. An open PR on the same ref always wins: still in flight.
    is_merged() {
      _br=$1
      if printf '%s\n' "$open_refs" | grep -qxF "$_br"; then return 1; fi
      if printf '%s\n' "$merged_refs" | grep -qxF "$_br"; then return 0; fi
      git merge-base --is-ancestor "$_br" "origin/$default_branch" 2>/dev/null
    }

    removed=0; kept=0; freed=0
    while IFS= read -r wt; do
      case "$wt" in "$wt_root"/*) ;; *) continue ;; esac
      br=$(git -C "$wt" symbolic-ref --short HEAD 2>/dev/null || true)
      if [ -z "$br" ]; then
        echo "KEPT (detached HEAD): $wt"; kept=$((kept + 1)); continue
      fi
      if ! is_merged "$br"; then
        echo "KEPT (no merged PR): $wt [$br]"; kept=$((kept + 1)); continue
      fi
      if [ -n "$(git -C "$wt" status --porcelain)" ]; then
        echo "KEPT (uncommitted changes — salvage first): $wt [$br]"; kept=$((kept + 1)); continue
      fi
      size=$(du -sm "$wt" 2>/dev/null | cut -f1); size=${size:-0}
      if [ "$dry" -eq 1 ]; then
        echo "DRY: would remove $wt [$br] (${size}MB)"
      else
        if ! git worktree remove "$wt"; then
          echo "KEPT (worktree remove failed): $wt [$br]"; kept=$((kept + 1)); continue
        fi
        git branch -D "$br" >/dev/null 2>&1 || true
        echo "removed: $wt [$br] (${size}MB)"
      fi
      removed=$((removed + 1)); freed=$((freed + size))
    done < <(git worktree list --porcelain | awk '/^worktree /{print $2}')

    if [ "$dry" -eq 0 ]; then git worktree prune; fi
    echo "worktree-clean: ${removed} removed, ${kept} kept, ~${freed}MB"
