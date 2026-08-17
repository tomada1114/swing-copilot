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
  workflow skills, plus the trading loop (`swing-daily`, `swing-track`,
  `swing-retro`, `swing-deepdive`) and read-only data analysis
  (`swing-research`)
- `.claude/settings.json` — shared permission allowlist for local build,
  lint, and test commands; personal preferences (model, output style, extra
  permissions) belong in `.claude/settings.local.json`, never here

## Scheduled Daily Run

`/swing-daily` runs unattended on weekdays via a **local Claude Desktop Routine**
(`swing-copilot-daily`), which replaced the former launchd agent: folder = this
repository, cron `30 18 * * 1-5`, mode `Auto`, branch `main`, model Opus,
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

Two consequences for feature work done in a worktree:

- A worktree has no `data/`, `.env`, or `.venv`. If a task there needs the
  cached price/filing history (e.g. regenerating a backtest report), **copy**
  the main checkout's `data/` into the worktree (`cp -R`); never symlink it,
  and never open the main checkout's `data/copilot.duckdb` from a worktree —
  its file lock is exclusive and a held handle can fail the 18:30 routine.
- After a PR merges to `main`, fast-forward this working copy
  (`git fetch --prune && git pull --ff-only`) so the next scheduled run
  executes the merged code instead of a stale checkout.

`AGENTS.md` is the canonical cross-tool contract for domain invariants,
source-of-truth precedence, test expectations, and the Japanese/English
language policy. Do not duplicate or weaken those rules here.

`copilot-daily` exits `2` (preflight abort) for two different reasons, and
stderr's first line carries a machine-readable tag the `swing-daily` skill
branches on — never assume exit 2 means "already ran":

- `PREFLIGHT_ABORT[same_day_rerun]:` — a `status='success'` run already exists
  for the resolved `run_date` (Issue #118: the `swing-copilot-daily` routine
  only fires once, but a cron edit or manual re-run of a completed day would
  otherwise write a second `verdicts` set). It exits before creating a `runs`
  row or `reports/` directory. The skill summarizes the existing run and
  terminates without writing `analysis_result.json`.
  `--allow-same-day-rerun` bypasses the guard for an intentional re-run.
- `PREFLIGHT_ABORT[account_equity_unset]:` — `risk.account_equity_usd` is
  unset while closed positions exist; continuing would only produce
  circuit-breaker-forced rejections. This is a configuration problem the
  skill must report to the user, **not** an "already analyzed" summary.

## Reading the Accumulated Data

Ad-hoc analysis of the DuckDB history (verdict outcomes, score breakdowns,
tracking ledger, regimes, rejections) goes through `swing_copilot.research` —
read-only, one connection per query, joined views included. Use the
`swing-research` skill for these questions; `docs/09_research_guide.md` is the
canonical how-to and data dictionary. Never open a raw read-write
`duckdb.connect()` against `data/copilot.duckdb` for exploration, and never
hold any connection across think-time: this working copy is the unattended
execution environment, and DuckDB's file lock is exclusive between a
read-write process and everything else, so a held connection can make the
18:30 routine fail its whole day. Improvement work discovered while analyzing
follows `docs/08_architecture_review_2026-08.md`'s principles (no config
changes on point estimates alone; route proposals through issues or
`swing-retro`).
