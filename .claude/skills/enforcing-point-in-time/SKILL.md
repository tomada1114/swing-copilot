---
name: enforcing-point-in-time
description: >
  Covers the point-in-time visibility discipline: threading an explicit `as_of`
  through every screening, risk, report and backtest calculation, the three
  visibility predicates (`date <= as_of`, `filed_at <= as_of`, `snapshot_date
  <= as_of`), the inclusive-boundary test shape, and why domain code never
  calls `date.today()`/`datetime.now()`. Use when adding a function that reads
  price/fundamentals/universe data, wiring a new `as_of` parameter, reviewing
  for look-ahead bias, or deciding how `run_date` gets resolved for a live run.
---

# Enforcing Point-in-Time

**Owns:** the `as_of` discipline itself — who must take it, what it means for
each entity, and how the boundary is tested. **Does not own:** how the storage
layer physically filters and adjusts rows on read (`writing-storage-code`),
how an adapter fetches data (`writing-external-adapters`), backtest-specific
look-ahead mechanics like sizing basis and regime-gate arms
(`writing-backtests`), or how a live run resolves `run_date` from closed
sessions (`diagnosing-daily-runs` owns that mechanism in full).

## `as_of` is required, not defaulted

Every screening, risk, report, and backtest calculation takes an explicit
`as_of: date` — see `screening/base.py`, `risk/checks.py`, `backtest/exits.py`,
`backtest/earnings_history.py`. There is no `as_of: date | None = None` that
falls back to "today" anywhere in this call graph. A default is strictly
worse than a required argument here: a default silently answers "what do we
know right now" for a function that is supposed to answer "what did we know
on that day", and the two answers are identical in exactly one case — a live
run on the day itself — which is precisely the case that hides the bug until
someone replays history. Requiring the argument makes every call site say,
in the diff, what date it is reasoning about.

## Three predicates, three entities

| Entity | Predicate | Field name | Not this |
| --- | --- | --- | --- |
| Price bars | `date <= as_of` | `date` (the trading session) | fetch time, `fetched_at` |
| Filings / fundamentals | `filed_at <= as_of` | `filed_at` (publication time) | `fiscal_period_end` |
| Universe membership | `snapshot_date <= as_of` | `snapshot_date` | today's live constituent list |

Filings are the one people get wrong: a 10-Q's `fiscal_period_end` can be
months before `as_of`, but the filing itself might not have been *published*
yet. `filed_at` is when the market actually learned the numbers; using
`fiscal_period_end` as the gate lets a screening run react to a quarter's
results before anyone could have read them. `data/edgar.py` and
`backtest/earnings_history.py` both gate on `filed_at`, never on the period
the filing describes.

## The boundary is inclusive — test the triple

`<=` in every predicate above means the row dated exactly `as_of` is visible.
Getting this backwards either hides same-day information (a bar or a filing
that arrived before the cutoff, wrongly excluded) or leaks it (a `<` written
as `<=` in the wrong direction). The required test shape is three cases, not
one:

```python
class TestAsOfDiscipline:
    def test_bar_immediately_before_the_cutoff_leaves_entries_allowed(self):
        ...  # as_of - 1 day: visible, ordinary case

    def test_bar_exactly_at_the_cutoff_is_included_and_blocks(self):
        ...  # as_of itself: visible — this is the line a `<` bug breaks

    def test_bar_after_the_cutoff_cannot_reach_back_and_block_an_earlier_day(self):
        ...  # as_of + 1 day: invisible — this is the line a look-ahead bug breaks
```

`tests/backtest/test_policy.py::TestAsOfDiscipline` and
`tests/backtest/test_earnings_history.py::TestVisibilityCutoff` are the shipped
models for this triple; `tests/storage/test_market_store.py::TestReadFilingDates`
runs the same shape against `read_filing_dates`' own `filed_at <= as_of` clause. A PR that
adds a new as_of-gated read and tests only the "before" case has not proven
the boundary — it has proven the easy 90% of it.

## `Clock` is the only wall-clock boundary

`clock.py`'s `Clock` protocol (`now()`, `today()`) is the sole sanctioned
source of wall time; `SystemClock` is its only production implementation.
Domain logic and adapters never call `date.today()` or `datetime.now()`
directly — grep for either across `src/swing_copilot/` outside `clock.py`
and it should come back empty. Wall time is *metadata* (when did we fetch
this, when did this run start), never a substitute for `as_of`. A function
that reaches for `datetime.now()` to decide what data is "current" has
smuggled a second, un-reviewable notion of "now" past every `as_of` the
caller passed in — and that second notion is exactly wrong on every replay,
backtest, or historical `--as-of` run, none of which happen at wall-clock
now.

## The tell of a look-ahead bug

Two practical signals, useful in review and in debugging a suspiciously good
backtest:

- **A result that improves when you widen the window.** If lengthening a
  lookback or moving `end` later makes a signal fire more reliably or a
  score go up, some downstream read is not actually cut off at `as_of` — it
  is seeing further than the caller intended.
- **A value only obtainable after the fact.** A forecast that uses a
  quarter's actual EPS before the filing that reported it was published, a
  split-adjusted price used to size an entry dated before the split's
  ex-date, a regime label computed from a bar the market hadn't printed yet.
  If reproducing the calculation by hand requires information a trader on
  that date could not have had, something upstream leaked `as_of`.

## The review question

For any new function that touches market, fundamentals, or universe data:
**what is the latest-dated input this function can see, and is that
provably `<= as_of`?** "Provably" means traced to a predicate in the table
above, not "the caller probably filters it first." A function whose answer
requires trusting an upstream caller's discipline is the function to push
the `as_of` parameter into directly.

**BACKGROUND:** `writing-storage-code` for how `read_bars`/`read_fundamentals`
physically enforce these predicates and adjust on read; `writing-backtests`
for the backtest-specific look-ahead surface (equity-basis sizing, regime-arm
gating); `diagnosing-daily-runs` for how a live run resolves `run_date` from
the latest *closed* session rather than the wall clock.
