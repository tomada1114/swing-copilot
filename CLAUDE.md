@AGENTS.md

# Claude Code Specifics

Shared, tool-agnostic project instructions live in `AGENTS.md` (imported
above). This repo additionally ships Claude Code configuration:

- `.claude/rules/` — path-scoped conventions (Python, tests, docs,
  pyproject.toml) that load automatically when matching files are read
- `.claude/hooks/format.py` — auto-formats every edited `*.py` file
  (PostToolUse), so do not re-run formatters after each edit
- `.claude/hooks/guard.py` — blocks writes to `uv.lock`, `.env*`, and
  `secrets/**` (via Edit/Write or shell commands), `git commit --no-verify`,
  and plain force-pushes (PreToolUse)
- `.claude/hooks/stop_check.py` — runs ruff (lint + format check) and mypy
  before a turn ends when Python files changed (Stop)
- `.claude/skills/` — `create-pr`, `smart-commit`, and `merge-dependabot`
  workflow skills
- `.claude/settings.json` — shared permission allowlist for local build,
  lint, and test commands; personal preferences (model, output style, extra
  permissions) belong in `.claude/settings.local.json`, never here
