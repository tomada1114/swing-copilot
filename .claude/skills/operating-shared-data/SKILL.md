---
name: operating-shared-data
description: >
  Covers the lifecycle of the shared canonical `data/` and `reports/` trees: pulling
  from and pushing to the private R2 bucket via `scripts/data_sync.py` (`just
  data-pull`/`data-push`/`data-status`), the single `manifest.json` and monotonic
  `generation` optimistic lock, DuckDB's exclusive file lock, `--reports-window` and
  `--reports-append-only`, and what a fresh worktree does and does not inherit. Use
  when running `just data-pull`/`data-push`/`data-status`, deciding whether to pull or
  push before a task, a `ConcurrentWriteError` or generation mismatch, setting up a new
  worktree or checkout, or recovering an out-of-window `reports/` archive.
---

# Operating Shared Data

**Owns:** the lifecycle of the shared canonical `data/` and `reports/` trees — pulling,
pushing, the generation lock, DuckDB's exclusive file lock, and what a fresh worktree
does and does not inherit. **Does not own:** what the stored rows mean or how they are
written in-process (`writing-storage-code`), running the daily pipeline itself
(`diagnosing-daily-runs`), answering research questions from the data (`swing-research`).

## The canonical copy is not in any working copy

`data/copilot.duckdb`, `data/bars/`, and the `reports/<run_date>/<run_id>/` daily run
archive all live in a private Cloudflare R2 bucket, not in any checkout. `data/` and the
`reports/` archive share **one** `manifest.json`, **one** monotonic `generation`, and one
`push`/`pull` commit (`scripts/data_sync.py`'s `SyncRoot` pair) — there is no separate
reports-side counter. `reports/latest.md` and everything under `reports/backtests/`,
`reports/dry_run/`, `reports/assets/`, and `reports/retro/` are local/derived and are
never synced; only `<date>/<run_id>.md` and `<date>/<run_id>/**` count as the archive.

## Two sittings, never a third shape

- **Read-only work** (ad-hoc research, the read-only dashboard): `just data-pull`, then
  read the local copy. `just data-status` confirms the copy still matches the remote
  before trusting it — a stale local copy silently answers questions about a `generation`
  that no longer exists.
- **Anything that writes** (a retrospective run, a live daily run): pull → work → push,
  **in one sitting**. The `generation` field is the only concurrent-write guard, covering
  `data/` and `reports/` together. Never leave a pulled copy unpushed, and never start a
  local write while the scheduled run holds the generation — a `push` from either side
  after the other has advanced fails as a `ConcurrentWriteError` and uploads nothing, but
  the fix (re-pull, redo the work) still costs the interrupted sitting's work.

Reject in review: a workflow that pulls, does something long-running or interactive, and
only later decides whether to push. The generation lock does not queue or merge — it
only detects. A held-open sitting is a held-open lock in spirit even though nothing
actually blocks the bucket.

## DuckDB's file lock, not `data_sync.py`'s

`copilot.duckdb` is moved as an opaque binary — `_sha256`'d, uploaded, downloaded,
verified — and is never opened as a database by `data_sync.py`. That is deliberate:
DuckDB's own file lock is exclusive between a read-write process and everything else, so
no exploration ever opens a read-write connection, and no connection — read-only or
read-write — is ever held across think-time. A connection left open while you read
output or wait on a subagent will fail the next `pull`/`push` on this machine outright,
and strands the local copy on a `generation` neither side can safely advance from.

## What a fresh worktree does not inherit

A fresh worktree or checkout has no `data/`, `reports/`, `.env`, or virtualenv of its
own. Populate it by installing dependencies, copying `.env` in by hand (it holds
credentials and is untracked, so it cannot travel with the branch), and running
`just data-pull` fresh. **Never** copy or symlink another checkout's `data/` or
`reports/` — that route bypasses the generation check entirely and can silently hand a
worktree a stale or mid-write tree with no record of what `generation` it actually holds.

## `--reports-window` is CI-only

`reports/<run_date>/<run_id>/` grows by one run every weekday forever (retention was
considered and rejected — nothing is ever deleted from it), so a fresh GitHub Actions
runner's full pull would grow linearly with calendar time. The scheduled workflow's pull
passes `--reports-window 10`, fetching `data/` in full plus only the `reports/` keys
belonging to the 10 most recent *run dates* (counted in actual runs, never calendar days,
so a holiday or a missed run cannot shrink the window below 10 real runs).

`justfile`'s `data-pull` recipe, and any local `scripts/data_sync.py pull`, never pass
this flag — a local working copy always pulls `reports/` in full. That is precisely what
lets an operator recover the windowed CI runner's blind spot: local state simply holds
more than CI's ever does.

The window actually used is recorded in `data/.r2_sync_state.json`
(`SyncState.reports_window`, whose `STATE_FILE_NAME` is `.r2_sync_state.json`),
not re-passed to `push` as a flag. The following `push` derives its behavior from that
record: `reports/`'s garbage collection is suppressed at the *key* level (an out-of-window
key is expected to be locally absent, not deleted — only a genuine orphan from an earlier
interrupted push is reclaimed), and `--reports-append-only`'s guard checks only the keys
the window actually fetched. A `push` that forgot the flag therefore cannot silently
garbage-collect real history it never even looked at.

The accepted trade-off: CI's own `copilot-retro collect` only ever sees the last 10 run
dates' worth of archives. Correcting an older archive — re-collecting a `reports/<date>`
outside that window — is an operator task, not something CI can do:

```bash
just data-pull            # no --reports-window: fetches reports/ in full
uv run copilot-retro collect
just data-push            # no --reports-append-only: correction is allowed
```

Do not run this with `--reports-append-only` — that guard exists specifically to stop an
unattended session from rewriting a published archive, and it would reject the very
correction this recovery path is for.

## Handoff

**REQUIRED:** `writing-storage-code` for what belongs in `data/copilot.duckdb` and how a
transaction/correction/replacement is implemented once bytes are on disk.
**BACKGROUND:** `diagnosing-daily-runs` for how the scheduled job orders this pull/push
around the analysis itself; `swing-research` for reading the pulled copy.
