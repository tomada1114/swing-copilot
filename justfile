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

# Run tests with coverage
test:
    uv run pytest --cov=my_package --cov-branch --cov-report=term-missing:skip-covered --cov-fail-under=80

# Run all checks: format, lint, test
check: fmt lint test

# Serve documentation locally
docs:
    uv run mkdocs serve

# Build distribution packages
build:
    uv build

# Build and smoke-test the wheel in a temporary virtual environment
smoke: build
    uv run python scripts/smoke_test.py

# Remove build artifacts
clean:
    rm -rf dist/ build/ .mypy_cache/ .ruff_cache/ .pytest_cache/ htmlcov/ .coverage site/
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
