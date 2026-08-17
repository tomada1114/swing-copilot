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

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.analysis.news_supply import (
    DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS,
)
from swing_copilot.retro.evaluate import (
    HIT,
    HORIZON_DAYS,
    MISS_SEVERE,
    NEUTRAL,
    PROCEED,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.config import PostmortemConfig
    from swing_copilot.storage.verdict_records import (
        VerdictCitationRow,
        VerdictDecisionRow,
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

_SEPARATION = "separation"
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
    """

    metric_id: str
    horizon_days: int | None
    value: float | None
    sample_size: int
    is_preliminary: bool


@dataclass(frozen=True, slots=True)
class RateMetricSummary:
    """A rate metric plus the same-period baseline it is judged against."""

    metric_id: str
    horizon_days: int | None
    value: float | None
    baseline_value: float | None
    is_flagged: bool
    sample_size: int
    is_preliminary: bool


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


@dataclass(frozen=True, slots=True)
class AlignmentCell:
    """One `(decision, recommendation, horizon)` cell of the human cross-tab."""

    cell_id: str
    decision: str
    recommendation: str
    horizon_days: int
    count: int
    mean_forward_return_pct: float
    hit_count: int
    severe_miss_count: int


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
    return MetricSummary(
        metric_id=_metric_id(_SEPARATION, horizon_days),
        horizon_days=horizon_days,
        value=value,
        sample_size=len(rows),
        is_preliminary=len(rows) < thresholds.preliminary_sample_threshold,
    )


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
    return RateMetricSummary(
        metric_id=metric_id,
        horizon_days=horizon_days,
        value=value,
        baseline_value=baseline,
        is_flagged=is_flagged(value, baseline),
        sample_size=part.sample_size,
        is_preliminary=part.sample_size < thresholds.preliminary_sample_threshold,
    )


def _flag_severe(value: float | None, baseline: float | None) -> bool:
    if value is None:
        return False
    return value > PROCEED_SEVERE_MISS_WATCH_RATE or (
        baseline is not None and value > baseline
    )


def _flag_below_baseline(value: float | None, baseline: float | None) -> bool:
    return value is not None and baseline is not None and value < baseline


def compute_human_alignment(
    rows: Sequence[VerdictDecisionRow],
) -> tuple[AlignmentCell, ...]:
    """Cross-tab the journal's decision against the verdict and what happened.

    Args:
        rows: `trades_journal` x `verdicts` x `verdict_outcomes` rows (E31.5).

    Returns:
        One cell per `(decision, recommendation, horizon)` seen, ordered
        deterministically. Empty when nothing was journaled -- a user who
        never records decisions simply has nothing to cross-tabulate, which
        is not an error.
    """
    grouped: dict[tuple[str, str, int], list[VerdictDecisionRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.decision, row.recommendation, row.horizon_days)].append(row)

    return tuple(
        AlignmentCell(
            cell_id=(
                f"{_METRIC_PREFIX}:human_alignment:"
                f"{decision}:{recommendation}:{horizon_days}d"
            ),
            decision=decision,
            recommendation=recommendation,
            horizon_days=horizon_days,
            count=len(cell),
            mean_forward_return_pct=(
                sum(row.forward_return_pct for row in cell) / len(cell)
            ),
            hit_count=sum(1 for row in cell if row.classification == HIT),
            severe_miss_count=sum(
                1 for row in cell if row.classification == MISS_SEVERE
            ),
        )
        for (decision, recommendation, horizon_days), cell in sorted(grouped.items())
    )


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
