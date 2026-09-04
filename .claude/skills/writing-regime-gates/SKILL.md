---
name: writing-regime-gates
description: >
  Covers src/swing_copilot/regime/**: the market-gate state machine
  (`GateVerdict`, `DistributionLevel`, `FtdState`, `ExposureVerdict`), the
  Distribution Day rolling-window count with its expiry/recovery rules
  (`window_days`, `dd_decline_pct`, `recovery_pct`), the Follow-Through Day
  lifecycle in `ftd.py`, `determine_exposure`'s unknown-cannot-loosen mapping,
  and the `copilot-dd-forward` explorer (`dd_forward.py`,
  `dd_forward_sweep.py`). Use when changing `regime/gate.py`,
  `regime/distribution.py`, `regime/ftd.py`, `regime/exposure.py`, or the
  `dd_forward*` modules, adding a Distribution Day or FTD threshold, or
  reviewing a diff that touches `RegimeSnapshot`/`ExposureDecision`.
---

# Writing Regime Gates

**Owns:** `src/swing_copilot/regime/**` — the market gate's state machine and
the determinism its inputs and transitions owe. **Does not own:** the `as_of`
predicate itself (`enforcing-point-in-time`), persisting or reading the
gate's state (`writing-storage-code`), how the gate result is rendered to an
operator (`rendering-reports`), simulating it over history
(`writing-backtests`), parsing its configuration (`wiring-the-pipeline`).

## What the gate decides

`calculate_regime_snapshot` (`gate.py`) turns SPY/QQQ/VIX bars into one
`RegimeSnapshot` for a run's `as_of`: a `MarketGate` trend verdict, each
index's `DistributionResult`, the strictest composite `DistributionLevel`,
and an FTD `FtdSnapshot`. `determine_exposure` (`exposure.py`) maps that
snapshot to one `ExposureVerdict` — `NEW_ENTRY_ALLOWED` / `REDUCE_ONLY` /
`CASH_PRIORITY`. `pipeline/daily.py::_calculate_regime_snapshot` is the one production
caller that reads `MarketStore` and builds the thresholds from
`settings.regime`; `_record_regime_snapshot`/`_record_exposure_decision`
compute each exactly once per run and persist it, same-run immutable — data
recovering mid-run must not loosen an already-recorded decision. Downstream,
`REDUCE_ONLY` is consumed only as a warning label, never a sizing multiplier
(`checking-risk-math` owns that boundary), and `CASH_PRIORITY` fails every
candidate closed with the `regime` reason in `backtest/policy.py`
(`writing-backtests`) and in the live risk path.

## The state machine, exactly as implemented

**`GateVerdict`** (`BULL`/`BEAR`/`NEUTRAL`/`UNKNOWN`) is not itself
stateful — `evaluate_market_gate` recomputes it fresh from the day's
`spy_close`, `spy_sma200`, `vix_close` every call. `BEAR` is
`spy_close < spy_sma200 * bear_spy_sma_ratio` (0.97 default), `NEUTRAL` is
the buffer between that and `spy_sma200`, `BULL` is at or above `spy_sma200`.
`is_panic` (`vix_close > bear_vix_min`, 30.0 default) is computed
independently of the trend branch and preserved even when trend inputs are
missing — `UNKNOWN` must never be able to hide a live VIX panic.

**`DistributionLevel`** (`NORMAL`/`CAUTION`/`HIGH`/`SEVERE`/`UNKNOWN`) is a
count classification, not a day-to-day machine, but the count it classifies
carries two real temporal rules inside `calculate_distribution_days`:

- **Rolling-window expiry.** A comparison day needs its prior close, so a
  full `window_days` (25) window needs 26 visible rows — `len(visible) <
  thresholds.window_days + 1` returns `INSUFFICIENT`. A counted day is
  dropped once `last_index - index >= thresholds.window_days - 1`: it stays
  live for `window_days - 1` (24) subsequent observations and expires on the
  25th trading day after itself, exactly as the module docstring states.
  Because `last_index` is `as_of`'s own last visible row, this is
  recomputed fresh at every `as_of` rather than aged forward from a stored
  count — there is no persisted running total to drift.
- **Recovery invalidation**, a separate and independently-triggered
  cancellation: `highest_after[i]` (the highest close strictly after row
  `i`) is compared against `closes[i] * (1.0 + recovery_pct)` (5% default);
  a day whose price later recovers past that bar is dropped from
  `valid_weights` regardless of its age. `test_invalidates_count_at_exact_five_percent_recovery`
  pins the boundary at exactly `>=`, and
  `test_a_gap_after_a_distribution_day_neither_recovers_nor_invalidates_it`
  pins that a `NaN` close cannot count as a recovery.
- A day counts `1.0` when `close/prev_close - 1 <= dd_decline_pct` (a real
  distribution day, decline on rising volume) and `0.5` when the change is
  smaller than `stall_abs_change_pct` in magnitude (a stall day) — both
  require `volumes[index] > volumes[index - 1]`.

**`FtdState`** (`UNKNOWN`/`AWAITING_CORRECTION`/`CORRECTION_CONFIRMED`/
`DAY1`/`DAY2_3`/`FTD_CONFIRMED`/`EXPIRED`) is a genuine day-by-day machine —
`transition()` in `ftd.py` is the single pure one-day step, replayed forward
over every visible row. `AWAITING_CORRECTION` and `EXPIRED` share the exact
same forward edge: either accepts a fresh `correction_observed` (close `<=`
rolling high `* (1 - correction_decline_pct)`, default 3%, with
`correction_down_days` consecutive down days, default 3) into
`CORRECTION_CONFIRMED` — `EXPIRED` is not a dead end, a new correction
restarts the cycle. From `DAY1`/`DAY2_3`, a close below the Day-1 low resets
back to `CORRECTION_CONFIRMED`, not all the way to `AWAITING_CORRECTION`
(`test_day1_low_break_resets_to_correction_confirmed`). Confirmation
requires `_FTD_FIRST_DAY <= day_number <= _FTD_LAST_DAY` (4 through 10
inclusive), a gain `>= ftd_gain_pct` (1.25% default, with a
`_FLOAT_TOLERANCE` guard against exact-boundary float noise), and rising
volume; reaching `day_number >= _FTD_LAST_DAY` without confirming expires
unconditionally (`test_day10_without_ftd_expires`). Once
`FTD_CONFIRMED`, the only forward edges are staying confirmed, expiring on a
close below `ftd_day_low` (the confirmation day's own low), or — only when
the caller supplies `spy_sma_period` — expiring the moment SPY's close
recovers to its SMA, checked as a state transition after `transition()`
runs so a later dip cannot resurrect an already-expired FTD.

## Why a counting rule with expiry is unusually easy to get wrong

A plausible-looking count can be wrong in ways that produce no crash and no
obviously-off number:

- **An off-by-one in the window.** Requiring `window_days` rows instead of
  `window_days + 1` silently drops the earliest comparison day's prior
  close. `test_requires_26_prices_for_25_day_window` parametrizes 24/25/26
  rows specifically to pin the boundary at 26.
- **A day counted twice, or dropped early.** `distribution.py` iterates each
  index once and applies both the window and recovery filters in the same
  pass; a refactor that re-slices the tail per day (which the
  `highest_after` precomputation replaced for performance) risks re-testing
  a comparison day against the wrong reference point, or skipping a day
  whose `NaN` close should have been transparent to the recovery scan
  (`test_a_gap_after_a_distribution_day_neither_recovers_nor_invalidates_it`).
- **Expiry evaluated at the wrong reference date.** The expiry test is
  `last_index - index`, relative to the *current* `as_of`'s last visible
  row, never the wall clock or a cached "today." A stale `last_index` from
  a previous call would let an old day's expiry drift.
- **A count rebuilt from a different starting point.** `dd_forward.py`'s
  `_TRAILING_ROWS = 80` optimization feeds `calculate_distribution_days`
  only a bounded trailing slice, on the claim that nothing older than the
  window can contribute — exactly the kind of change that can silently
  shift where counting starts. It is covered by an explicit equivalence
  test, `tests/regime/test_dd_forward.py::test_trailing_slice_is_exact_for_the_counter`,
  asserting the bounded and full-history counts agree at every index. A new
  shortcut anywhere in `regime/**` needs the same shape of proof, not just a
  comment claiming it.

## Determinism and point-in-time

The same price history through the same `as_of` must produce the same
`RegimeSnapshot`/`ExposureDecision`. What would break it: reading bars
without trimming to `date <= as_of` first; computing the snapshot more than
once per run and letting a second call see rows the first could not
(`pipeline/daily.py` deliberately computes and persists it exactly once,
immutable for the rest of the run); or an unbounded shortcut like
`_TRAILING_ROWS` that has not been proven equivalent to the full-history
calculation.

Every input the gate reads is price history, so it owes the same `date <=
as_of` discipline **REQUIRED:** `enforcing-point-in-time` owns in full.
`gate.py`, `distribution.py`, and `ftd.py` each trim their own frame
(`bars.loc[bars["date"] <= as_of].sort_values("date")`) at the functional-core
boundary rather than trusting the caller. A gate computed with tomorrow's
bar is the single most damaging look-ahead this system can make — it does
not just score one candidate wrongly, it can flip `CASH_PRIORITY` to
`NEW_ENTRY_ALLOWED` (or the reverse) and change whether *anything* trades
that day. `dd_forward.py` is the one deliberate, documented exception: its
forward-outcome measurement reads rows after the classification date on
purpose (an evaluation-only look-ahead), but the classification itself still
obeys the inclusive boundary, and the whole scan stays bounded by an outer
`as_of` — see its module docstring.

## Exposure tiering

`determine_exposure` folds `GateVerdict`, `DistributionLevel`, FTD activity,
and the VIX panic flag into one `ExposureVerdict` through `_base_exposure`,
strictest branch first: panic always wins to `CASH_PRIORITY`; `BEAR`
without an active FTD re-entry is `CASH_PRIORITY`, with one `REDUCE_ONLY`
exception when FTD is confirmed; `NEUTRAL` or DD `SEVERE` is `REDUCE_ONLY`;
otherwise `NEW_ENTRY_ALLOWED`. `distribution_severity` ranks
`DistributionLevel.UNKNOWN` above `SEVERE` specifically so an unknown level
can never win a `max()` comparison and quietly loosen the composite. When
one of `GateVerdict`/`DistributionLevel` is unknown, `_stricter` moves the
known-input baseline exactly one tier stricter rather than guessing; when
both are unknown the ceiling is fixed at `CASH_PRIORITY`. That downgrade is
never silent: `ExposureDecision.is_conservatively_downgraded` records
whenever unknown data (gate, DD, or an insufficient-quality FTD read while
`BEAR`) forced a stricter verdict than the known inputs alone would have,
so a consumer can see *that* a tier was clamped and *why*, not just the
final label.

## `dd_forward.py` / `dd_forward_sweep.py`: forward-testing the DD rule

`dd_forward.py::scan_forward` replays stored history one `as_of` at a time,
classifies each date the same way `calculate_regime_snapshot` does (the
strictest of SPY's and QQQ's own `DistributionLevel`), and pairs it with the
return and drawdown that actually followed. `dd_forward_sweep.py` scores
thousands of alternative `severe_*`/`high_*` boundary combinations against
one already-replayed scan, cheap because only the classification boundary
moves, never the counts. Both modules are explicit this is an **archived
DD-only counterfactual**, separate from the live six-branch
`determine_exposure` (which also weighs SMA200, VIX, and FTD): `SEVERE ->
CASH_PRIORITY` here is not what production does with `SEVERE` today (the
live gate maps it to `REDUCE_ONLY`). `sweep_grid`'s own docstring states the
standing principle directly — "Scores are in-sample over one stored
history. They rank candidates; they do not validate them." A favorable
sweep result is evidence for a proposal, never a `settings.yaml` edit by
itself — route it through **REQUIRED:** `swing-retro`'s proposal loop, the
same rule `writing-backtests` states for a favorable backtest number.

## Review checklist for a diff under `regime/**`

- Does every bar read stay behind `date <= as_of`, trimmed at this module's
  own boundary rather than trusted from the caller?
- If a counting or window rule changed, is there a boundary test at
  `window_days`/`window_days + 1` rows (or the FTD equivalent,
  `_FTD_FIRST_DAY`/`_FTD_LAST_DAY`), not just a "some history" case?
- Does any level/state comparison let an `UNKNOWN`/`INSUFFICIENT` input win
  a `max()`/severity comparison, instead of going through
  `distribution_severity`'s UNKNOWN-outranks-SEVERE ordering or `_stricter`?
- Does a new performance shortcut (a trailing-row bound, a cached
  intermediate) carry an equivalence test against the unshortened
  calculation, the way `_TRAILING_ROWS` does?
- Is `RegimeSnapshot`/`ExposureDecision` still computed exactly once per run
  and left immutable for the rest of it, not recomputed on a later step
  that could see more data?
- Does a threshold change land only through `RegimeConfig`'s order
  validator (`dd_severe_d25 > dd_high_d25 > dd_caution_d25`,
  `dd_severe_d15 > dd_high_d15`), citing sweep evidence via `swing-retro`
  rather than a bare number edit — and if `dd_forward_sweep.py` changed,
  does `ExposureBoundaries.is_loadable` still mirror that validator?
