---
name: checking-risk-math
description: >
  Covers src/swing_copilot/risk/checks.py's account-independent symbol trade
  plan (entry, limit, stop, atr14, stop_distance_pct, WIDE_STOP/earnings
  warnings), src/swing_copilot/risk/position_sizing.py's Fraction-exact share
  sizing (simulator-only, called from backtest/engine.py), and the
  quantitative correctness of screening/pipeline.py's ranking (score_weights,
  deterministic tie-break). Use when changing risk/checks.py, risk/
  position_sizing.py, screening/pipeline.py's scoring or sort key, or
  config.py's RankingConfig/ScoreWeights validation, or reviewing a
  NaN/non-positive metric branch.
---

# Checking Risk Math

**Owns:** `src/swing_copilot/risk/checks.py`, `risk/position_sizing.py`, and
the quantitative correctness of `screening/pipeline.py`'s ranking — symbol
trade-plan math, share sizing, and deterministic ordering. **Does not own:**
the simulator's fill/exit mechanics (`writing-backtests`), the `as_of`
predicate (`enforcing-point-in-time`), config file loading itself
(`wiring-the-pipeline`).

## The public product does not size a position or know your account

`risk/checks.py`'s module docstring states the constraint directly: "The
public product does not know a reader's account equity or holdings. This
module therefore evaluates only facts intrinsic to one symbol." `RiskChecker`
computes `limit_price`/`stop_price`/`atr14`/`stop_distance_pct` from the
candidate's own close and ATR, plus the market-wide `ExposureDecision` label
and the point-in-time earnings guard — never a share count, a correlation
coefficient, or a concentration limit.

`risk/position_sizing.py::calc_position_size` is the only place account-scale
sizing math still exists, and it is **simulator-only**: its own docstring
says "Do not call this from `risk/checks.py` or any other production path; a
public run must never assume a reader's account size or holdings." Its only
production caller is `backtest/engine.py`, sizing against a notional backtest
account. It uses `fractions.Fraction` rather than `float` division so
`shares * risk_per_share <= risk_budget` holds algebraically for every input
— `Fraction(account_equity) * Fraction(max_trade_risk_pct) //
Fraction(risk_per_share)`, not `int(equity * pct / risk_per_share)`, because
float division can round a quotient past an integer boundary at extreme
inputs (very large equity, or a `max_trade_risk_pct` as small as 0.0001%).
`PositionSizeResult` carries both intermediate share counts
(`shares_by_risk`, `shares_by_position_cap`) alongside the floored minimum
`shares`, so which cap actually bound the trade is visible in the output
rather than silently clamped away.

## Correlation and concentration were deliberately removed from production

An earlier version of this codebase ran a correlation check and
account-concentration limits inside the production risk path. They were removed
in stages — Issue #348 dropped reader-account-dependent sizing, PR #352
("本番経路から口座依存ルールを全廃") cleared the rest of the account-dependent
rules, and Issue #385 confined `position_sizing.py` to the simulator (the
genealogy is recorded in `position_sizing.py`'s module docstring). The reason is
the same at every stage: the public product cannot
know a reader's actual holdings, so any correlation-across-positions or
concentration-limit math *in the production path* was necessarily fictional.
`tests/risk/test_checks.py::TestTradePlan::test_public_plan_has_no_account_or_correlation_constraints`
is the regression — it asserts `RiskAssessment` has no `correlation`,
`max_shares`, `shares_by_risk`, or `shares_by_position_cap` attribute at all,
and `docs/07_invariant_test_matrix.md` #58 names the guarded regression
explicitly: "重複日付の系列結合や相関制約が公開経路へ戻る" (a duplicate-date
series join or a correlation constraint returning to the public path).
**Reintroducing correlation, concentration, or any account-scale constraint
into `risk/checks.py` is the anti-pattern this test exists to catch** — that
kind of math belongs only in `backtest/`'s notional simulation, never in a
`RiskAssessment` a real reader sees.

## NaN and non-positive values are an explicit branch, never an accident

`screening/pipeline.py::ranking_metrics` computes `rsi14`/`atr14`/
`avg_volume`/`close`/`sma50`/`sma200` from bars and returns `None` — dropping
the symbol from the candidate set entirely — under two explicit conditions:
any metric is `NaN` (`any(math.isnan(value) for value in metrics.values())`,
covering insufficient price history) or `metrics["close"] <= 0` (a corrupt or
placeholder row). The docstring is explicit about why this is a per-symbol
drop rather than a run-wide failure: `_score_rows` divides by `close`, so a
single bad symbol must cost that one symbol, not abort the whole run.
`_execution_distance` has the same shape: `atr14 <= 0.0` (a degenerate,
effectively constant-price series) returns `None` rather than dividing by it,
which downstream classifies as the `"UNKNOWN"` execution state rather than a
crash or a silently wrong bucket.

`risk/checks.py::_assess` mirrors this for the trade plan: a non-finite
`limit_price`/`stop_price`/`atr14`, a non-positive `limit_price`, or
`stop_price >= limit_price` all produce an explicit `status="not_calculable"`
(or `"rejected"` under `CASH_PRIORITY`) with a named reason
(`_INVALID_STOP_REASON`) rather than propagating a `NaN` stop distance into
a report.

## Ranking must be deterministic — tie-break and weight validation

`_build_candidates` sorts on `_state_sort_key(state, score, symbol) =
(EXECUTION_BUCKETS.index(execution_bucket(state)), -score, symbol)`: the
execution-state bucket first, then descending score, then **symbol
ascending as the deterministic tiebreak (REQ-010)**. Two candidates with the
same bucket and an identical score always order the same way regardless of
dict/set iteration order upstream, which matters because `candidate_symbols`
is built as a `set` earlier in the same function.

The weights that produce `score` are validated at config-parse time, before
any external I/O, by `StrategiesConfig`'s `model_validator`s in `config.py`:
`_require_score_weights_sum_to_one` rejects a strategy whose `ScoreWeights`
fields do not sum to `1.0` within `_SCORE_WEIGHT_SUM_TOLERANCE`, and
`_require_signal_for_weighted_component` rejects giving a non-zero weight to
a strategy-specific component (`pivot_proximity`, `rs_percentile`,
`criteria_met`) whose required signal
(`_SCORE_COMPONENT_REQUIRED_SIGNAL`) is not in that strategy's `signals_all`
— because an unmet dependency would otherwise contribute a silent constant
`0.0` and quietly shrink the effective weight of every other component
without any error. Both are `ValueError`s raised from `strategies.yaml`
loading, not something screening discovers at run time.

## What this layer's tests owe

`tests/risk/test_position_sizing.py::TestFractionFloorInvariant` and
`TestFractionFloorRegressions` are the shape for sizing math: assert the
exact algebraic invariant (`shares * risk_per_share <= risk_budget`) holds at
extreme inputs, not just a typical one, and pin at least one input where
naive float division would have rounded past the correct integer.
`tests/screening/test_pipeline.py::TestCompositeScoring` and
`TestCandidateAggregationAndRanking` are the model for ranking: hand-worked
expected scores from the literal weights and component values, plus an
explicit same-score-different-symbol case proving the symbol-ascending
tiebreak. A NaN/non-positive test must construct bars that produce the exact
condition (too few rows for `atr14`, a zero or negative close) rather than
mocking `ranking_metrics` to return `None` — the point is proving the real
computation reaches that branch.
