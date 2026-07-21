# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial project structure
- `scripts/bootstrap.py` deterministic template initializer: renames the
  package and replaces every placeholder (`my-package`, `my_package`,
  `your-username`, `Your Name`, `you@example.com`) across tracked files
- Python 3.14 support in the CI test matrix and trove classifiers
- `zizmor` security lint for GitHub Actions workflows, wired into both CI
  and pre-commit
- `actions/dependency-review-action` on pull requests
- Weekly `pip-audit` dependency vulnerability scan
- Weekly OpenSSF Scorecard analysis
- PR auto-labeling by Conventional Commit type, so the release changelog
  categories actually populate
- `.devcontainer/devcontainer.json` for a ready-to-use dev environment
- `.github/ISSUE_TEMPLATE/config.yml` disabling blank issues and linking
  security reports to GitHub Security Advisories
- Dependabot cooldown and `tool.uv.exclude-newer` supply-chain cutoff,
  documented in `.claude/rules/pyproject.md`
- `AGENTS.md` as the canonical, tool-agnostic agent guide (previously a
  symlink to `CLAUDE.md`, which breaks on Windows checkouts)
- `.claude/hooks/guard.py` PreToolUse guard blocking writes to
  `uv.lock`/`.env*`/`secrets/**` (via Edit/Write or shell commands),
  `git commit --no-verify`, and plain force-pushes
- `.claude/hooks/stop_check.py` Stop-hook gate running ruff (lint + format
  check) and mypy before an agent turn ends when Python files changed
- Committed Claude Code permission allowlist covering local build, lint,
  and test commands only — commit/push/PR creation stay behind approval

### Changed

- Moved coverage enforcement (`--cov-fail-under=80`) out of pytest
  `addopts` and into `just test` / CI, so a single test can be run in
  isolation without failing the coverage gate
- Restructured the release pipeline: a dedicated `build` job now builds
  and attests provenance once; `publish` and the GitHub Release both
  consume that artifact instead of rebuilding
- Scoped all workflow permissions to job level, added `timeout-minutes`
  to every job, added `--locked` to every `uv sync` in CI, and disabled
  checkout credential persistence outside the docs deploy job
- Simplified `src/my_package/__init__.py`'s version resolution to the
  standard `importlib.metadata.version()` pattern, dropping the ~50-line
  local-pyproject-walking fallback chain
- Replaced the bespoke `no-commit-to-main` pre-commit hook with the
  pre-commit-hooks builtin `no-commit-to-branch`
- Unified mypy targets (`src scripts tests`) across justfile, CI,
  release, and pre-commit
- Expanded ruff rule set (`D`, `PT`, `N`, `TRY`, `EM`, `DTZ`, `RSE`,
  `PGH`) to match `.claude/rules/python.md`; renamed `TCH` -> `TC`
- The post-edit format hook now formats only the edited Python file and
  surfaces failures to the agent, replacing the repo-wide ruff run that
  suppressed all errors
- `CLAUDE.md` is now a thin `@AGENTS.md` import plus Claude Code
  specifics; `.claude/rules/python.md` no longer restates rules ruff
  already enforces mechanically
- `just fmt` now runs `ruff check --fix` before `ruff format` (ruff's
  recommended order, matching the post-edit hook), so lint autofixes can
  no longer leave formatting drift behind

### Fixed

- Switched to PEP 639 license metadata (`license-files`, dropped the
  redundant OSI trove classifier)
- `CONTRIBUTING.md`'s manual mypy command now includes `tests`, matching
  justfile/CI/pre-commit
- The `create-pr` skill re-checks the working tree after `just check` so
  formatting changes cannot be left uncommitted behind a green checklist

[Unreleased]: https://github.com/your-username/my-package/commits/main
