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
  workflow skills, plus the trading loop (`swing-daily`, `swing-retro`,
  `swing-deepdive`) and read-only data analysis (`swing-research`)
- `.claude/settings.json` — shared permission allowlist for local build,
  lint, and test commands; personal preferences (model, output style, extra
  permissions) belong in `.claude/settings.local.json`, never here

## Scheduled Daily Run

`/swing-daily` runs unattended on weekdays in GitHub Actions
(`.github/workflows/swing-daily.yml`): cron `0 23 * * 1-5` UTC — JST Tue–Sat
8:00, two to three hours after the US close — plus `workflow_dispatch` for a
manual run. Those are the only two triggers; the repository is public, so no
path a third party can pull. Nothing is retried automatically: a failed or
skipped day is re-dispatched by hand, and #277's preflight makes the gap
visible in the next run.

The canonical `data/` lives in a private Cloudflare R2 bucket, not in any
working copy. The workflow pulls it before the analysis and pushes it back
only on success, so a failed day leaves the remote on the previous generation.

### Working with the data locally

- Read-only work (`swing-research`, the dashboard): `just data-pull`, then read
  the local copy. `just data-status` says whether it still matches the remote.
- Anything that writes (`swing-retro`, a live `copilot-daily`): `just data-pull`
  → work → `just data-push`, in one sitting.
  The optimistic lock in `scripts/data_sync.py` — a monotonic `generation` in
  `manifest.json` — is the only concurrent-write guard, so do not leave a
  pulled copy unpushed, and do not start a local write around JST 8:00 while
  the scheduled run holds the generation.
- Never open `data/copilot.duckdb` as a read-write DuckDB connection for
  exploration, and never hold any connection across think-time. The file lock
  is exclusive between a read-write process and everything else, so a held
  handle fails the next `just data-pull` / `data-push`. Sync always moves the
  file as bytes, never through DuckDB.
- A `git worktree` has no `data/`, `.env`, or `.venv`. Run `just install`
  there, copy `.env` in by hand (it holds the R2 and API credentials, and it is
  untracked), and get the history with `just data-pull` — never by copying or
  symlinking another checkout's `data/`.

`AGENTS.md` is the canonical cross-tool contract for domain invariants,
source-of-truth precedence, test expectations, and the Japanese/English
language policy. Do not duplicate or weaken those rules here.

`copilot-daily` exits `2` (preflight abort), and stderr's first line carries
a machine-readable tag the `swing-daily` skill branches on — never assume
exit 2 means "already ran":

- `PREFLIGHT_ABORT[same_day_rerun]:` — a `status='success'` run already exists
  for the resolved `run_date` (Issue #118: the schedule fires once per weekday,
  but a manual dispatch or a re-run of a completed day would otherwise write a
  second `verdicts` set). It exits before creating a `runs`
  row or `reports/` directory. The skill summarizes the existing run and
  terminates without writing `analysis_result.json`.
  `--allow-same-day-rerun` bypasses the guard for an intentional re-run.

## Reading the Accumulated Data

Ad-hoc analysis of the DuckDB history (verdict outcomes, score breakdowns,
tracking ledger, regimes, rejections) goes through `swing_copilot.research` —
read-only, one connection per query, joined views included. Use the
`swing-research` skill for these questions; `docs/09_research_guide.md` is the
canonical how-to and data dictionary. Never open a raw read-write
`duckdb.connect()` against `data/copilot.duckdb` for exploration, and never
hold any connection across think-time: DuckDB's file lock is exclusive between
a read-write process and everything else, so a held connection fails the next
`just data-pull` / `data-push` and strands the local copy on a stale
generation. Improvement work discovered while analyzing
follows `docs/08_architecture_review_2026-08.md`'s principles (no config
changes on point estimates alone; route proposals through issues or
`swing-retro`).
