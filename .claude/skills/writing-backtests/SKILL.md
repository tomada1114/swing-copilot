---
name: writing-backtests
description: >
  Covers src/swing_copilot/backtest/**: BacktestEngine.run's deterministic
  point-in-time simulation loop, the cost model (slippage_pct, commission_pct
  applied on both entry and exit including forced liquidation), exits.py's
  stop-versus-max_hold precedence, and the hand-calculated exact-arithmetic
  tests in tests/backtest/*.py. Use when changing backtest/engine.py,
  backtest/exits.py, backtest/policy.py, or backtest/metrics.py, adding a
  backtest fixture, or reviewing a PnL/equity assertion.
---

# Writing Backtests

**Owns:** deterministic point-in-time simulation, the cost model, exit
precedence, and the exact-arithmetic tests that prove them. **Does not own:**
the `as_of` visibility predicate itself (`enforcing-point-in-time`),
`calc_position_size`'s share-sizing arithmetic and the ranking this engine's
`candidates_fn` consumes (`checking-risk-math`), how a `BacktestResult` is
stored (`writing-storage-code`). Note that `risk/position_sizing.py` is
simulator-only: `backtest/engine.py` is its only production caller, and no
account-scale constraint (sizing, concentration, correlation) exists in the
production risk path at all.

## No look-ahead

`BacktestEngine.run` walks `trading_days` in order and at each simulated
`day` only ever reads bars up to that day — the docstring on `run` states
this explicitly: "the engine only ever reads up to the current simulated
day; no look-ahead occurs regardless of how much data is present" in the
passed-in `bars` frame. Signals are generated from `candidates_fn(day)` at
`day`'s close and only *filled* on the next iteration's open
(`_fill_pending_entries`), never the same day: the loop calls
`_fill_pending_entries` for `pending_entries` computed on the *previous*
iteration, before it recomputes `pending_entries = candidates_fn(day)` for
today. The regime/earnings entry policy (`backtest/policy.py`) is evaluated
at `EntryPolicyRequest.as_of`, deliberately the *signal* day, never the fill
day — evaluating the gate on the fill day's own bar would be exactly the
look-ahead the simulator exists to avoid, because tomorrow's open is the
first point at which today's close is an observable fact.

The failure mode when this breaks is silent: a backtest that peeks looks
*better*, not obviously wrong, because it is trading on information a real
participant would not have had yet. `tests/backtest/test_engine.py`'s
`TestNoLookahead` is the regression shape: assert equity is unchanged on the
signal day and only moves once the fill has actually happened.

## Costs, adverse on both sides, including forced liquidation

`self._slippage_pct = settings.backtest.slippage_pct *
settings.backtest.slippage_multiplier` is computed once in `__init__` so
every call site — entry, ordinary exit, and end-of-run liquidation — uses the
same rate. "Adverse" means the direction that hurts the position on every
side:

- Entry (`_entry_execution_price`): `bar["open"] * (1 + self._slippage_pct)`
  — you pay *more* than the quoted open.
- Exit (`_settle_exit`, called from both `_process_exits` and
  `_liquidate_remaining`): `exit_price * (1 - self._slippage_pct)` — you
  receive *less* than the quoted exit price.
- Commission (`settings.backtest.commission_pct`) is charged on the notional
  at both `_commit_entry` and `_settle_exit`, tracked as `Trade.commission_usd`
  separately from the entry/exit prices (which already carry slippage) so
  `Trade.pnl` — `(exit_price - entry_price) * shares - commission_usd` — is
  the one place both cost components meet.

Forced liquidation at the end of a run (`_liquidate_remaining`, called once
after the `trading_days` loop with `exit_reason="end_of_backtest"`) calls the
*same* `_settle_exit`, so it pays the same adverse slippage and commission as
any other exit — there is no cheaper "just close the books" path. Skipping
costs on liquidation is the anti-pattern: it would let a backtest that never
converges to flat still report a clean final equity number.

## Stop-versus-max-hold precedence is explicit and tested

`exits.py::evaluate_exit` is the one place this decision is made — production
`BacktestEngine` and anything reproducing exit behavior (a tracking ledger
walking a position forward) call the *same* function rather than a second,
drifting copy. The order inside it is the contract:

1. A gap through the stop fills at the open (`open_price <= stop_price`).
2. An intraday touch fills at the stop itself (`low <= stop_price`).
3. Only if neither stop condition fired does max-hold trigger, at the close.

When both a stop and max-hold would trigger on the same bar, **stop always
wins** — `tests/backtest/test_engine.py::TestMaxHold::test_stop_takes_precedence_if_triggered_on_max_hold_day`
and `tests/backtest/test_exits.py::test_evaluate_exit_stop_and_max_hold_on_same_day_prefers_stop`
pin this. This is the ambiguous case that silently changes results if left
unspecified: swap the order and every trade that would have stopped out on
its last held day instead exits at the close price under `"max_hold"`, moving
both the PnL and the reported exit-reason distribution without an obvious
symptom.

## Expected values are hand-calculated, never recomputed

A final-equity or PnL assertion must come from arithmetic worked out
independently of the engine — literal numbers in the test, not
`entry_price * shares` restated the way `_commit_entry` computes it.
`test_engine.py`'s `TestGapStop`/`TestTradePnl`/`TestCashAndRankConstraints`
classes are the model: `stop_trades[0].exit_price ==
pytest.approx(80.0 * (1 - 0.001))` states the slippage multiplication by hand
against a literal stop price, and a one-trade fixture asserts
`result.final_equity` against a fully hand-summed
`initial_cash - entry_notional - entry_commission + exit_notional -
exit_commission`. `tests/backtest/test_metrics.py`'s
`test_hand_calculated_*` tests do the same for the Sharpe/drawdown/PnL
formulas metrics.py implements. A test that recomputes the expected value the
way the implementation computes it passes by construction and can never
disagree with a wrong implementation.

## Determinism

Same `trading_days`, `bars`, and `candidates_fn` must produce the same
`BacktestResult` — `test_engine.py`'s
`TestBenchmarkAndReproducibility::test_...` asserts `first == second` from
two runs of the same inputs. What breaks it: iterating a `dict` whose
insertion order depends on something non-deterministic (candidate ranking
must already be stable — see `checking-risk-math`'s tie-break rule),
floating-point accumulation order changing between runs (the engine
accumulates cash/equity in a fixed loop order over `trading_days`, never a
set), and any unseeded sampling. `TestDuplicateBars` guards a related
input-shape hazard: duplicate `(symbol, date)` rows in `bars` must not change
the fill price or equity, because `_latest_bar`/`_bar` must resolve them the
same way every time.

## A backtest result is evidence for a proposal, not a config change

A favorable backtest number does not by itself justify changing
`strategies.yaml` or `settings.yaml`. Route the finding through
`swing-retro`'s proposal loop, which requires structured evidence and design
review before a threshold or weight actually changes — see AGENTS.md's
"Reading the Accumulated Data" note: no config changes on point estimates
alone.

## Review checklist

- Does the change read a bar past the current simulated `day` anywhere, even
  indirectly through a helper that does not take an explicit cutoff?
- Is slippage/commission applied on every exit path, including
  `_liquidate_remaining`, not only the "normal" `_process_exits` path?
- If the change touches stop or max-hold logic, is the same-bar precedence
  test still present and still asserting stop wins?
- Is the new/changed assertion's expected value written by hand, or does it
  quietly recompute what the code under test computes?
- Does a determinism test still pass — same inputs, byte-identical
  `BacktestResult`?
