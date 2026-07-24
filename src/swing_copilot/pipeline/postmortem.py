"""Signal postmortem / forward-return verification (P2-11, roadmap §5 P2-11).

Each daily run looks back at the candidates from the run 5 and 20 trading
days ago, computes what their price actually did since then (the "forward
return"), classifies each as a hit or miss, persists it to `signal_outcomes`,
and aggregates the trailing window into per-signal hit-rate stats for the
Markdown report. This is purely retrospective: it never adjusts screening,
ranking, or risk in response to what it finds (roadmap's explicit
"not in scope" -- weight auto-tuning stays a human decision).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from swing_copilot.storage.audit_records import SignalOutcomeRecord
from swing_copilot.storage.history_queries import (
    get_run_by_date,
    get_run_detail,
    get_signal_outcomes,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from swing_copilot.config import PostmortemConfig
    from swing_copilot.storage.history_queries import SignalOutcomeRow
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

# `_find_target_trading_day`'s calendar-derivation window: wide enough to
# cross holidays/weekends for the requested horizon (mirrors the (N+5)*3
# heuristic used elsewhere in this feature's design).
_CALENDAR_WINDOW_PADDING_DAYS = 5
_CALENDAR_WINDOW_MULTIPLIER = 3


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


@dataclass(frozen=True, slots=True)
class SignalPerformanceRow:
    """One signal's weighted hit-rate stats for the markdown aggregation (REQ-005/REQ-008).

    `true_positive_count`/`false_positive_count`/`neutral_count` are RAW
    (unweighted) occurrence tallies -- the issue's "TP/FP/NEUTRAL件数" reads
    as a literal count column, distinct from `hit_rate`, which alone is
    horizon-weighted. `n` is also raw and includes NEUTRAL occurrences: the
    issue's own "n=15" preliminary-sample example counts every occurrence of
    a signal, not just its TP/FP ones.
    """

    signal_name: str
    true_positive_count: int
    false_positive_count: int
    neutral_count: int
    hit_rate: float | None
    n: int
    is_preliminary: bool


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


def _find_target_trading_day(
    market_store: MarketStore, benchmark_symbol: str, as_of: date, horizon_days: int
) -> date | None:
    """Return the trading day `horizon_days` sessions before `as_of`, or `None`.

    Derives the trading calendar from `benchmark_symbol`'s own distinct bar
    dates over a generously wide window -- this repo has no dedicated
    trading-calendar module; mirrors `backtest/runner.py`'s `_trading_days()`.

    Args:
        market_store: Bars source.
        benchmark_symbol: Reference symbol whose distinct bar dates stand in
            for the trading calendar (e.g. `settings.backtest.benchmark`).
        as_of: Today's evaluation date -- assumed to itself be the most
            recent trading day in the window.
        horizon_days: How many trading sessions back to look.

    Returns:
        `None` if there are fewer than `horizon_days + 1` distinct trading
        days in the window -- there is no way to compute this horizon yet
        (e.g. too early in the product's life, or a data gap).
    """
    window_days = (
        horizon_days + _CALENDAR_WINDOW_PADDING_DAYS
    ) * _CALENDAR_WINDOW_MULTIPLIER
    start = as_of - timedelta(days=window_days)
    bars = market_store.read_bars([benchmark_symbol], start, as_of, as_of)
    if bars.empty:
        return None
    trading_days: list[date] = sorted(bars["date"].unique().tolist())
    if len(trading_days) < horizon_days + 1:
        return None
    return trading_days[-1 - horizon_days]


def _compute_forward_return(
    market_store: MarketStore, symbol: str, run_date: date, as_of: date
) -> float | None:
    """Return `(close(as_of) - close(run_date)) / close(run_date) * 100`, or `None`.

    `read_bars`'s own `as_of` clamp already guarantees no bar dated after
    `as_of` is ever considered (REQ-006, look-ahead prevention) -- this is
    not re-checked here, it is structurally impossible via this call.
    `None` covers a missing close on either date, a genuine data-quality
    skip rather than an error (the issue's own boundary condition).
    """
    bars = market_store.read_bars([symbol], run_date, as_of, as_of)
    if bars.empty:
        return None
    run_rows = bars[bars["date"] == run_date]
    as_of_rows = bars[bars["date"] == as_of]
    if run_rows.empty or as_of_rows.empty:
        return None
    run_close = float(run_rows.iloc[0]["close"])
    as_of_close = float(as_of_rows.iloc[0]["close"])
    if run_close == 0:
        return None
    return (as_of_close - run_close) / run_close * 100


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


def _process_horizon(
    market_store: MarketStore,
    state_store: StateStore,
    horizon_days: int,
    request: _PostmortemRequest,
) -> str | None:
    """Compute and persist one horizon's outcomes; return a skip note, or `None` on success."""
    as_of = request.as_of
    target_date = _find_target_trading_day(
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

    detail = get_run_detail(state_store.database, historical_run_id)
    # Defensive only: `get_run_by_date` just found this exact `run_id` in the
    # same `runs` table, so this branch requires the row to vanish between
    # the two reads (impossible in this batch's single-threaded execution
    # model) -- excluded from coverage as genuinely unreachable in tests.
    if detail is None:  # pragma: no cover
        note = f"{horizon_days}d: run {historical_run_id} not found on detail read"
        logger.warning("postmortem step: %s", note)
        return note

    records: list[SignalOutcomeRecord] = []
    for candidate in detail.candidates:
        forward_return_pct = _compute_forward_return(
            market_store, candidate.symbol, target_date, as_of
        )
        if forward_return_pct is None:
            continue
        records.append(
            SignalOutcomeRecord(
                run_id=historical_run_id,
                symbol=candidate.symbol,
                horizon_days=horizon_days,
                as_of=as_of,
                signal_names=candidate.signal_names,
                forward_return_pct=forward_return_pct,
                classification=classify_forward_return(
                    forward_return_pct,
                    neutral_threshold_pct=request.thresholds.neutral_threshold_pct,
                    severe_threshold_pct=request.thresholds.severe_threshold_pct,
                ),
            )
        )
    if records:
        state_store.record_signal_outcomes(records)
    return None


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
    A horizon with no prior run, or a candidate with missing bars, is a
    normal skip -- never an exception -- so a brand-new database (no run
    history yet) completes this step successfully with nothing to show
    (the roadmap's NO_PRIOR_RUN fallback). Only a genuinely unexpected
    exception (e.g. a DB connectivity failure) propagates, to be caught by
    `pipeline/daily.py`'s `_run_step_postmortem` wrapper and recorded as a
    degraded (not failed/aborted) step.

    Args:
        market_store: Bars source for both the trading-calendar derivation
            and each candidate's forward-return closes.
        state_store: Write target for `signal_outcomes`, and (via its
            `database` property) the read source for the historical run
            lookup, its candidates, and the trailing-window aggregation.
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
