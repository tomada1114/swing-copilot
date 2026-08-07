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
  before a turn ends when Python files changed (Stop). This is intentionally a
  lightweight feedback loop, not completion evidence; use `just verify` before
  a PR or final completion claim
- `.claude/skills/` — `create-pr`, `smart-commit`, and `merge-dependabot`
  workflow skills
- `.claude/settings.json` — shared permission allowlist for local build,
  lint, and test commands; personal preferences (model, output style, extra
  permissions) belong in `.claude/settings.local.json`, never here

## Scheduled Daily Run

`/swing-daily` runs unattended on weekdays via a **local Claude Desktop Routine**
(`swing-copilot-daily`), which replaced the former launchd agent: folder = this
repository, cron `5 15 * * 1-5`, mode `Auto`, branch `main`, model Opus,
**Worktree off**. The routine's Instructions live in the Claude Desktop app and
are deliberately not mirrored here, to avoid drift.

Worktree stays off because `data/`, `.env`, and `.venv` are untracked — a clean
checkout would lose the API keys and the cached price/filing history and refetch
everything. So **this working copy is the execution environment**: keep it on
`main` and clean, and do feature work in a `git worktree` elsewhere. A scheduled
run aborts on its own if the branch is not `main`, or if `src`, `config`,
`pyproject.toml`, or `uv.lock` has uncommitted changes. A local routine only
fires while the machine is awake and online; unlike launchd, a missed run is
never retried later.

`AGENTS.md` is the canonical cross-tool contract for domain invariants,
source-of-truth precedence, test expectations, and the Japanese/English
language policy. Do not duplicate or weaken those rules here.
