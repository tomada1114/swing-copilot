---
name: diagnosing-daily-runs
description: >
  Covers reading the outcome of a scheduled or manually dispatched `swing-daily.yml`
  run: exit code 2 and the `PREFLIGHT_ABORT[<reason>]:` stderr tag, the three
  `PreflightAbortReason` values (`same_day_rerun`, `no_trading_day`,
  `price_fetch_failed`), `scripts/check_daily_complete.py`'s legitimate-stop whitelist,
  and the `--outcome-file`/`COPILOT_DAILY_OUTCOME_FILE` terminal-outcome JSON. Use when
  a `swing-daily` CI job goes red or green unexpectedly, deciding whether a
  `PreflightAbort` needs a manual re-dispatch, reading `copilot-daily`'s exit code, or
  investigating why `check_daily_complete.py` failed or passed a job.
---

# Diagnosing Daily Runs

**Owns:** reading the outcome of a scheduled or manually dispatched daily run — the exit
codes, the `PREFLIGHT_ABORT[...]` stderr tags, the outcome file, and what to do next in
each case. **Does not own:** the qualitative analysis procedure itself (`swing-daily`),
the R2 sync mechanics around the run (`operating-shared-data`), the pipeline's internal
composition (`wiring-the-pipeline`).

## Exit 2 is never "already ran"

`copilot-daily` exits `2` on a preflight abort, and the first line of stderr carries the
machine-readable `PREFLIGHT_ABORT[<reason>]:` tag — never infer the reason from exit code
alone, and never assume exit 2 means the day already succeeded. Two of the three reasons
are legitimate stops; one is a genuine failure that happens to share the exit code. Read
the tag.

## The three reasons

| Reason | What actually happened | Job stays green? | Next action |
| --- | --- | --- | --- |
| `same_day_rerun` | A successful run already exists for the resolved `run_date`. Exits before creating a run record or report directory. | Yes — legitimate stop. | None needed. An intentional re-run passes `--allow-same-day-rerun` to bypass the guard. |
| `no_trading_day` | The price prefetch succeeded, but no fetched bar belongs to a session that has actually closed (16:00 America/New_York) — either the prefetch came back empty, or the newest bar is still mid-session because the job started late. | Yes — legitimate stop. | None automatic. The next scheduled run resolves it, or dispatch manually once a session has closed. |
| `price_fetch_failed` | The price prefetch itself raised (e.g. a data-provider outage), so whether any session had closed could not even be determined. | **No** — `scripts/check_daily_complete.py` fails the job on this reason. | Investigate the provider outage; re-dispatch by hand once resolved. |

`no_trading_day` and `price_fetch_failed` look similar from the outside (both fire before
`run_date` resolves, both abort before any state is written) but mean opposite things: one
is "nothing to analyze yet," the other is "we could not tell." Conflating them would let a
provider outage pass as a clean day with nothing analyzed — the exact failure the split
exists to prevent. `run_date` itself is never the wall clock and never merely "the newest
bar fetched" — either would risk booking a day before its session actually closed.

## The legitimate-stop check is a whitelist

`scripts/check_daily_complete.py`'s `_LEGITIMATE_STOP_REASONS` names exactly two values:
`same_day_rerun` and `no_trading_day`. This is a whitelist, not `outcome ==
"preflight_abort"` — an unrecognized or missing `reason`, `price_fetch_failed` included,
fails closed the same way an actual crash would. Reject in review: widening this to "any
`preflight_abort` passes" or adding a new `PreflightAbortReason` (see `designing-errors`)
without also deciding, explicitly, which side of this whitelist it belongs on.

## Nothing retries automatically

A failed or skipped day is re-dispatched by hand — there is no cron catch-up. The next
scheduled run's own preflight makes the gap visible (a `same_day_rerun` guard only fires
once a successful run exists; a missed day simply has none, so the following run resolves
`run_date` to the missed session if it is still the most recent unclosed one, or moves on).

## The outcome file closes a gap the DB alone cannot

`copilot-daily` writes its own terminal outcome — `success`/`degraded`/`failed`/
`preflight_abort`, plus the `reason` when applicable — to a JSON file **outside**
`reports/<run_date>/<run_id>/`, whenever `--outcome-file` (or its
`COPILOT_DAILY_OUTCOME_FILE` environment fallback, which CI always sets) is configured.
This exists because `reports/` is now pulled from R2 in full at job start: without it, a
workspace holding *previous* days' `analysis_result.json` files could make
`check_daily_complete.py` mistake yesterday's report for today's success. The outcome
file lets the checker distinguish "the pipeline never even started" (file missing) from
"it started and legitimately found no trading day yet" (`preflight_abort` with a
whitelisted reason) — without relying on the headless analysis session to self-report
anything. A missing outcome file, an unreadable one, or one recording a non-whitelisted
abort reason all fail the check outright rather than falling through to the DB-based
candidate check.

## Schedule and job ordering

The workflow fires the day *after* the US session it analyzes, so the weekday mask
includes Saturday to cover Friday's session — a Monday-through-Friday mask would miss it.
This schedule, plus manual dispatch, are the only two triggers.

Inside the job: pull from R2 → run the analysis → `copilot-retro collect` (fills
`verdicts`; the only path that does, per design decision D2) → push to R2, on success
only. A failed day therefore leaves the remote `generation` on the previous day's value —
nothing partial is ever published. `check_daily_complete.py` itself runs **after** the
push, deliberately: a half-finished day's prices/fundamentals/ledger are still worth
persisting, so the data goes up first and the job is failed loudly afterward.

**BACKGROUND:** `designing-errors` for the `PreflightAbort`/`PreflightAbortReason` class
shape itself; `operating-shared-data` for what the pull/push around this job actually
moves; `enforcing-point-in-time` for the `as_of` discipline the resolved `run_date` then
feeds (this skill owns `run_date` resolution itself).
