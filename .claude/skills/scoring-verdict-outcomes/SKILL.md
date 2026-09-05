---
name: scoring-verdict-outcomes
description: >
  Covers the feedback loop's accounting: src/swing_copilot/retro/** (collect,
  evaluate, ledger, validate, ingest, adoption, schemas) and
  src/swing_copilot/tracking/** (update, board, cli) — how a proceed/skip
  verdict's outcome is classified, how the virtual position ledger replays a
  verdict forward, split re-basing, the verdict_outcomes audit columns, and the
  proposal ledger's status lifecycle. Use when changing retro/collect.py,
  retro/evaluate.py, retro/ledger.py, tracking/update.py, tracking/board.py,
  VerdictOutcomeRecord, VerdictPosition, or reviewing a diff under retro/** or
  tracking/**.
---

# Scoring Verdict Outcomes

**Owns:** `src/swing_copilot/retro/**` and `src/swing_copilot/tracking/**` —
the virtual ledger's accounting, how a verdict's outcome is measured after the
fact, and the proposal ledger's rules. **Does not own:** the retrospective
run's procedure and its approval gates (`swing-retro`, an existing workflow
skill — point at it, never restate it), the strict schemas at the analysis
boundary (`guarding-analysis-boundary`), DuckDB write mechanics
(`writing-storage-code`), the `as_of` predicate itself
(`enforcing-point-in-time`), answering ad-hoc questions from the history
(`swing-research`).

## A virtual ledger holds no money and places no orders

`tracking/update.py`'s docstring states the boundary directly: a verdict is
treated as a purchase at that run's closing price, unconditionally, with no
fill simulation and no gate on the planned `limit_price`. `verdict_outcomes`
(`retro/evaluate.py`) is further removed still — a two-point 5/20-session
classification, not a position at all. Nothing here rewrites configuration,
code, or any deterministic screening/sizing/ranking value (both packages'
`__init__.py` state this as a charter).

That a ledger is virtual is exactly why it must be accounted for as precisely
as a real one: its only output is a number a retrospective reads as "the
verdict layer is right N% of the time", and a sloppy ledger does not fail
loudly — it produces a hit rate that is confidently wrong. A stop tested
against a pre-split price, an entry price silently drifted off the bar it
claims to be, or a same-day duplicate counted twice all look like ordinary
output: a table with numbers in it. The three recent fixes below exist
because each of those silently corrupted a published statistic before anyone
noticed.

## D2: `copilot-retro collect` is the only path that fills `verdicts`

`copilot-ingest-analysis` never opens a database connection
(`guarding-analysis-boundary` owns that boundary). `retro/collect.py` is the
sole writer of `verdicts`/`verdict_sources`: it walks
`reports/<date>/<run_id>/`, re-parsing a run only when its two documents'
digest no longer matches what was last collected (`_document_digest`, Issue
#209). Keeping this a separate, deferred step — rather than folding it into
ingest — lets a corrected `analysis_result.json` be picked up by re-running
the scan, and makes `verdicts` safe to rebuild from the gitignored `reports/`
archive after any DuckDB repair. If something other than `collect` ever wrote
`verdicts` directly, that guarantee is gone: the row could describe bytes no
longer on disk, and a re-scan could no longer prove it was reproduced from
the archive rather than typed by hand.

## Accounting invariants the recent fixes established

- **A frozen entry price is checked against its own day's bar, not a ratio.**
  `_seed_position` compares a newly-tracked verdict's
  `risk_assessments.entry_price` to the stored bar's close for that *same*
  entry date (`_ENTRY_PRICE_BAR_TOLERANCE = 0.005`, Issue #423). Both claim to
  be one session's close, so a disagreement beyond tolerance is not a market
  move to interpret — it falls back to the bar's close and is recorded in
  `notes`, never silently substituted.
- **A split re-bases an already-open position, including the very first one.**
  `_rebase_position` divides `entry_price`, `stop_price`, and every published
  mark by the cumulative split factor since `last_marked_date` (Issue #413).
  `_seed_position` applies the same division to a verdict tracked for the
  first time (Issue #420) — without it, `copilot-track rebuild` reopens a
  position with a pre-split stop against post-split bars and stops it out
  instantly, reproducing the corruption the rebuild exists to remove.
  Re-basing always comes from the *events* (`MarketStore.read_splits`), never
  from inferring a ratio from price jumps — that inference is what
  `data/adjustments.py::has_mixed_basis_signature` gates at write time
  (`writing-storage-code`), not something this layer repeats.
- **`copilot-track rebuild` reconstructs a position, it does not patch one.**
  `rebuild_positions` deletes the named positions and lets the ordinary
  open-and-advance path reopen them from `risk_assessments` and replay every
  session from entry, because `update_tracking` never revisits an
  already-marked session (`last_marked_date` is a resume point) — so a stop
  fired at a price nothing ever traded at can only be fixed by a full replay,
  not by editing one row. Delete and replay are two separate write sets, not
  one transaction: an interruption between them leaves positions deleted but
  recoverable, since the source `verdicts` rows are untouched and the next
  `copilot-track update` reopens them exactly as the replay would have.

## Point-in-time in this layer

`evaluate_verdicts` reads only bars dated `<= request.as_of` and fixes each
horizon's return at its own maturity session (`find_maturity_trading_day`)
rather than the observation date, so re-running the batch later reproduces
the same rows instead of leaking a later session into a shorter horizon.
`tracking/update.py` reads through the same `MarketStore.read_bars(...,
as_of)` boundary. The classic bug this layer rejects: scoring a verdict
against a price the market had not printed yet (an unmatured horizon,
reported as `pending_slice_count`, never as a note), or against the wrong
adjustment basis (a raw entry price scored against bars `read_bars` already
adjusted — the re-basing invariants above exist to reconcile the two).
**BACKGROUND:** `enforcing-point-in-time` for the `<=` predicate;
`writing-storage-code` for how `read_bars` adjusts on read.

## Audit columns: an evaluation that cannot be reproduced is not evidence

`verdict_outcomes.entry_close`/`maturity_close` (Issue #413) store the exact
two closes `compute_forward_return_detail` divided to produce
`forward_return_pct` — quoted on the *maturity date's* adjustment basis, not
the as-traded price of the run day. They feed no aggregate; they exist so
"which price was this classified at" survives a later store repair that
rebases the bars underneath it. `NULL` means the row predates the columns and
is never backfilled — recomputing today's basis and writing it in as the
historical value would misrepresent what the original evaluation actually
divided. The same discipline applies to `benchmark_return_pct`: `NULL` means
"not measured", never a flat market. Treat any change to `_evaluate_slice`
that could make a stored classification unreproducible from its own audit
columns as a break in this layer's central promise — a number nobody can
retrace is not a track record, it is an assertion.

## Idempotence and rerun safety

- **`collect`** is safe to re-run on an unchanged archive (a byte-identical
  digest skips the re-parse and re-write) and after a correction (any edit to
  either document changes the digest and forces a full re-parse-and-replace).
  A same-day duplicate run directory is resolved by
  `retro/adoption.py::adopt_one_run_per_date` (latest `runs.started_at` wins,
  ties break on the greater `run_id` string) — and a run this scan does not
  adopt is *never touched*, so any window read must re-apply the same rule
  (`keep_adopted_rows`) or it double-counts the loser's rows forever. Both
  `evaluate_verdicts` and `retro/aggregate.py`'s window readers do this; a new
  reader that queries `get_verdicts_in_window` directly, without calling
  `keep_adopted_rows`, reproduces the exact bug Issue #124 found.
- **`evaluate`** replaces a whole `(run_id, horizon_days)` slice at once
  (`replace_verdict_outcomes`), so a re-run after a price correction
  reclassifies every symbol in the slice together, never one row. The daily
  step's `only_pending=True` scope skips a slice only when its recorded
  outcomes already match the run's current verdicts exactly
  (`get_recorded_outcome_slices`) — a corrected verdict (added, dropped, or
  flipped `proceed`/`skip`) stops matching and is reclassified. A symbol whose
  maturity-day close later goes missing from the store is the one exception
  to "replace the whole slice": `_evaluate_slice` cannot recompute it, but
  simply omitting it would let the full-slice replace silently delete its
  previously recorded row (Issue #424 — the MNST 2026-08-10 case, a
  `copilot-backfill rebuild` re-fetch that dropped a historical bar).
  `get_verdict_outcomes_for_slice` reads that old row back and carries it
  forward unchanged, but only when its `recommendation` still matches the
  current verdict — a verdict correction on top of the missing bar leaves
  nothing trustworthy to carry forward, and the row is dropped exactly as
  before.
- **`update_tracking`** never revisits a session before `last_marked_date`, so
  re-running it at the same `--as-of` is a no-op for every already-marked
  position and adds no duplicate marks.

## The proposal ledger

`docs/retro/proposals.md` (`retro/ledger.py`) is history, audit trail, and
duplicate suppressor — **never an approval gate itself**. `ingest` only ever
appends rows with `status=proposed` (D10); every later status
(`applied`/`rejected`/`deferred`/`verification_failed`/`merged`/`reverted`) is
written by the applying skill or a human, so `read_ledger` parses
structurally (matching `RP-\d+` and the documented status vocabulary) rather
than by column position — a reordered column must not silently blind the
re-proposal guard. A `proposal_key` already carried by an *open* row reuses
its RP-ID on re-ingest (idempotent); a key whose only rows are closed
(`rejected`/`verification_failed`) is a reopening and gets a fresh RP-ID,
which is why `retro/validate.py`'s re-proposal guard runs first — a
reopening without `reopen_justification` is withheld, not silently recorded.

The three levels (`retro/schemas.py`'s `ProposalLevel`) are a **scope**
classification, and their gate is about **who must approve**, not about how
technically hard the change is:

- **L1** — an existing config value (threshold, weight, budget). No prior
  approval; the skill applies it on its own branch, verifies, and opens a PR.
- **L2** — a compositional change (add/remove a metric, signal, filter, news
  source; change the analysis schema or the skill's own procedure). Needs an
  explicit design-direction approval step before it is applied.
- **L3** — an architecture or evaluation-framework-level design review. Same
  approval gate as L2, plus at least two compared alternatives.

The run procedure for exercising this — evidence gates, the approval prompt,
branch/PR mechanics — belongs entirely to `swing-retro`; do not restate it
here. What belongs here is that the levels exist to route review effort,
not to measure the size of the diff: a one-line config edit and a one-line
schema field can both be L1 or L2 depending on what they compose, not on line
count.

## No configuration changes on a point estimate alone

`verdict_outcomes` and the ledger's aggregates never write back into
`config.py` directly — there is no automated path from a measured hit rate to
a changed threshold (`docs/04_detailed_design.md` §3.23 states this
explicitly: no feedback loop exists that lets the aggregate rewrite what it
measures). Every change is a *proposal*, reviewed by a human through the
ledger's approval gates above. Defend this in review: a component that both
measures its own performance and can silently retune itself on that
measurement cannot be trusted to report honestly when it starts drifting.

## Review checklist for a diff under `retro/**` or `tracking/**`

- Does a frozen or re-based dollar figure (`entry_price`, `stop_price`, a
  mark's `close`) ever get compared against a price on a *different*
  adjustment basis without an explicit re-basing step?
- Does a new window read (aggregate, report, CLI) call
  `retro/adoption.py::keep_adopted_rows`, or read `verdicts` /
  `verdict_outcomes` directly and risk double-counting a same-day loser?
- Does a new or changed audit column get backfilled from today's data for
  historical rows? It should not — `NULL` must mean "not measured", never a
  recomputed stand-in.
- Is a new write to `verdicts`, `verdict_outcomes`, `verdict_positions`, or
  the proposal ledger reachable from anywhere other than `collect`/`evaluate`
  (retro) or `update_tracking`/`rebuild_positions` (tracking)? A second writer
  breaks the single-source-of-truth guarantee D2 exists to protect.
- Does a rerun/correction-path test assert *replace* semantics (a whole slice
  or position replaced) rather than a patch to one field?
  `tests/retro/test_evaluate.py::TestEvaluateIdempotence` and
  `tests/tracking/test_update.py::TestSplitRebase`/`TestRebuildPositions` are
  the shipped models.
- Does a win/loss or return computation route through `backtest/metrics.py`'s
  shared functions rather than reimplementing them?
  `tests/test_shared_trade_metrics.py` exists to catch the backtest `Trade`
  ledger and the tracking `VerdictPosition` ledger silently disagreeing on
  what counts as a win.
