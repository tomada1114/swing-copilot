# Contributing

Thank you for considering a contribution! This document explains how to set up
your development environment and submit changes.

## Prerequisites

Install these tools:

- [Python 3.14+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Just](https://just.systems/man/en/installation.html) (optional — you can run
  `uv run` commands directly)

Then:

```bash
uv sync --all-groups
```

If you're working in a Git checkout, also install the local hooks:

```bash
uv run pre-commit install --install-hooks
```

## Development Workflow

```bash
# Format and auto-fix
just fmt

# Lint + type check
just lint

# Run tests
just test

# Build and verify the wheel in an isolated temp environment
just smoke

# Mutating development check (format → lint → test)
just check

# Non-mutating PR/release gate (lint + test + strict docs + wheel smoke)
just verify
```

**Without Just**, run the equivalent commands:

```bash
uv run ruff check --fix .
uv run ruff format .
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
uv run pytest --cov=swing_copilot --cov-branch --cov-report=term-missing:skip-covered --cov-fail-under=95
uv run mkdocs build --strict
uv build && uv run python scripts/smoke_test.py
```

## Pull Request Process

1. Fork the repository and create a branch from `main`
2. Make your changes
3. Apply formatting with `just fmt`, commit the result, then ensure `just verify` passes
4. Write or update tests for your changes
5. Open a pull request using the PR template

### Code Standards

- All public functions and methods must have type annotations
- mypy strict mode must pass
- Ruff must pass with no warnings
- Maintain or improve test coverage (minimum 95%)

### Commit Messages

Use Conventional Commits for both commits and PR titles:

```
<type>(<optional-scope>): <short summary>
```

Examples:

- `feat: add JSON export support`
- `fix(api): handle empty input`
- `docs: update installation guide`

Recommended types: `feat`, `fix`, `docs`, `refactor`, `test`, `ci`, `chore`,
`perf`, `build`.

Code identifiers and public API names use English. Prose may use Japanese or
English in documentation, comments/docstrings, commit summaries, PR titles,
and PR bodies. Keep Conventional Commit type/scope tokens in English and keep
one prose language consistent within a document or PR.

### Changelog Policy

`CHANGELOG.md` (in [Keep a Changelog](https://keepachangelog.com/) format) is
the canonical, human-curated record of user-facing changes. Add an entry
under `[Unreleased]` for any user-facing change in the same PR that makes it.

GitHub's auto-generated release notes (via `.github/release.yml` categories)
are supplementary — useful for a quick PR-by-PR diff, but `CHANGELOG.md` is
what users should read to understand what changed in a release.

## Getting Help

If something is unclear, open an issue or start a discussion. We're happy to
help you get started.
