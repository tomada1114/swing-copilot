"""`copilot-retro evaluate`: classify matured verdicts (P8-30).

Unlike `pipeline/postmortem.py`, which runs daily and asks "which run is
exactly N sessions old *today*", the retrospective runs in batches every few
days. So it inverts the question: for each collected run it computes when each
horizon came due and evaluates only the horizons that matured on or before
`as_of`. The maturity session -- not the observation date -- is what lands in
`verdict_outcomes.as_of`, which is why re-running the batch on any later day
reproduces exactly the same rows, with no missed or double-counted evaluation
(design §5.2, decision D7).

Classification is asymmetric because a verdict is not a direction forecast but
an extra risk-avoidance filter over already-screened candidates (design §3.1):

* `proceed` asserts only "no severe adverse move", a one-sided claim. A small
  move in either direction does not refute it, so there is no NEUTRAL bucket.
* `skip` asserts "this decline is worth avoiding". A decline is the hit; an
  advance is an opportunity cost, which is a real miss but a milder failure
  than a `proceed` that lost money.

Thresholds are `settings.postmortem`'s existing ones -- no second threshold
vocabulary is invented for verdicts (decision D6). This module is
observation-only: it never adjusts screening, sizing, ranking, or config.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from swing_copilot.pipeline.forward_returns import (
    compute_forward_return,
    compute_forward_return_detail,
    find_maturity_trading_day,
)
from swing_copilot.retro.adoption import keep_adopted_rows
from swing_copilot.storage.verdict_records import VerdictOutcomeRecord

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from uuid import UUID

    from swing_copilot.config import PostmortemConfig
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore
    from swing_copilot.storage.verdict_records import VerdictRow

logger = logging.getLogger(__name__)

HIT = "HIT"
MISS_MILD = "MISS_MILD"
MISS_SEVERE = "MISS_SEVERE"
NEUTRAL = "NEUTRAL"

PROCEED = "proceed"

#: Evaluated horizons, shared with `pipeline/postmortem.py` so verdict and
#: signal performance are measured through the same window (decision D6).
HORIZON_DAYS: tuple[int, ...] = (5, 20)

#: Extra calendar days added to `lookback_window_days` when selecting runs to
#: evaluate: a run at the far edge of the reporting window still needs its
#: 20-session horizon to fall inside the scan (design §5.2).
_EVALUATION_WINDOW_PADDING_DAYS = 30


@dataclass(frozen=True, slots=True)
class EvaluateSummary:
    """What one evaluation batch classified, deferred, and had to skip.

    `recorded_slice_count` is only ever non-zero under `only_pending`
    (Issue #209): the slices left alone because `verdict_outcomes` already
    holds exactly the symbols and recommendations they would produce.

    `preserved_outcome_count` is the subset of `outcome_count` carried
    forward unchanged from a previous evaluation rather than recomputed this
    round (Issue #424): a maturity-day close that has since gone missing
    from the store (a re-fetch that dropped a historical bar, for example)
    cannot be reproduced, but the previously recorded, already-audited
    classification is not wrong merely because it cannot be recomputed right
    now. Without this, the full-slice replace `replace_verdict_outcomes`
    performs would silently delete that still-valid row instead of leaving it
    corrected or untouched.
    """

    evaluated_slice_count: int
    pending_slice_count: int
    outcome_count: int
    notes: tuple[str, ...]
    recorded_slice_count: int = 0
    preserved_outcome_count: int = 0


def classify_verdict_outcome(
    recommendation: str,
    forward_return_pct: float,
    *,
    neutral_threshold_pct: float = 0.5,
    severe_threshold_pct: float = 2.0,
) -> str:
    """Classify one matured verdict against design §3.3's asymmetric table.

    Boundary resolution, stated once so both sides stay total and disjoint:
    for `proceed`, exactly `-neutral_threshold_pct` is already an adverse move
    (MISS_MILD) and exactly `-severe_threshold_pct` is severe; for `skip`,
    exactly `-neutral_threshold_pct` counts as the decline it avoided (HIT)
    and exactly `+severe_threshold_pct` is a severe opportunity cost.

    Args:
        recommendation: `"proceed"` or `"skip"`. Anything else is treated as
            `"skip"`, which the `verdicts` CHECK constraint makes unreachable
            in practice.
        forward_return_pct: Plain percentage number, e.g. `1.2` means +1.2%.
        neutral_threshold_pct: The noise band, `settings.postmortem`'s.
        severe_threshold_pct: The severity boundary, `settings.postmortem`'s.

    Returns:
        One of `HIT`, `MISS_MILD`, `MISS_SEVERE`, or `NEUTRAL`. `proceed`
        never returns `NEUTRAL` -- a small move does not refute its one-sided
        claim, so it stays a hit.
    """
    if recommendation == PROCEED:
        return _classify_proceed(
            forward_return_pct, neutral_threshold_pct, severe_threshold_pct
        )
    return _classify_skip(
        forward_return_pct, neutral_threshold_pct, severe_threshold_pct
    )


def _classify_proceed(
    forward_return_pct: float, neutral_threshold_pct: float, severe_threshold_pct: float
) -> str:
    """Classify a `proceed`: one-sided, so no NEUTRAL bucket exists."""
    if forward_return_pct > -neutral_threshold_pct:
        return HIT
    if forward_return_pct <= -severe_threshold_pct:
        return MISS_SEVERE
    return MISS_MILD


def _classify_skip(
    forward_return_pct: float, neutral_threshold_pct: float, severe_threshold_pct: float
) -> str:
    """Classify a `skip`: a decline is the hit, an advance an opportunity cost."""
    if forward_return_pct <= -neutral_threshold_pct:
        return HIT
    if abs(forward_return_pct) < neutral_threshold_pct:
        return NEUTRAL
    if forward_return_pct >= severe_threshold_pct:
        return MISS_SEVERE
    return MISS_MILD


def evaluate_verdicts(
    market_store: MarketStore,
    state_store: StateStore,
    request: EvaluationRequest,
) -> EvaluateSummary:
    """Classify every collected verdict whose horizon matured by `as_of`.

    Only prices dated `<= request.as_of` are ever read, and each horizon's
    return is fixed at its own maturity session, so later sessions inside the
    window cannot leak into a shorter horizon's result.

    Args:
        market_store: Bars source for the trading calendar and each symbol's
            maturity close.
        state_store: Read source for `verdicts`, write target for
            `verdict_outcomes`.
        request: The batch's cutoff, thresholds, benchmark symbol, and whether
            to bound the work to slices that are not already recorded.

    Returns:
        Counts plus a note per symbol skipped for missing bars. A horizon that
        has simply not matured yet is counted in `pending_slice_count` rather
        than noted: in a batch that runs every few days, most recent runs are
        legitimately not due, and noting each one would drown the real
        data-quality signals.
    """
    as_of = request.as_of
    window_start = as_of - timedelta(
        days=request.thresholds.lookback_window_days + _EVALUATION_WINDOW_PADDING_DAYS
    )
    # P8-124: classifying a same-day loser would write `verdict_outcomes` rows
    # that every window aggregate then double-counts.
    runs = _group_by_run(
        keep_adopted_rows(
            state_store.get_verdicts_in_window(window_start, as_of), state_store
        )
    )
    only_pending = request.only_pending
    recorded = (
        state_store.get_recorded_outcome_slices(tuple(runs)) if only_pending else {}
    )

    stores = _EvaluationStores(market_store=market_store, state_store=state_store)
    notes: list[str] = []
    evaluated = pending = already_recorded = outcome_count = preserved_count = 0
    for run_id, rows in runs.items():
        expected = frozenset((row.symbol, row.recommendation) for row in rows)
        for horizon_days in HORIZON_DAYS:
            if only_pending and recorded.get((run_id, horizon_days)) == expected:
                already_recorded += 1
                continue
            slice_result = _evaluate_slice(stores, rows, horizon_days, request, notes)
            if slice_result is None:
                pending += 1
                continue
            state_store.replace_verdict_outcomes(
                run_id, horizon_days, slice_result.outcomes
            )
            evaluated += 1
            outcome_count += len(slice_result.outcomes)
            preserved_count += slice_result.preserved_count

    return EvaluateSummary(
        evaluated_slice_count=evaluated,
        pending_slice_count=pending,
        recorded_slice_count=already_recorded,
        outcome_count=outcome_count,
        notes=tuple(notes),
        preserved_outcome_count=preserved_count,
    )


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """One evaluation batch's cutoff, thresholds, benchmark, and scope.

    Grouped into one value to keep `evaluate_verdicts` and `_evaluate_slice`
    within the project's parameter-count guideline, mirroring
    `postmortem._PostmortemRequest`.

    `only_pending` is Issue #209's scope switch. `False` (the manual
    `copilot-retro evaluate` / `prepare` batch) re-classifies every matured
    slice in the window, which is how a *price* correction reaches
    `verdict_outcomes`. `True` (the daily step, which now runs ahead of the
    analysis export) leaves alone every slice whose recorded outcomes already
    match the run's verdicts exactly, so the pre-export cost follows the
    slices that newly matured rather than the whole window. A corrected
    verdict -- a symbol added, dropped, or flipped between `proceed` and
    `skip` -- stops matching and is reclassified either way, and so does a
    slice still missing a symbol whose bars never arrived.
    """

    as_of: date
    thresholds: PostmortemConfig
    benchmark_symbol: str
    only_pending: bool = False


def _finite_or_none(value: float | None) -> float | None:
    """Collapse an unusable benchmark return to `None` (Issue #190).

    A stored bar with a `NaN` close produces a `NaN` return rather than a
    missing one. Persisting that into `benchmark_return_pct` would put a value
    in a column whose `NULL` means "not measured", and a `NaN` silently
    poisons every excess-return average computed from it.
    """
    return None if value is None or not math.isfinite(value) else value


def _group_by_run(rows: Sequence[VerdictRow]) -> dict[UUID, list[VerdictRow]]:
    """Group verdict rows by run, preserving the query's deterministic order."""
    grouped: dict[UUID, list[VerdictRow]] = {}
    for row in rows:
        grouped.setdefault(row.run_id, []).append(row)
    return grouped


@dataclass(frozen=True, slots=True)
class _SliceEvaluation:
    """One matured `(run, horizon)` slice's replacement outcomes.

    `preserved_count` is the subset of `outcomes` carried forward unchanged
    from the previous evaluation rather than recomputed this round (Issue
    #424) -- see `EvaluateSummary.preserved_outcome_count`.
    """

    outcomes: tuple[VerdictOutcomeRecord, ...]
    preserved_count: int = 0


@dataclass(frozen=True, slots=True)
class _EvaluationStores:
    """The two stores `_evaluate_slice` reads from.

    Grouped into one value to stay within the project's parameter-count
    guideline, mirroring `EvaluationRequest`.
    """

    market_store: MarketStore
    state_store: StateStore


def _evaluate_slice(
    stores: _EvaluationStores,
    rows: Sequence[VerdictRow],
    horizon_days: int,
    request: EvaluationRequest,
    notes: list[str],
) -> _SliceEvaluation | None:
    """Classify one `(run, horizon)`; return `None` if it has not matured.

    Each record also carries the two closes the classification was computed
    from (`entry_close` / `maturity_close`, Issue #413). Those are audit
    values, not raw prices: both are quoted on the *maturity date's* own
    adjustment basis, which is what `compute_forward_return` divides, so a
    run-day close recorded here is the split-adjusted number rather than the
    dollars that traded that morning. Reading them any other way would make
    them disagree with the ratio they are meant to explain.
    """
    run_id = rows[0].run_id
    run_date = rows[0].as_of
    maturity_date = find_maturity_trading_day(
        stores.market_store,
        request.benchmark_symbol,
        run_date,
        horizon_days,
        as_of=request.as_of,
    )
    if maturity_date is None:
        logger.debug(
            "retro evaluate: %s の %dd horizon は %s 時点で未満期",
            run_id,
            horizon_days,
            request.as_of,
        )
        return None

    # Issue #190: one benchmark read per `(run, horizon)`, not per symbol --
    # every row in the slice spans exactly the same two sessions, so the
    # market's move over it is one number. `None` (no benchmark bars) is
    # recorded as such; it must not become a silent zero, which would turn
    # "unmeasured" into "the market went nowhere".
    benchmark_return_pct = _finite_or_none(
        compute_forward_return(
            stores.market_store, request.benchmark_symbol, run_date, maturity_date
        )
    )

    outcomes: list[VerdictOutcomeRecord] = []
    preserved_count = 0
    # Fetched at most once per slice, and only when a row actually fails to
    # recompute -- the common case (every row matures cleanly) never pays for
    # this read (Issue #424).
    previous_by_symbol: dict[str, VerdictOutcomeRecord] | None = None
    for row in rows:
        # The detail form, not the bare ratio: `entry_close`/`maturity_close`
        # are audit columns recording *which prices this was classified at*,
        # so they must be the very numbers the ratio divided rather than a
        # second read that a later store repair could answer differently.
        forward_return = compute_forward_return_detail(
            stores.market_store, row.symbol, run_date, maturity_date
        )
        if forward_return is None:
            if previous_by_symbol is None:
                previous_by_symbol = {
                    previous.symbol: previous
                    for previous in stores.state_store.get_verdict_outcomes_for_slice(
                        run_id, horizon_days
                    )
                }
            preserved = previous_by_symbol.get(row.symbol)
            # Issue #424: `replace_verdict_outcomes` replaces the whole slice
            # it is given, so simply omitting this row here would delete a
            # previously recorded, already-audited classification -- not
            # correct it -- the moment its maturity-day close goes missing
            # from the store (a re-fetch that dropped a historical bar, for
            # example). Carry the old row forward untouched instead. Only
            # when the verdict itself was corrected since (`recommendation`
            # no longer matches) is the old row not trustworthy enough to
            # keep; it is dropped, exactly as an unrecomputable row always
            # was before this fix.
            if preserved is not None and preserved.recommendation == row.recommendation:
                outcomes.append(preserved)
                preserved_count += 1
                notes.append(
                    f"{run_date.isoformat()} {row.symbol} {horizon_days}d: "
                    f"満期日 {maturity_date.isoformat()} までの終値が揃わないため、"
                    "既存の評価行を保持した（削除しない）"
                )
            else:
                notes.append(
                    f"{run_date.isoformat()} {row.symbol} {horizon_days}d: "
                    f"満期日 {maturity_date.isoformat()} までの終値が揃わないためスキップ"
                )
            continue
        outcomes.append(
            VerdictOutcomeRecord(
                run_id=run_id,
                symbol=row.symbol,
                horizon_days=horizon_days,
                as_of=maturity_date,
                recommendation=row.recommendation,
                forward_return_pct=forward_return.pct,
                benchmark_return_pct=benchmark_return_pct,
                entry_close=forward_return.run_close,
                maturity_close=forward_return.as_of_close,
                classification=classify_verdict_outcome(
                    row.recommendation,
                    forward_return.pct,
                    neutral_threshold_pct=request.thresholds.neutral_threshold_pct,
                    severe_threshold_pct=request.thresholds.severe_threshold_pct,
                ),
            )
        )
    return _SliceEvaluation(outcomes=tuple(outcomes), preserved_count=preserved_count)
