"""Retrospective aggregate metrics (P8-31, design §3.4).

Pure functions over already-classified `verdict_outcomes` rows -- except
`compute_verdict_mix` (P8-120), which reads the window's raw `verdicts`
instead, since a `proceed`-less window never matures an outcome to read. No
database, no clock, no network either way. `retro/export.py` reads the rows
and packs the results into `retro_input.json`; the skill narrates them but
never recomputes them.

Three properties shape the shapes below:

* **Every metric carries an ID.** `retro_result.json`'s `evidence_refs` must be
  provable subsets of what the export supplied (E32.4), which only works if
  each aggregate is addressable rather than merely rendered.
* **A baseline travels with each rate.** A verdict is an extra filter over
  already-screened candidates, so "15% of proceeds fell 2%" only means
  something next to how often the whole candidate pool fell that far
  (design §3.4). A rate under its watch level but worse than the pool it
  filters is still flagged.
* **A missing value is `None`, never 0.0.** An empty window and a genuinely
  zero rate lead to opposite conclusions.

Thresholds come from `settings.postmortem` (decision D6). The one number with
no existing home is the proceed severe-miss watch level below.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.analysis.news_supply import (
    DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS,
)
from swing_copilot.backtest.metrics import (
    compute_avg_r_multiple,
    compute_expectancy_per_trade,
    compute_profit_factor,
    compute_win_rate,
    exit_reason_breakdown,
    holding_days_stats,
)
from swing_copilot.retro.evaluate import (
    HIT,
    HORIZON_DAYS,
    MISS_SEVERE,
    NEUTRAL,
    PROCEED,
)
from swing_copilot.storage.tracking_records import (
    OPEN,
    TRACKED_RECOMMENDATIONS,
    TRACKING_EXIT_REASONS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date
    from uuid import UUID

    from swing_copilot.config import PostmortemConfig
    from swing_copilot.storage.tracking_records import (
        VerdictPosition,
        VerdictPositionMark,
    )
    from swing_copilot.storage.verdict_records import (
        VerdictCitationRow,
        VerdictOutcomeRecord,
        VerdictReasonBasisRow,
        VerdictRow,
    )

#: Watch level for the proceed severe-miss rate (design §3.4, 要検証): more
#: than one severe adverse move in ~7 accepted candidates puts the filter's
#: added value in question. Deliberately a module constant rather than a
#: `RetroConfig` field -- D6 keeps the retrospective's tunables in
#: `settings.postmortem`, and this bound is itself one of the things the
#: mechanism is meant to review.
PROCEED_SEVERE_MISS_WATCH_RATE = 0.15

#: Issue #154: the level a verdict row carries when its archive predates the
#: measurement. A separate bucket rather than folding into `none`, which is a
#: measured zero -- collapsing the two would let unmeasured history argue for
#: or against the threshold it never saw.
UNRECORDED_NEWS_SUPPLY_LEVEL = "unrecorded"

#: Issue #191: the bucket a verdict reason falls into when its author left
#: `basis` unset (or the row predates the field). Reported rather than
#: dropped, for the same reason the level above is: the share of the window
#: that is untagged is what says whether the tagged rows mean anything.
UNTAGGED_VERDICT_BASIS = "untagged"

#: Two-sided 95% normal quantile, the confidence level every interval in this
#: module reports (Issue #190). Hard-coded rather than configurable on
#: purpose: an interval whose level can be tuned per run is an interval that
#: can be widened until a proposal passes its gate.
CONFIDENCE_LEVEL = 0.95
_Z_TWO_SIDED_95 = 1.959963984540054

#: Below two observations a sample variance is undefined, so the spread is
#: reported as unknown rather than as zero.
_MIN_SAMPLES_FOR_SPREAD = 2

_SEPARATION = "separation"
_SEPARATION_PAIRED = "separation_paired"
_SEPARATION_PAIRED_EXCESS = "separation_paired_excess"
_PROCEED_SEVERE_MISS_RATE = "proceed_severe_miss_rate"
_SKIP_HIT_RATE = "skip_hit_rate"
_VERDICT_MIX = "verdict_mix"
_NEWS_SUPPLY = "news_supply"
_METRIC_PREFIX = "metric"
_COMPOSED = "composed"

#: P8-120: below this many windowed verdicts, a zero-proceed stretch is not
#: distinguishable from an ordinary quiet period -- 8/3-8/6's 4 scheduled
#: days (32 verdicts) is the level that motivated the ticket.
_VERDICT_MIX_FLAG_MIN_VERDICT_COUNT = 20

#: Decides whether a rate deserves attention, given the rate and its
#: baseline. The two rates flag for opposite reasons, so the rule travels
#: with the metric instead of branching inside the shared assembly.
_FlagRule = Callable[[float | None, float | None], bool]


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """One horizon's (or the weighted headline's) value for a metric.

    `horizon_days is None` marks the weight-composed headline; `sample_size`
    is the raw row count behind it, which is what the preliminary flag reads.

    Issue #190 added the dispersion fields. A point estimate with no spread
    around it is exactly what let `n >= 20` alone promote noise into a config
    change, so `stderr` / `ci_low` / `ci_high` travel with every value that
    has a defined one. They are `None` on the weight-composed headline
    deliberately: two horizons measured over the same runs are not
    independent samples, so any interval computed across them would be
    narrower than the truth and would read as more certain than the data is.
    `excluded_day_count` is set only by the paired metrics, where it counts
    the run days dropped for having just one verdict side.
    """

    metric_id: str
    horizon_days: int | None
    value: float | None
    sample_size: int
    is_preliminary: bool
    stderr: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    excluded_day_count: int | None = None


@dataclass(frozen=True, slots=True)
class RateMetricSummary:
    """A rate metric plus the same-period baseline it is judged against.

    `ci_low` / `ci_high` are a Wilson score interval (Issue #190), which
    stays inside `[0, 1]` and stays sane at the small counts and extreme
    rates this window routinely produces -- both places where the textbook
    normal interval on a proportion misbehaves. `None` on the weight-composed
    headline, for the same non-independence reason as `MetricSummary`, and
    because its "counts" are weighted rather than observed.
    """

    metric_id: str
    horizon_days: int | None
    value: float | None
    baseline_value: float | None
    is_flagged: bool
    sample_size: int
    is_preliminary: bool
    ci_low: float | None = None
    ci_high: float | None = None


@dataclass(frozen=True, slots=True)
class VerdictMixSummary:
    """Whether the window's verdicts could produce `proceed` at all (P8-120).

    Unlike the other metrics here, this has no baseline (there is nothing to
    compare a mix against) and no horizon breakdown (a verdict is not tied to
    one -- `horizon_days` is deliberately absent). Computed from the window's
    `verdicts` rather than `verdict_outcomes`, so it is not silenced by the
    same condition it exists to detect: a `proceed`-less window never
    matures any outcomes for `separation` / `proceed_severe_miss_rate` to
    measure, but `verdict_mix` still sees every skip.
    """

    metric_id: str
    run_count: int
    verdict_count: int
    proceed_count: int
    skip_count: int
    proceed_ratio: float | None
    is_flagged: bool


@dataclass(frozen=True, slots=True)
class NewsSupplyCell:
    """One `(news supply level, recommendation)` cell of the supply cross-tab.

    The mention statistics are `None` only for the `unrecorded` level, where
    there is no count to describe -- everywhere else they are what the
    threshold is actually being judged against.
    """

    cell_id: str
    level: str
    recommendation: str
    verdict_count: int
    min_symbol_mention_items: int | None
    max_symbol_mention_items: int | None
    mean_symbol_mention_items: float | None


@dataclass(frozen=True, slots=True)
class NewsSupplySummary:
    """Whether the `sufficient` threshold matches what the verdicts did (#154).

    Computed from the window's `verdicts` rather than `verdict_outcomes`, like
    `verdict_mix`: the question is which supply levels the layer was willing
    to say `proceed` under, which is answerable the day the verdict is made
    and must not wait for maturity.

    `sufficient_threshold` travels with the counts so a dossier read months
    later says which boundary produced its cells, and a proposal to move it
    can cite the value it is changing.
    """

    metric_id: str
    sufficient_threshold: int
    verdict_count: int
    recorded_verdict_count: int
    unrecorded_verdict_count: int
    cells: tuple[NewsSupplyCell, ...]


#: Notional every shadow position is normalized to before its P&L is measured
#: (Issue #190). The tracking ledger never decided a share count -- nobody
#: sized a position that was never put on offer -- so `pnl` in dollars would
#: mean "one share of a $400 stock beats ten of a $20 one". Sizing every
#: position to $100 instead makes `pnl` numerically identical to the realized
#: return in percent, which is what profit factor and expectancy then report.
_SHADOW_NOTIONAL_USD = 100.0

_TRACKED_PERFORMANCE = "tracked_performance"

#: The stratum that pools both verdict sides. Not a recommendation value, so
#: it cannot collide with one.
ALL_RECOMMENDATIONS = "all"


@dataclass(frozen=True, slots=True)
class ShadowTrade:
    """One closed shadow position in `backtest.metrics.ClosedTrade` shape.

    Sized to `_SHADOW_NOTIONAL_USD`, so `pnl` *is* the realized return in
    percentage points; `days_held` is the ledger's own session count, not a
    calendar difference.
    """

    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    shares: float
    initial_stop_price: float | None
    exit_reason: str
    days_held: int

    @property
    def pnl(self) -> float:
        """Realized return in percentage points (see `_SHADOW_NOTIONAL_USD`)."""
        return (self.exit_price - self.entry_price) * self.shares


@dataclass(frozen=True, slots=True)
class ExitReasonCount:
    """How many of a stratum's closed shadow positions ended a given way."""

    reason: str
    count: int


@dataclass(frozen=True, slots=True)
class TrackedPerformance:
    """One verdict side's realized record in the shadow-tracking ledger.

    The measurement Issue #190 exists to supply: `proceed` and `skip` carried
    under identical exit rules, so the difference between the two rows is the
    qualitative layer's contribution rather than an artifact of two different
    measurements. Every rate here comes from `backtest/metrics.py`, the same
    functions the simulator and the paper journal use.

    All monetary figures are percentage points, never dollars (see
    `_SHADOW_NOTIONAL_USD`). `None` means "not computable from this set",
    never zero: an empty stratum and a genuinely flat one read oppositely.
    """

    metric_id: str
    recommendation: str
    closed_count: int
    open_count: int
    win_rate: float | None
    profit_factor: float | None
    expectancy_pct: float | None
    avg_r_multiple: float | None
    avg_holding_days: float | None
    exit_reason_counts: tuple[ExitReasonCount, ...]


@dataclass(frozen=True, slots=True)
class BasisContributionRow:
    """One evidence kind's tally across the verdicts that rested on it.

    The `basis` counterpart of `SourceContributionRow` (Issue #191). Source
    contribution can only ever separate *providers* -- finnhub against edgar
    -- which cannot answer whether verdicts justified by an earnings surprise
    outperform verdicts justified by the technical score alone, since both
    kinds of reasoning may cite the same provider or none at all.
    """

    basis_id: str
    basis: str
    verdict_count: int
    hit_count: int
    miss_count: int
    neutral_count: int
    hit_citation_ratio: float | None


@dataclass(frozen=True, slots=True)
class SourceContributionRow:
    """One `(source_type, provider)` group's citation tally.

    `provider` is the source ID's prefix (`finnhub:123` -> `finnhub`), the
    same encoding `analysis/export.py` uses: `text_items` has no provider
    column, so the ID prefix is the only recorded origin.
    """

    contribution_id: str
    source_type: str
    provider: str
    citation_count: int
    hit_citation_count: int
    miss_citation_count: int
    neutral_citation_count: int
    hit_citation_ratio: float | None


def compute_separation(
    outcomes: Sequence[VerdictOutcomeRecord], thresholds: PostmortemConfig
) -> tuple[MetricSummary, ...]:
    """Return proceed-minus-skip mean forward return per horizon, plus headline.

    Separation is the qualitative layer's reason to exist (design §3.4): if
    the symbols it waved through do not outperform the ones it set aside, the
    layer is adding no information -- or selecting adversely.

    Args:
        outcomes: Classified rows for the window, both horizons mixed.
        thresholds: Horizon weights and the preliminary-sample floor
            (`settings.postmortem`).

    Returns:
        One summary per horizon plus the weight-composed headline. A horizon
        missing either group has `value=None`: a one-sided window cannot
        state a difference, and 0.0 would read as "no edge" instead of "not
        measurable".
    """
    per_horizon = [
        _separation_for(
            [row for row in outcomes if row.horizon_days == horizon_days],
            horizon_days,
            thresholds,
        )
        for horizon_days in HORIZON_DAYS
    ]
    return (*per_horizon, _composed_mean(per_horizon, _SEPARATION, thresholds))


def _separation_for(
    rows: Sequence[VerdictOutcomeRecord],
    horizon_days: int,
    thresholds: PostmortemConfig,
) -> MetricSummary:
    proceed = [row.forward_return_pct for row in rows if row.recommendation == PROCEED]
    skip = [row.forward_return_pct for row in rows if row.recommendation != PROCEED]
    value = (
        sum(proceed) / len(proceed) - sum(skip) / len(skip)
        if proceed and skip
        else None
    )
    stderr = _welch_stderr(proceed, skip) if value is not None else None
    return MetricSummary(
        metric_id=_metric_id(_SEPARATION, horizon_days),
        horizon_days=horizon_days,
        value=value,
        sample_size=len(rows),
        is_preliminary=len(rows) < thresholds.preliminary_sample_threshold,
        stderr=stderr,
        ci_low=_ci_bound(value, stderr, sign=-1),
        ci_high=_ci_bound(value, stderr, sign=1),
    )


def compute_separation_paired(
    outcomes: Sequence[VerdictOutcomeRecord], thresholds: PostmortemConfig
) -> tuple[MetricSummary, ...]:
    """Return separation as the mean of *per-run-day* proceed-minus-skip gaps.

    The pooled `compute_separation` averages every `proceed` in the window
    against every `skip` in it, which silently compares symbols judged on
    different days under different market conditions: a window whose
    `proceed`s cluster on strong days scores well for reasons the verdict
    layer had nothing to do with. Differencing inside each run day removes
    that day's common move before anything is averaged, so the market's own
    swings cancel instead of accumulating (Issue #190).

    Days carrying only one verdict side state no difference at all and are
    excluded rather than treated as zero; `excluded_day_count` reports how
    many, because a metric computed from three of twenty days is a different
    claim from one computed from twenty.

    Args:
        outcomes: Classified rows for the window, both horizons mixed.
        thresholds: Horizon weights and the preliminary-sample floor.

    Returns:
        One summary per horizon plus the weight-composed headline. Each
        horizon's `sample_size` counts the rows that actually contributed.
    """
    return _paired_separation(outcomes, thresholds, _SEPARATION_PAIRED, _raw_return)


def compute_separation_paired_excess(
    outcomes: Sequence[VerdictOutcomeRecord], thresholds: PostmortemConfig
) -> tuple[MetricSummary, ...]:
    """Return the paired separation measured on *excess* (benchmark-relative) returns.

    The same day-by-day pairing as `compute_separation_paired`, over
    `forward_return_pct - benchmark_return_pct`. Published as its own metric
    rather than replacing the raw one: pairing already removes the common
    daily move, so agreement between the two versions is evidence that the
    effect is not a beta artifact, and disagreement is itself the finding.

    Rows whose `benchmark_return_pct` was never measured (classified before
    the column existed, or evaluated without benchmark bars) contribute
    nothing rather than being treated as zero excess.

    Args:
        outcomes: Classified rows for the window, both horizons mixed.
        thresholds: Horizon weights and the preliminary-sample floor.

    Returns:
        One summary per horizon plus the weight-composed headline. An archive
        with no benchmark column yields `value=None` throughout, which reads
        as "not measurable here" rather than as "no excess".
    """
    return _paired_separation(
        outcomes, thresholds, _SEPARATION_PAIRED_EXCESS, _excess_return
    )


def _raw_return(row: VerdictOutcomeRecord) -> float | None:
    return row.forward_return_pct


def _excess_return(row: VerdictOutcomeRecord) -> float | None:
    if row.benchmark_return_pct is None:
        return None
    return row.forward_return_pct - row.benchmark_return_pct


def _paired_separation(
    outcomes: Sequence[VerdictOutcomeRecord],
    thresholds: PostmortemConfig,
    metric: str,
    value_of: Callable[[VerdictOutcomeRecord], float | None],
) -> tuple[MetricSummary, ...]:
    per_horizon = [
        _paired_separation_for(
            [row for row in outcomes if row.horizon_days == horizon_days],
            horizon_days,
            thresholds,
            metric,
            value_of,
        )
        for horizon_days in HORIZON_DAYS
    ]
    return (*per_horizon, _composed_mean(per_horizon, metric, thresholds))


def _paired_separation_for(
    rows: Sequence[VerdictOutcomeRecord],
    horizon_days: int,
    thresholds: PostmortemConfig,
    metric: str,
    value_of: Callable[[VerdictOutcomeRecord], float | None],
) -> MetricSummary:
    """Average one horizon's per-day gaps.

    Days are keyed on `verdict_outcomes.as_of`, the *maturity* session. Within
    a single horizon that is a one-to-one relabelling of the run date (the
    maturity day is N sessions after the run), and `keep_adopted_rows` has
    already reduced each run date to one run, so grouping on it groups exactly
    one run day per key without needing a `runs` join.
    """
    # Seeded from *every* row's day, not just the usable ones: a day whose
    # rows were all dropped (no benchmark recorded, say) still stated no
    # difference and must be counted as excluded rather than vanishing.
    by_day: dict[date, list[tuple[str, float]]] = {row.as_of: [] for row in rows}
    contributing = 0
    for row in rows:
        value = value_of(row)
        if value is None:
            continue
        contributing += 1
        by_day[row.as_of].append((row.recommendation, value))

    differences: list[float] = []
    excluded_day_count = 0
    for cell in by_day.values():
        proceed = [value for side, value in cell if side == PROCEED]
        skip = [value for side, value in cell if side != PROCEED]
        if not proceed or not skip:
            excluded_day_count += 1
            continue
        differences.append(sum(proceed) / len(proceed) - sum(skip) / len(skip))

    value = statistics.fmean(differences) if differences else None
    stderr = _mean_stderr(differences)
    return MetricSummary(
        metric_id=_metric_id(metric, horizon_days),
        horizon_days=horizon_days,
        value=value,
        sample_size=contributing,
        is_preliminary=contributing < thresholds.preliminary_sample_threshold,
        stderr=stderr,
        ci_low=_ci_bound(value, stderr, sign=-1),
        ci_high=_ci_bound(value, stderr, sign=1),
        excluded_day_count=excluded_day_count,
    )


def _welch_stderr(group_a: Sequence[float], group_b: Sequence[float]) -> float | None:
    """Standard error of a difference of two independent means (Welch).

    `None` when either group has fewer than two observations, where the
    sample variance is undefined -- reported as unknown rather than as zero
    spread, which would manufacture a razor-thin interval around a single
    point.
    """
    if len(group_a) < _MIN_SAMPLES_FOR_SPREAD or len(group_b) < _MIN_SAMPLES_FOR_SPREAD:
        return None
    return math.sqrt(
        statistics.variance(group_a) / len(group_a)
        + statistics.variance(group_b) / len(group_b)
    )


def _mean_stderr(values: Sequence[float]) -> float | None:
    """Standard error of a mean; `None` below two observations."""
    if len(values) < _MIN_SAMPLES_FOR_SPREAD:
        return None
    return statistics.stdev(values) / math.sqrt(len(values))


def _ci_bound(value: float | None, stderr: float | None, *, sign: int) -> float | None:
    """One end of the normal-approximation interval, or `None` without a spread."""
    if value is None or stderr is None:
        return None
    return value + sign * _Z_TWO_SIDED_95 * stderr


def wilson_interval(
    successes: float, trials: float
) -> tuple[float | None, float | None]:
    """Return the Wilson score interval for a proportion (Issue #190).

    Preferred over the normal ("Wald") interval because it stays within
    `[0, 1]` and remains sensible at zero or unanimous counts, which this
    window produces routinely -- a `skip_hit_rate` of 0/3 is a real state, and
    a Wald interval reports it as the point `[0, 0]`.

    Args:
        successes: Observed successes. Must not exceed `trials`.
        trials: Observations behind the rate.

    Returns:
        `(low, high)`, or `(None, None)` when `trials <= 0` -- there is no
        proportion to bound.
    """
    if trials <= 0:
        return None, None
    z_squared = _Z_TWO_SIDED_95**2
    observed = successes / trials
    denominator = 1 + z_squared / trials
    center = (observed + z_squared / (2 * trials)) / denominator
    half_width = (
        _Z_TWO_SIDED_95
        * math.sqrt(observed * (1 - observed) / trials + z_squared / (4 * trials**2))
        / denominator
    )
    return max(0.0, center - half_width), min(1.0, center + half_width)


def _composed_mean(
    per_horizon: Sequence[MetricSummary], metric: str, thresholds: PostmortemConfig
) -> MetricSummary:
    """Weight the horizons that produced a value, renormalizing over them.

    Renormalization matters when only the 5-day horizon has matured, which is
    the normal state early in a window: weighting a missing 20-day value as
    zero would drag the headline toward zero and understate a real effect.
    """
    weighted = [
        (_horizon_weight(row.horizon_days, thresholds), row.value)
        for row in per_horizon
        if row.value is not None
    ]
    total_weight = sum(weight for weight, _ in weighted)
    value = (
        sum(weight * observed for weight, observed in weighted) / total_weight
        if total_weight > 0
        else None
    )
    sample_size = sum(row.sample_size for row in per_horizon)
    return MetricSummary(
        metric_id=_metric_id(metric, None),
        horizon_days=None,
        value=value,
        sample_size=sample_size,
        is_preliminary=sample_size < thresholds.preliminary_sample_threshold,
    )


@dataclass(frozen=True, slots=True)
class _RateParts:
    """One horizon's numerator/denominator before it becomes a rate.

    Kept separate so the composed headline can weight *counts* (as
    `compute_signal_performance` does) rather than average two rates computed
    from different sample sizes.
    """

    horizon_days: int
    numerator: float
    denominator: float
    baseline_numerator: float
    baseline_denominator: float
    #: Raw (unweighted) row count behind the rate, so the preliminary flag
    #: keeps meaning "n rows" even for the weight-composed headline.
    sample_size: int


def compute_proceed_severe_miss_rate(
    outcomes: Sequence[VerdictOutcomeRecord], thresholds: PostmortemConfig
) -> tuple[RateMetricSummary, ...]:
    """Return the share of `proceed` verdicts that suffered a severe adverse move.

    The baseline is the same window's severe-decline rate across *every*
    evaluated candidate, `skip`s included. It is computed from the raw return
    rather than from `classification`, because a `skip`'s MISS_SEVERE means a
    severe *advance* -- the opposite event.

    Args:
        outcomes: Classified rows for the window.
        thresholds: Severity boundary, horizon weights, preliminary floor.

    Returns:
        Per-horizon rates plus the weighted headline. `is_flagged` is set when
        the rate exceeds `PROCEED_SEVERE_MISS_WATCH_RATE` *or* the baseline.
    """
    severe = -thresholds.severe_threshold_pct
    parts = [
        _RateParts(
            horizon_days=horizon_days,
            numerator=sum(
                1
                for row in rows
                if row.recommendation == PROCEED and row.classification == MISS_SEVERE
            ),
            denominator=sum(1 for row in rows if row.recommendation == PROCEED),
            baseline_numerator=sum(
                1 for row in rows if row.forward_return_pct <= severe
            ),
            baseline_denominator=len(rows),
            sample_size=sum(1 for row in rows if row.recommendation == PROCEED),
        )
        for horizon_days, rows in _rows_by_horizon(outcomes)
    ]
    return _rate_summaries(parts, _PROCEED_SEVERE_MISS_RATE, thresholds, _flag_severe)


def compute_skip_hit_rate(
    outcomes: Sequence[VerdictOutcomeRecord], thresholds: PostmortemConfig
) -> tuple[RateMetricSummary, ...]:
    """Return the share of non-NEUTRAL `skip` verdicts that avoided a decline.

    Judged against the same window's decline rate among moves that cleared the
    noise band, so both sides of the comparison exclude the same "no
    information" moves (design §3.4: skip is a selective minority call, so a
    baseline comparison is the verdict, not an absolute threshold).

    Args:
        outcomes: Classified rows for the window.
        thresholds: Noise band, horizon weights, preliminary floor.

    Returns:
        Per-horizon rates plus the weighted headline. `is_flagged` is set when
        the rate falls below the baseline -- skipping picked worse than the
        pool it selected from.
    """
    noise = -thresholds.neutral_threshold_pct
    parts = [
        _RateParts(
            horizon_days=horizon_days,
            numerator=sum(
                1
                for row in rows
                if row.recommendation != PROCEED and row.classification == HIT
            ),
            denominator=sum(
                1
                for row in rows
                if row.recommendation != PROCEED and row.classification != NEUTRAL
            ),
            baseline_numerator=sum(
                1 for row in rows if row.forward_return_pct <= noise
            ),
            baseline_denominator=sum(
                1
                for row in rows
                if abs(row.forward_return_pct) >= thresholds.neutral_threshold_pct
            ),
            sample_size=sum(
                1
                for row in rows
                if row.recommendation != PROCEED and row.classification != NEUTRAL
            ),
        )
        for horizon_days, rows in _rows_by_horizon(outcomes)
    ]
    return _rate_summaries(parts, _SKIP_HIT_RATE, thresholds, _flag_below_baseline)


def shadow_trade(
    position: VerdictPosition, initial_mark: VerdictPositionMark | None
) -> ShadowTrade | None:
    """Adapt one closed shadow position into the shared measurement shape.

    Args:
        position: A tracked position; `None` is returned unless it is closed
            with both an exit price and an exit reason recorded.
        initial_mark: The position's entry-session mark, whose `stop_price` is
            the stop actually in force at entry. `VerdictPosition.stop_price`
            is *not* usable for this: it ratchets upward with the trailing
            stop, so reading it would understate the risk taken and inflate
            every R-multiple.

    Returns:
        The adapted trade, or `None` when the position is still open or its
        exit was never fully recorded (a corrupted row, excluded rather than
        measured with a guessed exit).
    """
    if (
        position.exit_date is None
        or position.exit_price is None
        or position.exit_reason is None
        or position.entry_price <= 0
    ):
        return None
    return ShadowTrade(
        entry_date=position.entry_date,
        entry_price=position.entry_price,
        exit_date=position.exit_date,
        exit_price=position.exit_price,
        shares=_SHADOW_NOTIONAL_USD / position.entry_price,
        initial_stop_price=None if initial_mark is None else initial_mark.stop_price,
        exit_reason=position.exit_reason,
        days_held=position.days_held,
    )


def compute_tracked_performance(
    positions: Sequence[VerdictPosition],
    marks: Mapping[tuple[UUID, str], VerdictPositionMark],
) -> tuple[TrackedPerformance, ...]:
    """Summarize the shadow ledger's realized record, stratified by verdict side.

    Issue #190's counterfactual: `proceed` and `skip` positions were opened at
    their run's close and carried under the same trailing stop and max-hold,
    so the two rows differ only by the judgement being measured. The pooled
    `all` row is the "buy every screened candidate" arm.

    Args:
        positions: Tracked positions, open and closed. The caller decides the
            window (this function is pure and applies no cutoff of its own).
        marks: Each position's *entry-session* mark, keyed by `(run_id,
            symbol)` -- `StateStore.get_earliest_verdict_position_marks()`.
            A position missing from the mapping simply contributes no
            R-multiple.

    Returns:
        One row per verdict side plus the pooled `all` row, always in that
        fixed order so a dossier's rows do not reshuffle between windows. A
        side with no positions still gets a row, with `None` rates and zero
        counts: "nothing has been tracked on this side yet" is exactly the
        reading the sample-size argument needs.
    """
    strata = (*TRACKED_RECOMMENDATIONS, ALL_RECOMMENDATIONS)
    return tuple(
        _tracked_performance(
            stratum,
            [
                position
                for position in positions
                if stratum in (position.recommendation, ALL_RECOMMENDATIONS)
            ],
            marks,
        )
        for stratum in strata
    )


def _tracked_performance(
    recommendation: str,
    positions: Sequence[VerdictPosition],
    marks: Mapping[tuple[UUID, str], VerdictPositionMark],
) -> TrackedPerformance:
    trades = [
        trade
        for position in positions
        if (
            trade := shadow_trade(
                position, marks.get((position.run_id, position.symbol))
            )
        )
        is not None
    ]
    holding = holding_days_stats(trades)
    return TrackedPerformance(
        metric_id=f"{_METRIC_PREFIX}:{_TRACKED_PERFORMANCE}:{recommendation}",
        recommendation=recommendation,
        closed_count=len(trades),
        open_count=sum(1 for position in positions if position.status == OPEN),
        win_rate=compute_win_rate(trades),
        profit_factor=compute_profit_factor(trades),
        expectancy_pct=compute_expectancy_per_trade(trades),
        avg_r_multiple=compute_avg_r_multiple(trades),
        avg_holding_days=None if holding is None else holding.median,
        exit_reason_counts=tuple(
            ExitReasonCount(reason=reason, count=count)
            for reason, count in sorted(
                exit_reason_breakdown(trades, TRACKING_EXIT_REASONS).items()
            )
        ),
    )


def compute_verdict_mix(verdicts: Sequence[VerdictRow]) -> VerdictMixSummary:
    """Return how the window's verdicts split between `proceed` and `skip`.

    Args:
        verdicts: Every verdict in the window, unfiltered by maturity.

    Returns:
        Counts, the resulting `proceed` ratio (`None` on an empty window,
        never `0.0`), and `is_flagged` when the window has enough verdicts to
        be meaningful (`>= 20`) yet produced zero `proceed`.
    """
    verdict_count = len(verdicts)
    proceed_count = sum(1 for row in verdicts if row.recommendation == PROCEED)
    skip_count = verdict_count - proceed_count
    return VerdictMixSummary(
        metric_id=_VERDICT_MIX,
        run_count=len({row.run_id for row in verdicts}),
        verdict_count=verdict_count,
        proceed_count=proceed_count,
        skip_count=skip_count,
        proceed_ratio=proceed_count / verdict_count if verdict_count > 0 else None,
        is_flagged=(
            verdict_count >= _VERDICT_MIX_FLAG_MIN_VERDICT_COUNT and proceed_count == 0
        ),
    )


def compute_news_supply_mix(
    verdicts: Sequence[VerdictRow],
    sufficient_mention_items: int = DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS,
) -> NewsSupplySummary:
    """Cross the archived news-supply level against what the verdict said.

    The measurable half of Issue #154: how often the layer said `proceed`
    under a supply it had itself graded `sparse` or `none`. Whether a
    `sufficient` grade was wrong in the other direction (an expert who still
    found nothing company-specific) is not visible from these counts and is
    left to the skill's re-reading of the surprise dossiers.

    Args:
        verdicts: Every verdict in the window, unfiltered by maturity.
        sufficient_mention_items: The `sufficient` floor the run was graded
            under, from `settings.analysis.sufficient_news_mention_items`. It
            is reported back in the summary so a dossier read later says which
            boundary produced its cells.

    Returns:
        One cell per `(level, recommendation)` actually seen, ordered by that
        key, plus how many rows carried no measurement at all. An empty
        window yields no cells rather than zero-filled ones.
    """
    grouped: dict[tuple[str, str], list[VerdictRow]] = defaultdict(list)
    for row in verdicts:
        level = (
            UNRECORDED_NEWS_SUPPLY_LEVEL
            if row.news_supply is None
            else row.news_supply.level
        )
        grouped[(level, row.recommendation)].append(row)

    unrecorded = sum(1 for row in verdicts if row.news_supply is None)
    return NewsSupplySummary(
        metric_id=f"{_METRIC_PREFIX}:{_NEWS_SUPPLY}",
        sufficient_threshold=sufficient_mention_items,
        verdict_count=len(verdicts),
        recorded_verdict_count=len(verdicts) - unrecorded,
        unrecorded_verdict_count=unrecorded,
        cells=tuple(
            _news_supply_cell(level, recommendation, cell)
            for (level, recommendation), cell in sorted(grouped.items())
        ),
    )


def _news_supply_cell(
    level: str, recommendation: str, cell: Sequence[VerdictRow]
) -> NewsSupplyCell:
    """Summarize one cell's mention counts, which `unrecorded` rows lack."""
    mentions = [
        row.news_supply.symbol_mention_items
        for row in cell
        if row.news_supply is not None
    ]
    return NewsSupplyCell(
        cell_id=f"{_METRIC_PREFIX}:{_NEWS_SUPPLY}:{level}:{recommendation}",
        level=level,
        recommendation=recommendation,
        verdict_count=len(cell),
        min_symbol_mention_items=min(mentions) if mentions else None,
        max_symbol_mention_items=max(mentions) if mentions else None,
        mean_symbol_mention_items=(sum(mentions) / len(mentions) if mentions else None),
    )


def _rows_by_horizon(
    outcomes: Sequence[VerdictOutcomeRecord],
) -> list[tuple[int, list[VerdictOutcomeRecord]]]:
    return [
        (horizon_days, [row for row in outcomes if row.horizon_days == horizon_days])
        for horizon_days in HORIZON_DAYS
    ]


def _rate_summaries(
    parts: Sequence[_RateParts],
    metric: str,
    thresholds: PostmortemConfig,
    is_flagged: _FlagRule,
) -> tuple[RateMetricSummary, ...]:
    """Turn per-horizon counts into per-horizon rates plus the weighted headline."""
    summaries = [
        _rate_summary(
            _metric_id(metric, part.horizon_days),
            part.horizon_days,
            part,
            thresholds,
            is_flagged,
        )
        for part in parts
    ]
    composed = _RateParts(
        # The headline is not one horizon's; `_metric_id(..., None)` names it.
        horizon_days=0,
        numerator=sum(
            _horizon_weight(part.horizon_days, thresholds) * part.numerator
            for part in parts
        ),
        denominator=sum(
            _horizon_weight(part.horizon_days, thresholds) * part.denominator
            for part in parts
        ),
        baseline_numerator=sum(
            _horizon_weight(part.horizon_days, thresholds) * part.baseline_numerator
            for part in parts
        ),
        baseline_denominator=sum(
            _horizon_weight(part.horizon_days, thresholds) * part.baseline_denominator
            for part in parts
        ),
        sample_size=sum(part.sample_size for part in parts),
    )
    return (
        *summaries,
        _rate_summary(_metric_id(metric, None), None, composed, thresholds, is_flagged),
    )


def _rate_summary(
    metric_id: str,
    horizon_days: int | None,
    part: _RateParts,
    thresholds: PostmortemConfig,
    is_flagged: _FlagRule,
) -> RateMetricSummary:
    value = part.numerator / part.denominator if part.denominator > 0 else None
    baseline = (
        part.baseline_numerator / part.baseline_denominator
        if part.baseline_denominator > 0
        else None
    )
    # The weight-composed headline's numerator/denominator are weighted, not
    # observed, counts; a Wilson interval on them would describe a sample that
    # was never drawn. Only the per-horizon rows get one (Issue #190).
    ci_low, ci_high = (
        wilson_interval(part.numerator, part.denominator)
        if horizon_days is not None
        else (None, None)
    )
    return RateMetricSummary(
        metric_id=metric_id,
        horizon_days=horizon_days,
        value=value,
        baseline_value=baseline,
        is_flagged=is_flagged(value, baseline),
        sample_size=part.sample_size,
        is_preliminary=part.sample_size < thresholds.preliminary_sample_threshold,
        ci_low=ci_low,
        ci_high=ci_high,
    )


def _flag_severe(value: float | None, baseline: float | None) -> bool:
    if value is None:
        return False
    return value > PROCEED_SEVERE_MISS_WATCH_RATE or (
        baseline is not None and value > baseline
    )


def _flag_below_baseline(value: float | None, baseline: float | None) -> bool:
    return value is not None and baseline is not None and value < baseline


def compute_basis_contribution(
    bases: Sequence[VerdictReasonBasisRow],
    outcomes: Sequence[VerdictOutcomeRecord],
) -> tuple[BasisContributionRow, ...]:
    """Tally how each kind of evidence-backed reasoning actually turned out.

    Scored exactly like `compute_source_contribution`: every basis a verdict
    rested on is credited with every horizon of that `(run, symbol)` which
    matured, so a verdict that hit at 5 days and missed severely at 20 lands
    in both buckets for each of its bases.

    Reasons the writer left untagged are reported under
    `UNTAGGED_VERDICT_BASIS` rather than dropped: how much of the window is
    untagged is what tells a reader whether the other rows can be trusted at
    all, and silently omitting them would make a thin sample look complete.

    Args:
        bases: One row per `(run, symbol, basis)` for verdicts matured in the
            window.
        outcomes: The same window's classified rows.

    Returns:
        One row per basis, ordered by basis, untagged last. A basis whose
        verdicts have no matured outcome keeps a `None` ratio -- "used but
        never measurable" is itself worth seeing.
    """
    by_symbol: dict[tuple[str, str], list[VerdictOutcomeRecord]] = defaultdict(list)
    for outcome in outcomes:
        by_symbol[(str(outcome.run_id), outcome.symbol)].append(outcome)

    grouped: dict[str, list[list[VerdictOutcomeRecord]]] = defaultdict(list)
    for row in bases:
        grouped[row.basis or UNTAGGED_VERDICT_BASIS].append(
            by_symbol.get((str(row.run_id), row.symbol), [])
        )

    return tuple(
        _basis_row(basis, cited)
        for basis, cited in sorted(
            grouped.items(),
            key=lambda item: (item[0] == UNTAGGED_VERDICT_BASIS, item[0]),
        )
    )


def _basis_row(
    basis: str, cited: Sequence[Sequence[VerdictOutcomeRecord]]
) -> BasisContributionRow:
    """Fold one basis's per-verdict outcome lists into a single tally."""
    linked = [outcome for verdict_outcomes in cited for outcome in verdict_outcomes]
    hits = sum(1 for outcome in linked if outcome.classification == HIT)
    neutrals = sum(1 for outcome in linked if outcome.classification == NEUTRAL)
    misses = len(linked) - hits - neutrals
    decided = hits + misses
    return BasisContributionRow(
        basis_id=f"{_METRIC_PREFIX}:basis_contribution:{basis}",
        basis=basis,
        verdict_count=len(cited),
        hit_count=hits,
        miss_count=misses,
        neutral_count=neutrals,
        hit_citation_ratio=hits / decided if decided > 0 else None,
    )


def compute_source_contribution(
    citations: Sequence[VerdictCitationRow],
    outcomes: Sequence[VerdictOutcomeRecord],
) -> tuple[SourceContributionRow, ...]:
    """Tally how often each source informed a verdict, and how those turned out.

    Each citation is scored against every horizon of its own `(run, symbol)`
    that matured, so a source cited before a symbol that hit at 5 days and
    missed severely at 20 lands in both buckets: the retrospective is looking
    for sources that skew toward misses, not for a single verdict per source.

    Args:
        citations: Cited sources for verdicts matured in the window.
        outcomes: The same window's classified rows.

    Returns:
        One row per `(source_type, provider)`, ordered by that key. A source
        whose symbol has no matured outcome still appears with a `None` ratio:
        "cited but never measurable" is itself worth seeing.
    """
    by_symbol: dict[tuple[str, str], list[VerdictOutcomeRecord]] = defaultdict(list)
    for outcome in outcomes:
        by_symbol[(str(outcome.run_id), outcome.symbol)].append(outcome)

    grouped: dict[tuple[str, str], list[list[VerdictOutcomeRecord]]] = defaultdict(list)
    for citation in citations:
        provider = citation.source_id.split(":", 1)[0]
        grouped[(citation.source_type, provider)].append(
            by_symbol.get((str(citation.run_id), citation.symbol), [])
        )

    return tuple(
        _contribution_row(source_type, provider, cited)
        for (source_type, provider), cited in sorted(grouped.items())
    )


def _contribution_row(
    source_type: str, provider: str, cited: Sequence[Sequence[VerdictOutcomeRecord]]
) -> SourceContributionRow:
    linked = [outcome for citation_outcomes in cited for outcome in citation_outcomes]
    hits = sum(1 for outcome in linked if outcome.classification == HIT)
    neutrals = sum(1 for outcome in linked if outcome.classification == NEUTRAL)
    misses = len(linked) - hits - neutrals
    decided = hits + misses
    return SourceContributionRow(
        contribution_id=(
            f"{_METRIC_PREFIX}:source_contribution:{source_type}:{provider}"
        ),
        source_type=source_type,
        provider=provider,
        citation_count=len(cited),
        hit_citation_count=hits,
        miss_citation_count=misses,
        neutral_citation_count=neutrals,
        hit_citation_ratio=hits / decided if decided > 0 else None,
    )


def _metric_id(metric: str, horizon_days: int | None) -> str:
    suffix = _COMPOSED if horizon_days is None else f"{horizon_days}d"
    return f"{_METRIC_PREFIX}:{metric}:{suffix}"


def _horizon_weight(horizon_days: int | None, thresholds: PostmortemConfig) -> float:
    return (
        thresholds.horizon_5d_weight
        if horizon_days == HORIZON_DAYS[0]
        else thresholds.horizon_20d_weight
    )
