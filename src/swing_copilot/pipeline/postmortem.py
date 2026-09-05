"""Signal postmortem / forward-return verification (P2-11, roadmap §5 P2-11).

Each daily run looks back at the candidates from the run 5 and 20 trading
days ago, computes what their price actually did since then (the "forward
return"), classifies each as a hit or miss, persists it to `signal_outcomes`,
and aggregates the trailing window into per-signal hit-rate stats for the
Markdown report.

Issue #188 widens the *measurement* (not the classification) to that run's
whole screened universe: the same forward return is also stored, untagged by
hit/miss, for the near-misses `candidate_limit` cut and for every symbol a
filter or signal rejected (`universe_forward_returns`). Without those two
control groups only the false-positive rate is knowable -- nothing says
whether the symbols the screen threw away would have done better.

This is purely retrospective: it never adjusts screening,
ranking, or risk in response to what it finds (roadmap's explicit
"not in scope" -- weight auto-tuning stays a human decision).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from swing_copilot.pipeline.forward_returns import (
    compute_forward_return,
    find_target_trading_day,
)
from swing_copilot.report.daily_brief import SignalPerformanceRow
from swing_copilot.storage.audit_records import (
    OUTCOME_CLASS_CANDIDATE,
    OUTCOME_CLASS_REJECTED,
    OUTCOME_CLASS_TRUNCATED,
    SignalOutcomeRecord,
    UniverseForwardReturnRecord,
)
from swing_copilot.storage.history_queries import (
    get_rejections,
    get_run_by_date,
    get_run_detail,
    get_signal_outcomes,
    get_truncations,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from swing_copilot.config import PostmortemConfig
    from swing_copilot.storage.history_queries import RunDetail, SignalOutcomeRow
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore

logger = logging.getLogger(__name__)

TRUE_POSITIVE = "TRUE_POSITIVE"
FALSE_POSITIVE_MILD = "FALSE_POSITIVE_MILD"
FALSE_POSITIVE_SEVERE = "FALSE_POSITIVE_SEVERE"
NEUTRAL = "NEUTRAL"

# Which past runs get re-evaluated each day: a fixed structural choice
# (roadmap §5 P2-11), not a "要検証" threshold -- unlike PostmortemConfig's
# weights/percentages, changing this would change what the feature *is*, not
# how conservatively it's tuned.
HORIZON_DAYS: tuple[int, ...] = (5, 20)


def classify_forward_return(
    forward_return_pct: float,
    *,
    neutral_threshold_pct: float = 0.5,
    severe_threshold_pct: float = 2.0,
) -> str:
    """Classify one forward return into a postmortem outcome bucket (REQ-002..004).

    Boundary resolution (an implementer's choice the issue itself flags as
    needing to be pinned, not a new threshold): the issue's own wording
    ("|return| < 0.5% is NEUTRAL", ">0.5% is TRUE_POSITIVE") leaves exactly
    +neutral_threshold_pct and -neutral_threshold_pct undefined under a
    literal strict reading. NEUTRAL uses `<=` on both sides, resolving both
    ties toward the noise-exclusion bucket -- the most conservative choice,
    and one that closes the gap without inventing a new threshold value.
    SEVERE is strictly worse than -severe_threshold_pct; exactly
    -severe_threshold_pct stays MILD (a closed-interval reading of the
    issue's "-0.5〜-2% MILD").

    Args:
        forward_return_pct: Plain percentage number, e.g. `1.2` means +1.2%.
        neutral_threshold_pct: `|forward_return_pct| <=` this is NEUTRAL.
        severe_threshold_pct: `forward_return_pct <` the negative of this is
            FALSE_POSITIVE_SEVERE.

    Returns:
        One of `TRUE_POSITIVE`, `FALSE_POSITIVE_MILD`,
        `FALSE_POSITIVE_SEVERE`, or `NEUTRAL`.
    """
    if abs(forward_return_pct) <= neutral_threshold_pct:
        return NEUTRAL
    if forward_return_pct > neutral_threshold_pct:
        return TRUE_POSITIVE
    if forward_return_pct < -severe_threshold_pct:
        return FALSE_POSITIVE_SEVERE
    return FALSE_POSITIVE_MILD


@dataclass(slots=True)
class _SignalAccumulator:
    """Mutable per-signal running totals, private to `compute_signal_performance`."""

    n: int = 0
    raw_tp: int = 0
    raw_fp: int = 0
    raw_neutral: int = 0
    weighted_tp: float = 0.0
    weighted_fp: float = 0.0


def compute_signal_performance(
    outcomes: Sequence[SignalOutcomeRow],
    thresholds: PostmortemConfig,
) -> tuple[SignalPerformanceRow, ...]:
    """Aggregate `signal_outcomes` rows into weighted per-signal hit-rate stats.

    Each outcome row fans out to every signal name in its `signal_names`
    tuple: a candidate that fired multiple simultaneous signals attributes
    the same realized outcome to each of them (intentional, not a bug -- the
    outcome co-occurred with every signal that fired that day). `hit_rate`'s
    numerator/denominator are weighted by horizon
    (`horizon_5d_weight`/`horizon_20d_weight`); NEUTRAL rows are excluded
    from both (noise, per the issue's own "ノイズ除外" wording), so a signal
    with zero weighted TP+FP gets `hit_rate=None` rather than a
    division-by-zero.

    Args:
        outcomes: `signal_outcomes` rows already scoped to the desired
            trailing window (e.g. `lookback_window_days` via
            `get_signal_outcomes`).
        thresholds: Horizon weights and the preliminary-sample threshold.

    Returns:
        One row per distinct signal name observed in `outcomes`, sorted
        alphabetically for a deterministic render. Empty if `outcomes` is
        empty -- a valid all-zero-signals result, not an error.
    """
    accumulators: dict[str, _SignalAccumulator] = {}
    for outcome in outcomes:
        weight = (
            thresholds.horizon_5d_weight
            if outcome.horizon_days == 5  # noqa: PLR2004 - the other member of HORIZON_DAYS is 20
            else thresholds.horizon_20d_weight
        )
        for signal_name in outcome.signal_names:
            acc = accumulators.setdefault(signal_name, _SignalAccumulator())
            acc.n += 1
            if outcome.classification == TRUE_POSITIVE:
                acc.raw_tp += 1
                acc.weighted_tp += weight
            elif outcome.classification == NEUTRAL:
                acc.raw_neutral += 1
            else:
                acc.raw_fp += 1
                acc.weighted_fp += weight

    rows = []
    for signal_name in sorted(accumulators):
        acc = accumulators[signal_name]
        denominator = acc.weighted_tp + acc.weighted_fp
        hit_rate = acc.weighted_tp / denominator if denominator > 0 else None
        rows.append(
            SignalPerformanceRow(
                signal_name=signal_name,
                true_positive_count=acc.raw_tp,
                false_positive_count=acc.raw_fp,
                neutral_count=acc.raw_neutral,
                hit_rate=hit_rate,
                n=acc.n,
                is_preliminary=acc.n < thresholds.preliminary_sample_threshold,
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class _PostmortemRequest:
    """Per-call inputs `_process_horizon` needs beyond the stores themselves.

    Grouped into one value (rather than three positional params) to keep
    `_process_horizon` within the project's parameter-count guideline,
    mirroring `audit_records.ScreeningRunMeta`'s own rationale.
    """

    as_of: date
    thresholds: PostmortemConfig
    benchmark_symbol: str


@dataclass(frozen=True, slots=True)
class _ScreeningDecision:
    """What one past run decided about one symbol, for the control-group pass."""

    symbol: str
    outcome_class: str
    reason_code: str | None


def _screening_decisions(
    state_store: StateStore, detail: RunDetail
) -> tuple[_ScreeningDecision, ...]:
    """Union one past run's candidates, near-misses, and rejections (Issue #188).

    One row per symbol, and a symbol claimed by an earlier group keeps that
    group: a run screening two strategies can rank a symbol into one's
    candidates while another truncates or rejects it, and "it was a
    candidate that day" is the strongest true statement about how the
    screen treated it. `candidate > truncated > rejected` is therefore the
    precedence, matching the natural-key `(run_id, symbol, horizon_days)`
    the rows are stored under.

    Args:
        state_store: Read source for the truncation and rejection ledgers.
        detail: The historical run's own candidate list.

    Returns:
        Deduplicated decisions, candidates first (dict insertion order).
    """
    decisions: dict[str, _ScreeningDecision] = {}
    for candidate in detail.candidates:
        decisions.setdefault(
            candidate.symbol,
            _ScreeningDecision(candidate.symbol, OUTCOME_CLASS_CANDIDATE, None),
        )
    for truncation in get_truncations(state_store.database, detail.run_id):
        decisions.setdefault(
            truncation.symbol,
            _ScreeningDecision(truncation.symbol, OUTCOME_CLASS_TRUNCATED, None),
        )
    for rejection in get_rejections(state_store.database, detail.run_id):
        decisions.setdefault(
            rejection.symbol,
            _ScreeningDecision(
                rejection.symbol, OUTCOME_CLASS_REJECTED, rejection.reason_code
            ),
        )
    return tuple(decisions.values())


def _process_horizon(
    market_store: MarketStore,
    state_store: StateStore,
    horizon_days: int,
    request: _PostmortemRequest,
) -> str | None:
    """Compute and persist one horizon's outcomes; return a skip note, or `None` on success."""
    as_of = request.as_of
    target_date = find_target_trading_day(
        market_store, request.benchmark_symbol, as_of, horizon_days
    )
    if target_date is None:
        note = (
            f"{horizon_days}d: insufficient trading-day history to locate the "
            "target date"
        )
        logger.info("postmortem step: %s", note)
        return note

    historical_run_id = get_run_by_date(state_store.database, target_date)
    if historical_run_id is None:
        note = f"{horizon_days}d: no prior run at {target_date} (NO_PRIOR_RUN)"
        logger.info("postmortem step: %s", note)
        return note

    # `get_run_by_date` just selected this exact row from the same local runs
    # table. The daily batch has no concurrent deletion path, so model the
    # relational invariant directly instead of retaining a dead fail-open
    # branch solely for defensive coverage suppression.
    detail = cast("RunDetail", get_run_detail(state_store.database, historical_run_id))

    decisions = _screening_decisions(state_store, detail)
    returns = _forward_returns(market_store, decisions, target_date, as_of)

    records = [
        SignalOutcomeRecord(
            run_id=historical_run_id,
            symbol=candidate.symbol,
            horizon_days=horizon_days,
            as_of=as_of,
            signal_names=candidate.signal_names,
            forward_return_pct=returns[candidate.symbol],
            classification=classify_forward_return(
                returns[candidate.symbol],
                neutral_threshold_pct=request.thresholds.neutral_threshold_pct,
                severe_threshold_pct=request.thresholds.severe_threshold_pct,
            ),
        )
        for candidate in detail.candidates
        if candidate.symbol in returns
    ]
    state_store.replace_signal_outcomes(
        historical_run_id,
        horizon_days,
        records,
    )
    state_store.replace_universe_forward_returns(
        historical_run_id,
        horizon_days,
        [
            UniverseForwardReturnRecord(
                run_id=historical_run_id,
                symbol=decision.symbol,
                horizon_days=horizon_days,
                as_of=as_of,
                outcome_class=decision.outcome_class,
                reason_code=decision.reason_code,
                forward_return_pct=returns[decision.symbol],
            )
            for decision in decisions
            if decision.symbol in returns
        ],
    )
    return None


def _forward_returns(
    market_store: MarketStore,
    decisions: Sequence[_ScreeningDecision],
    target_date: date,
    as_of: date,
) -> dict[str, float]:
    """Return each decided symbol's realized move, skipping the ones with no bars.

    Bars are already local Parquet, so widening the pass from the candidates
    to the whole screened union (Issue #188) adds no network I/O. A symbol
    missing a close on either end is simply absent from the result -- the
    same fail-soft data-quality skip the candidate-only pass always made,
    never an exception.
    """
    returns: dict[str, float] = {}
    for decision in decisions:
        forward_return_pct = compute_forward_return(
            market_store, decision.symbol, target_date, as_of
        )
        if forward_return_pct is not None:
            returns[decision.symbol] = forward_return_pct
    return returns


def run_postmortem_step(
    market_store: MarketStore,
    state_store: StateStore,
    as_of: date,
    thresholds: PostmortemConfig,
    benchmark_symbol: str,
) -> tuple[str | None, tuple[SignalPerformanceRow, ...]]:
    """Compute/persist forward-return outcomes for past candidates, then aggregate (P2-11).

    For each of `HORIZON_DAYS` (5 and 20 trading days back), locates the run
    at that target trading day, classifies each of its candidates' forward
    returns as of `as_of`, and upserts the results (REQ-001/REQ-006/REQ-007).
    The same pass records the untagged forward return of that run's
    truncated near-misses and rejected symbols too (Issue #188). Both writes
    replace their `(run_id, horizon_days)` slice wholesale, so re-running a
    day is idempotent rather than additive.
    A horizon with no prior run, or a candidate with missing bars, is a
    normal skip -- never an exception -- so a brand-new database (no run
    history yet) completes this step successfully with nothing to show
    (the roadmap's NO_PRIOR_RUN fallback). Only a genuinely unexpected
    exception (e.g. a DB connectivity failure) propagates, to be caught by
    `pipeline/daily.py`'s `run_step_postmortem` wrapper and recorded as a
    degraded (not failed/aborted) step.

    Args:
        market_store: Bars source for both the trading-calendar derivation
            and each candidate's forward-return closes.
        state_store: Write target for `signal_outcomes` and
            `universe_forward_returns`, and (via its `database` property) the
            read source for the historical run lookup, its candidates,
            near-misses, and rejections, and the trailing-window aggregation.
        as_of: Today's evaluation date -- the forward-return endpoint.
        thresholds: Classification/weight/sample-size settings
            (`settings.postmortem`).
        benchmark_symbol: Trading-calendar reference symbol
            (typically `settings.backtest.benchmark`, `"SPY"` by default).

    Returns:
        `(note, performance_rows)`. `note` summarizes any horizon(s)
        skipped (joined with `"; "`), or `None` if every horizon found a
        prior run. `performance_rows` is always the full
        trailing-`lookback_window_days` aggregation, even when this call's
        own writes contributed nothing new.
    """
    request = _PostmortemRequest(
        as_of=as_of, thresholds=thresholds, benchmark_symbol=benchmark_symbol
    )
    notes: list[str] = []
    for horizon_days in HORIZON_DAYS:
        note = _process_horizon(market_store, state_store, horizon_days, request)
        if note is not None:
            notes.append(note)

    window_start = as_of - timedelta(days=thresholds.lookback_window_days)
    outcomes = get_signal_outcomes(state_store.database, window_start, as_of)
    performance = compute_signal_performance(outcomes, thresholds)
    return ("; ".join(notes) if notes else None, performance)
