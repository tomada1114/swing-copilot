<!-- agents-md-sync:begin -->
@AGENTS.md
<!-- agents-md-sync:end -->

# Claude Code Specifics

Shared, tool-agnostic project instructions live in `AGENTS.md` (imported
above). This repo additionally ships Claude Code configuration:

- `.claude/hooks/format.py` — auto-formats every edited `*.py` file
  (PostToolUse), so do not re-run formatters after each edit
- `.claude/hooks/guard.py` — blocks writes to `uv.lock`, `.env*`, and
  `secrets/**` (via Edit/Write or shell commands), `git commit --no-verify`,
  and plain force-pushes (PreToolUse)
- `.claude/hooks/stop_check.py` — runs ruff (lint + format check) and mypy
  before a turn ends when Python files changed (Stop). This is intentionally a
  lightweight feedback loop, not completion evidence; use `just verify`
  (diff-scoped) before a PR, or `just verify-full` (the whole suite, no
  scoping) before a release or a direct-to-main final completion claim
- `.claude/skills/` — `create-pr`, `smart-commit`, and `merge-dependabot`
  workflow skills, plus the trading loop (`swing-daily`, `swing-retro`,
  `swing-deepdive`) and read-only data analysis (`swing-research`)
- `.claude/settings.json` — shared permission allowlist for local build,
  lint, and test commands; personal preferences (model, output style, extra
  permissions) belong in `.claude/settings.local.json`, never here
