"""`copilot-retro export`: assemble `retro_input.json` (P8-31, design §5.3).

The retrospective's evidence dossier. Everything quantitative is already in
DuckDB by the time this runs (`collect` wrote the verdicts, `evaluate`
classified them), so the export's job is to aggregate, select the surprises
worth re-reading, fetch what the text adapters say about them *now*, and
write one strict document the skill can read without touching the database.

Boundaries this module keeps:

* Only `date <= as_of` data is read. Every query is windowed on the
  retrospective's own cutoff, and the freshness fetch never asks for anything
  later than it either.
* Wall time enters exactly once, as `generated_at` from the injected `Clock`.
  It is provenance, never a cutoff.
* The write is atomic (`write_json_atomically`): a failure mid-write leaves
  the previous export intact.
* Nothing here changes configuration or code. The document reports the config
  it observed; applying a change is the skill's job, via a PR (design §10).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from swing_copilot.analysis.filing_selection import MIN_FILING_CHARS
from swing_copilot.analysis.schemas import (
    FilingCoverage,
    FilingSectionCoverage,
    FilingSectionStatus,
    FilingSelectionMode,
    NewsSupply,
    NewsSupplyLevel,
)
from swing_copilot.config import config_snapshot_hash, config_snapshot_sections
from swing_copilot.io_atomic import write_json_atomically
from swing_copilot.pipeline.postmortem import compute_signal_performance
from swing_copilot.retro.adoption import keep_adopted_rows
from swing_copilot.retro.aggregate import (
    PROCEED_SEVERE_MISS_WATCH_RATE,
    compute_basis_contribution,
    compute_human_alignment,
    compute_news_supply_mix,
    compute_proceed_severe_miss_rate,
    compute_separation,
    compute_separation_paired,
    compute_separation_paired_excess,
    compute_skip_hit_rate,
    compute_source_contribution,
    compute_tracked_performance,
    compute_verdict_mix,
)
from swing_copilot.retro.evaluate import MISS_SEVERE
from swing_copilot.retro.ledger import read_ledger
from swing_copilot.retro.schemas import (
    RETRO_INPUT_SCHEMA_VERSION,
    AggregateMetrics,
    AlignmentEntry,
    ArchivedFilingCoverage,
    BasisContributionEntry,
    ConfigSnapshot,
    ConfigVersionAggregateEntry,
    EvaluationSettings,
    ExitReasonCountEntry,
    FailureClassCountEntry,
    FailureClassHistoryEntry,
    FreshnessEntry,
    InputCoverageSummary,
    MetricEntry,
    NewsSupplyCellEntry,
    NewsSupplySummaryEntry,
    ProposalsLedger,
    RateMetricEntry,
    RetroInput,
    SignalPerformanceEntry,
    SourceContributionEntry,
    SurpriseBundle,
    SurpriseDossier,
    SurpriseOutcomeEntry,
    TrackedPerformanceEntry,
    VerdictMixEntry,
    VerdictReasonEntry,
    retro_input_digest,
)
from swing_copilot.retro.surprises import (
    FreshnessSources,
    fetch_freshness,
    select_surprises,
)
from swing_copilot.storage.history_queries import get_signal_outcomes

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date
    from pathlib import Path
    from uuid import UUID

    from swing_copilot.clock import Clock
    from swing_copilot.config import PostmortemConfig, Settings
    from swing_copilot.report.daily_brief import SignalPerformanceRow
    from swing_copilot.retro.aggregate import TrackedPerformance
    from swing_copilot.retro.schemas import FailureClass
    from swing_copilot.retro.surprises import SurpriseCandidate
    from swing_copilot.storage.config_records import ConfigVersionRecord
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore
    from swing_copilot.storage.tracking_records import (
        VerdictPosition,
        VerdictPositionMark,
    )
    from swing_copilot.storage.verdict_records import (
        AnalysisSourceCoverageRecord,
        VerdictCitationRow,
        VerdictOutcomeRecord,
        VerdictRecord,
        VerdictRow,
    )

logger = logging.getLogger(__name__)

RETRO_INPUT_FILENAME = "retro_input.json"
#: `reports/retro/<as_of>/`, beside the daily run archives but never mistaken
#: for one: `collect`'s scan only descends into date-named directories.
RETRO_OUTPUT_SUBDIR = "retro"
DEFAULT_LEDGER_PATH = "docs/retro/proposals.md"

#: Design §8.1's L2 qualitative gate: "the same `failure_class` five times
#: across the last three retrospectives". Constants rather than settings on
#: purpose -- the gate is a proposal rule (`references/proposal-rules.md`),
#: and letting the configuration move it would let a proposal lower the bar
#: it has to clear.
L2_GATE_SESSION_WINDOW = 3
L2_GATE_MIN_COUNT = 5


@dataclass(frozen=True, slots=True)
class RetroExportDependencies:
    """Collaborators the export composes: two stores, settings, clock, adapters."""

    market_store: MarketStore
    state_store: StateStore
    settings: Settings
    clock: Clock
    #: Absent adapters mean no freshness, not a failure (see `FreshnessBundle`).
    freshness: FreshnessSources = field(default_factory=FreshnessSources)


@dataclass(frozen=True, slots=True)
class RetroExportRequest:
    """One export's point-in-time cutoff and where its outputs live."""

    as_of: date
    #: The daily pipeline's output root; the dossier lands in its `retro/`
    #: subdirectory so one `--reports-dir` covers scanning and writing.
    reports_root: Path
    ledger_path: Path


@dataclass(frozen=True, slots=True)
class ExportSummary:
    """What one export wrote, for the CLI to report."""

    path: Path
    digest: str
    outcome_count: int
    surprise_count: int
    dropped_surprise_count: int
    notes: tuple[str, ...]


def evaluated_row_count(document: RetroInput) -> int:
    """Rows behind the window, read off separation's weight-composed entry.

    That entry counts every classification in the window (both horizons,
    both recommendations), which is exactly what the CLI reports as "評価".
    Shared with `retro/ingest.py`, which records the same number on the
    session row so a later reader knows how much evidence a retrospective had.
    """
    return next(
        (
            row.sample_size
            for row in document.aggregates.separation
            if row.horizon_days is None
        ),
        0,
    )


def retro_output_dir(reports_root: Path, as_of: date) -> Path:
    """Return `reports/retro/<as_of>/`, the directory one retrospective owns."""
    return reports_root / RETRO_OUTPUT_SUBDIR / as_of.isoformat()


def export_retro_input(
    deps: RetroExportDependencies, request: RetroExportRequest
) -> ExportSummary:
    """Build the dossier and replace `retro_input.json` atomically.

    Args:
        deps: Stores, settings, clock, and the optional text adapters.
        request: The retrospective's `as_of` and output locations.

    Returns:
        Where the document landed, its digest, and per-run counts.

    Raises:
        RetroIngestError: The proposal ledger exists but could not be read.
            Nothing is written in that case.
        ValidationError: `build_retro_input` refused to hand over a document
            (Issue #292's readback assertion, or the construction validate
            above it). Nothing is written in that case either. This is a
            schema-drift signal aimed at development, not an operator-facing
            failure mode, so it stays outside `_INGEST_EXIT`'s conversion --
            the same place the pre-existing construction `ValidationError`
            has always sat.
        OSError: Writing failed. The previous export is left untouched.
    """
    document = build_retro_input(deps, request)
    destination = write_retro_input(
        document, retro_output_dir(request.reports_root, request.as_of)
    )
    return ExportSummary(
        path=destination,
        digest=document.input_digest,
        outcome_count=evaluated_row_count(document),
        surprise_count=len(document.surprises.items),
        dropped_surprise_count=document.surprises.dropped_count,
        notes=tuple(document.notes),
    )


def write_retro_input(document: RetroInput, output_dir: Path) -> Path:
    """Write the dossier into `output_dir` via atomic replacement.

    Args:
        document: The validated dossier.
        output_dir: `reports/retro/<as_of>/`, created if absent.

    Returns:
        The resolved absolute path of the written file.

    Raises:
        OSError: Writing or replacing failed. The previous destination file is
            left untouched and the temporary artifact is removed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / RETRO_INPUT_FILENAME
    write_json_atomically(destination, document.model_dump(mode="json"))
    return destination.resolve()


def build_retro_input(
    deps: RetroExportDependencies, request: RetroExportRequest
) -> RetroInput:
    """Assemble the dossier for the window ending at `request.as_of`.

    The window is `settings.postmortem.lookback_window_days` back from
    `as_of`, matched against each classification's *maturity* date, so the
    same window means the same rows no matter which day the retrospective
    runs (decision D7).

    Args:
        deps: Stores, settings, clock, and the optional text adapters.
        request: The retrospective's `as_of` and output locations.

    Returns:
        A validated `retro-input-v1` document, digest included. An empty
        database yields a valid document whose metrics are all `None` -- the
        normal state before enough verdicts have matured.

    Raises:
        ValidationError: The document does not survive the round trip through
            the file's own bytes -- see the assertion at the end of this
            function (Issue #292). Nothing is written in that case.
    """
    thresholds = deps.settings.postmortem
    store = deps.state_store
    as_of = request.as_of
    window_start = as_of - timedelta(days=thresholds.lookback_window_days)

    # P8-124: `collect` leaves a non-adopted same-day run's rows in place, so
    # the window read has to re-apply its rule or the day is counted twice.
    verdicts = keep_adopted_rows(
        store.get_verdicts_in_window(window_start, as_of), store
    )
    outcomes = keep_adopted_rows(
        store.get_verdict_outcomes_in_window(window_start, as_of), store
    )
    coverages = store.get_analysis_source_coverages_in_window(window_start, as_of)
    citations = store.get_verdict_citations_in_window(window_start, as_of)
    reason_bases = store.get_verdict_reason_bases_in_window(window_start, as_of)
    alignment = store.get_verdict_decision_alignment(window_start, as_of)
    signals = compute_signal_performance(
        get_signal_outcomes(store.database, window_start, as_of), thresholds
    )

    selection = select_surprises(outcomes, deps.settings.retro.max_surprises)
    notes: list[str] = []
    if selection.selected and _has_no_text_adapter(deps.freshness):
        notes.append("鮮度データ: text アダプタが未設定のため全銘柄の鮮度欄は空になる")
    dossiers = [
        dossier
        for candidate in selection.selected
        if (dossier := _dossier(deps, request, candidate, citations, notes)) is not None
    ]

    unsigned: dict[str, object] = {
        "schema_version": RETRO_INPUT_SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "generated_at": deps.clock.now().isoformat(),
        "window_start": window_start.isoformat(),
        "evaluation": _evaluation_settings(deps.settings).model_dump(mode="json"),
        "aggregates": _aggregates(
            verdicts,
            outcomes,
            deps.settings,
            _tracked_ledger_window(store, window_start, as_of),
        ).model_dump(mode="json"),
        "signal_performance": _signal_entries(signals),
        "human_alignment": [
            AlignmentEntry(**asdict(cell)).model_dump(mode="json")
            for cell in compute_human_alignment(alignment)
        ],
        "source_contribution": [
            SourceContributionEntry(**asdict(row)).model_dump(mode="json")
            for row in compute_source_contribution(citations, outcomes)
        ],
        "basis_contribution": [
            BasisContributionEntry(**asdict(row)).model_dump(mode="json")
            for row in compute_basis_contribution(reason_bases, outcomes)
        ],
        "input_coverage": _input_coverage_summary(outcomes, coverages).model_dump(
            mode="json"
        ),
        "failure_class_history": _failure_class_history(store, as_of),
        "aggregates_by_config": _aggregates_by_config(
            _config_ledger_window(store, as_of), outcomes, thresholds
        ),
        "surprises": SurpriseBundle(
            max_surprises=deps.settings.retro.max_surprises,
            dropped_count=selection.dropped_count,
            items=dossiers,
        ).model_dump(mode="json"),
        "config_snapshot": _config_snapshot(deps.settings).model_dump(mode="json"),
        "proposals_ledger": read_proposals_ledger(request.ledger_path).model_dump(
            mode="json"
        ),
        "notes": notes,
    }
    document = RetroInput.model_validate(
        {
            **unsigned,
            "input_digest": retro_input_digest(unsigned),
        }
    )
    _assert_readable_back(document)
    return document


def _assert_readable_back(document: RetroInput) -> None:
    """State the invariant here: what gets written can be read again.

    `write_retro_input` writes `model_dump(mode="json")` -- every default
    materialized -- while the digest above is signed over the hand-built
    `unsigned` dict. Since Issue #289 the verification hashes the parsed
    document's `fields_set` (`exclude_unset`), so a top-level field added to
    `RetroInput` but forgotten in `unsigned` no longer disturbs construction:
    the export would succeed and leave a dossier whose file carries the
    materialized field, whose `fields_set` on re-read therefore differs from
    the signed one, and whose digest can never verify again (Issue #292).

    Re-validating the full-key dump -- the exact bytes the file will hold --
    turns that "writable but unreadable forever" dossier back into a failure
    at write time. It deliberately tightens only this side: the reader's drop
    rules are untouched, so `exclude_unset` never becomes a way to make an
    unreadable file readable.

    Raises:
        ValidationError: The written form does not parse back, digest
            included.
    """
    RetroInput.model_validate(document.model_dump(mode="json"))


def _signal_entries(
    signals: Sequence[SignalPerformanceRow],
) -> list[dict[str, object]]:
    """Carry P2-11's rows across verbatim, field for field.

    Spelled out rather than `asdict`-ed because `SignalPerformanceRow` belongs
    to the report layer: a field added there should fail here loudly instead
    of silently widening this contract.
    """
    return [
        SignalPerformanceEntry(
            signal_name=row.signal_name,
            true_positive_count=row.true_positive_count,
            false_positive_count=row.false_positive_count,
            neutral_count=row.neutral_count,
            hit_rate=row.hit_rate,
            n=row.n,
            is_preliminary=row.is_preliminary,
        ).model_dump(mode="json")
        for row in signals
    ]


def _has_no_text_adapter(sources: FreshnessSources) -> bool:
    return sources.news_client is None and sources.edgar_client is None


def _evaluation_settings(settings: Settings) -> EvaluationSettings:
    thresholds = settings.postmortem
    return EvaluationSettings(
        horizon_5d_weight=thresholds.horizon_5d_weight,
        horizon_20d_weight=thresholds.horizon_20d_weight,
        neutral_threshold_pct=thresholds.neutral_threshold_pct,
        severe_threshold_pct=thresholds.severe_threshold_pct,
        preliminary_sample_threshold=thresholds.preliminary_sample_threshold,
        lookback_window_days=thresholds.lookback_window_days,
        proceed_severe_miss_watch_rate=PROCEED_SEVERE_MISS_WATCH_RATE,
    )


@dataclass(frozen=True, slots=True)
class TrackedLedgerWindow:
    """The shadow-tracking ledger's slice of one retrospective window (#190).

    Two reads bundled into one value: the positions themselves and each one's
    entry-session mark, which is the only place the stop actually in force at
    entry survives (the position row's `stop_price` ratchets with the trailing
    stop). Carried together so `_aggregates` stays inside the project's
    parameter-count guideline.
    """

    positions: tuple[VerdictPosition, ...]
    initial_marks: Mapping[tuple[UUID, str], VerdictPositionMark]


def _tracked_ledger_window(
    store: StateStore, window_start: date, as_of: date
) -> TrackedLedgerWindow:
    """Read the ledger slice this window's performance row describes."""
    return TrackedLedgerWindow(
        positions=_tracked_positions_in_window(store, window_start, as_of),
        initial_marks=store.get_earliest_verdict_position_marks(),
    )


def _tracked_positions_in_window(
    store: StateStore, window_start: date, as_of: date
) -> tuple[VerdictPosition, ...]:
    """Select the shadow positions this window's performance row describes.

    A closed position belongs to the window when it *realized* inside it,
    which is the same "matured in this period" rule `verdict_outcomes` uses,
    so the two aggregate blocks describe the same stretch of time. Open
    positions are included when they were entered by `as_of`: they contribute
    only to `open_count`, and reporting them is what stops a window of nothing
    but unrealized positions from looking like a window of no activity.
    """
    return tuple(
        position
        for position in store.get_verdict_positions()
        if position.entry_date <= as_of
        and (position.exit_date is None or window_start <= position.exit_date <= as_of)
    )


def _aggregates(
    verdicts: Sequence[VerdictRow],
    outcomes: Sequence[VerdictOutcomeRecord],
    settings: Settings,
    tracked: TrackedLedgerWindow,
) -> AggregateMetrics:
    thresholds = settings.postmortem
    return AggregateMetrics(
        separation=[
            MetricEntry(**asdict(row))
            for row in compute_separation(outcomes, thresholds)
        ],
        separation_paired=[
            MetricEntry(**asdict(row))
            for row in compute_separation_paired(outcomes, thresholds)
        ],
        separation_paired_excess=[
            MetricEntry(**asdict(row))
            for row in compute_separation_paired_excess(outcomes, thresholds)
        ],
        tracked_performance=[
            _tracked_performance_entry(row)
            for row in compute_tracked_performance(
                tracked.positions, tracked.initial_marks
            )
        ],
        proceed_severe_miss_rate=[
            RateMetricEntry(**asdict(row))
            for row in compute_proceed_severe_miss_rate(outcomes, thresholds)
        ],
        skip_hit_rate=[
            RateMetricEntry(**asdict(row))
            for row in compute_skip_hit_rate(outcomes, thresholds)
        ],
        verdict_mix=VerdictMixEntry(**asdict(compute_verdict_mix(verdicts))),
        news_supply=_news_supply_entry(
            verdicts, settings.analysis.sufficient_news_mention_items
        ),
    )


def _failure_class_history(store: StateStore, as_of: date) -> dict[str, object] | None:
    """Cross-tab the trailing retrospectives' failure classes (Issue #189).

    Returns:
        The gate block, or `None` when no retrospective has been ingested at
        or before `as_of` -- which is also the shape every dossier written
        before `retro_sessions` existed carries, so its digest is unchanged.
    """
    history = store.get_failure_class_history(as_of, L2_GATE_SESSION_WINDOW)
    if not history.sessions:
        return None
    return FailureClassHistoryEntry(
        gate_window_sessions=L2_GATE_SESSION_WINDOW,
        gate_min_count=L2_GATE_MIN_COUNT,
        sessions=list(history.sessions),
        counts=[
            FailureClassCountEntry(
                count_id=f"failure_class_{row.failure_class}",
                failure_class=cast("FailureClass", row.failure_class),
                count=row.count,
                session_count=row.session_count,
                meets_l2_gate=row.count >= L2_GATE_MIN_COUNT,
            )
            for row in history.counts
        ],
    ).model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class ConfigLedgerWindow:
    """Which configuration each evaluated run executed under (Issue #189).

    Keyed by `run_id` rather than filtered by `run_date`: an outcome matures
    5 or 20 sessions *after* the run it judges, so the runs behind one
    retrospective window almost never fall inside that window's own dates.
    """

    run_configs: Mapping[UUID, str]
    versions: Mapping[str, ConfigVersionRecord]


def _config_ledger_window(store: StateStore, as_of: date) -> ConfigLedgerWindow:
    """Read the config ledger and the run-to-config map, as of the cutoff."""
    return ConfigLedgerWindow(
        run_configs=store.get_run_config_hashes(as_of),
        versions={row.config_hash: row for row in store.get_config_versions()},
    )


def _aggregates_by_config(
    ledger: ConfigLedgerWindow,
    outcomes: Sequence[VerdictOutcomeRecord],
    thresholds: PostmortemConfig,
) -> list[dict[str, object]]:
    """Split the window's separation by the configuration each run used.

    A run whose `config_hash` is unknown (its `runs` row was pruned) is
    dropped rather than pooled into an "unknown" bucket: pooling would put
    outcomes produced under different settings into one number, which is the
    exact confusion this block exists to remove.

    Args:
        ledger: The run-to-config map and the ledger rows behind it.
        outcomes: The window's classified outcomes.
        thresholds: The postmortem thresholds separation is computed under.

    Returns:
        One entry per configuration, ordered by `config_hash` so the dossier
        is byte-reproducible.
    """
    grouped: dict[str, list[VerdictOutcomeRecord]] = {}
    for outcome in outcomes:
        config_hash = ledger.run_configs.get(outcome.run_id)
        if config_hash is not None:
            grouped.setdefault(config_hash, []).append(outcome)
    return [
        _config_aggregate_entry(
            config_hash, ledger.versions.get(config_hash), rows, thresholds
        ).model_dump(mode="json")
        for config_hash, rows in sorted(grouped.items())
    ]


def _config_aggregate_entry(
    config_hash: str,
    version: ConfigVersionRecord | None,
    rows: Sequence[VerdictOutcomeRecord],
    thresholds: PostmortemConfig,
) -> ConfigVersionAggregateEntry:
    """Build one configuration's slice, with `@`-suffixed metric IDs.

    The suffix keeps the per-config entries citable without colliding with
    the window-wide `aggregates.separation` IDs, which name a different
    population.
    """
    return ConfigVersionAggregateEntry(
        config_hash=config_hash,
        snapshot_hash=None if version is None else version.snapshot_hash,
        first_seen_run_date=None if version is None else version.first_seen_run_date,
        run_count=len({row.run_id for row in rows}),
        outcome_count=len(rows),
        separation=[
            MetricEntry(
                **{**asdict(row), "metric_id": f"{row.metric_id}@{config_hash}"}
            )
            for row in compute_separation(rows, thresholds)
        ],
    )


def _tracked_performance_entry(row: TrackedPerformance) -> TrackedPerformanceEntry:
    """Carry one stratum across, field for field (Issue #190).

    Spelled out rather than `asdict`-ed for the same reason as
    `_signal_entries`: a field added to the aggregate should fail here loudly
    instead of silently widening the exported contract.
    """
    return TrackedPerformanceEntry(
        metric_id=row.metric_id,
        recommendation=row.recommendation,
        closed_count=row.closed_count,
        open_count=row.open_count,
        win_rate=row.win_rate,
        profit_factor=row.profit_factor,
        expectancy_pct=row.expectancy_pct,
        avg_r_multiple=row.avg_r_multiple,
        avg_holding_days=row.avg_holding_days,
        exit_reason_counts=[
            ExitReasonCountEntry(reason=cell.reason, count=cell.count)
            for cell in row.exit_reason_counts
        ],
    )


def _news_supply_entry(
    verdicts: Sequence[VerdictRow], sufficient_mention_items: int
) -> NewsSupplySummaryEntry:
    """Carry the supply cross-tab across, cell by cell (Issue #154).

    The threshold is the operator's configured one (Issue #191), so a dossier
    re-read later is graded against the value the run actually used rather
    than whatever the code's default has since become.
    """
    summary = compute_news_supply_mix(verdicts, sufficient_mention_items)
    return NewsSupplySummaryEntry(
        metric_id=summary.metric_id,
        sufficient_threshold=summary.sufficient_threshold,
        verdict_count=summary.verdict_count,
        recorded_verdict_count=summary.recorded_verdict_count,
        unrecorded_verdict_count=summary.unrecorded_verdict_count,
        cells=[NewsSupplyCellEntry(**asdict(cell)) for cell in summary.cells],
    )


def _dossier(
    deps: RetroExportDependencies,
    request: RetroExportRequest,
    candidate: SurpriseCandidate,
    citations: Sequence[VerdictCitationRow],
    notes: list[str],
) -> SurpriseDossier | None:
    """Build one surprise's evidence packet, or `None` if its verdict is gone."""
    verdict = _find_verdict(deps.state_store, candidate.run_id, candidate.symbol)
    if verdict is None:
        notes.append(
            f"{candidate.symbol}: {candidate.run_id} の verdict 行が無いため"
            "サプライズ dossier を作成できず除外"
        )
        return None

    freshness, fetch_notes = fetch_freshness(
        deps.freshness,
        candidate.symbol,
        since=verdict.as_of,
        as_of=request.as_of,
        limits=deps.settings.analysis,
    )
    notes.extend(fetch_notes)
    return SurpriseDossier(
        surprise_id=candidate.surprise_id,
        run_id=candidate.run_id,
        symbol=candidate.symbol,
        run_as_of=verdict.as_of,
        strategy_key=verdict.strategy_key,
        recommendation=verdict.recommendation,
        no_trade=verdict.no_trade,
        reasons=[
            VerdictReasonEntry(text=reason.text, source_ids=list(reason.source_ids))
            for reason in verdict.reasons
        ],
        cited_source_ids=[
            row.source_id
            for row in citations
            if row.run_id == candidate.run_id and row.symbol == candidate.symbol
        ],
        outcomes=[
            SurpriseOutcomeEntry(
                horizon_days=row.horizon_days,
                maturity_as_of=row.as_of,
                forward_return_pct=row.forward_return_pct,
                classification=row.classification,
            )
            for row in candidate.outcomes
        ],
        max_adverse_return_pct=_max_adverse_return(
            deps.market_store,
            candidate.symbol,
            verdict.as_of,
            max(row.as_of for row in candidate.outcomes),
        ),
        input_filing_coverage=[
            ArchivedFilingCoverage(
                source_id=coverage.source_id,
                coverage=_filing_coverage(coverage),
            )
            for coverage in deps.state_store.get_analysis_source_coverages(
                candidate.run_id, candidate.symbol
            )
        ],
        news_supply=_archived_news_supply(verdict),
        freshness=FreshnessEntry(
            news=list(freshness.news),
            filings=list(freshness.filings),
            fetch_failed=freshness.fetch_failed,
        ),
    )


def _archived_news_supply(verdict: VerdictRecord) -> NewsSupply | None:
    """Rebuild the supply block this verdict was made under, if it was measured.

    Re-validated through `NewsSupply` rather than passed as a bare row so the
    dossier's copy is held to the same internal consistency the export
    originally wrote it under (`exported <= collected`, `none` iff zero
    mentions). A stored row that cannot satisfy that is a corrupted archive,
    not something to publish into the evidence dossier.
    """
    supply = verdict.news_supply
    if supply is None:
        return None
    return NewsSupply(
        collected_items=supply.collected_items,
        exported_items=supply.exported_items,
        symbol_mention_items=supply.symbol_mention_items,
        level=cast("NewsSupplyLevel", supply.level),
    )


def _input_coverage_summary(
    outcomes: Sequence[VerdictOutcomeRecord],
    coverages: Sequence[AnalysisSourceCoverageRecord],
) -> InputCoverageSummary:
    """Count severe misses by whether their original filing input had a gap.

    A gap is either export-stage (`is_truncated`) or collection-stage
    (`exhibit_truncated`, Issue #157). Counting only the first put a filing
    whose 8-K exhibits were cut off before export into `without_gap`, which
    positively told the retrospective that the input had been complete --
    the misreading this field exists to prevent, one layer down.

    `without_gap` therefore requires every one of the symbol's rows to *know*
    it has no exhibit gap. A row written before the column existed carries
    `None`, which is "not recorded"; such a symbol falls through to the
    `unknown` remainder rather than being claimed as complete.

    The per-mode tallies alongside it say how a filing was cut;
    `starved_filing_count` says whether what survived was worth calling an
    input at all (`_is_starved`, Issue #267).
    """
    severe_keys = {
        (outcome.run_id, outcome.symbol)
        for outcome in outcomes
        if outcome.classification == MISS_SEVERE
    }
    by_key: dict[tuple[UUID, str], list[AnalysisSourceCoverageRecord]] = {}
    for coverage in coverages:
        by_key.setdefault((coverage.run_id, coverage.symbol), []).append(coverage)
    with_gap = {
        key
        for key in severe_keys
        if key in by_key and any(_has_gap(row) for row in by_key[key])
    }
    without_gap = {
        key
        for key in severe_keys
        if key in by_key
        and all(
            not _has_gap(row) and row.exhibit_truncated is not None
            for row in by_key[key]
        )
    }
    return InputCoverageSummary(
        filing_count=len(coverages),
        truncated_filing_count=sum(row.is_truncated for row in coverages),
        exhibit_truncated_filing_count=sum(
            row.exhibit_truncated is True for row in coverages
        ),
        fallback_filing_count=sum(
            row.selection_mode == "head_fallback" for row in coverages
        ),
        omitted_filing_count=sum(
            row.selection_mode == "omitted_symbol_budget" for row in coverages
        ),
        starved_filing_count=sum(_is_starved(row) for row in coverages),
        severe_miss_symbol_count_with_gap=len(with_gap),
        severe_miss_symbol_count_without_gap=len(without_gap),
        severe_miss_symbol_count_unknown=len(severe_keys - with_gap - without_gap),
    )


def _has_gap(record: AnalysisSourceCoverageRecord) -> bool:
    """Whether this filing row reports a known gap, at either stage."""
    return record.is_truncated or record.exhibit_truncated is True


def _is_starved(record: AnalysisSourceCoverageRecord) -> bool:
    """Whether this filing reached the analysis context too small to be read.

    Measured in characters rather than by `selection_mode`, because the mode
    does not carry the size. `omitted_symbol_budget` used to be a workable
    proxy -- a starved filing was one that got nothing at all -- but Issue
    #255 gave every filing a reserved minimum, so the same starvation now
    exports as a `head_fallback` (or a `section_priority_partial`) of roughly
    `MIN_FILING_CHARS`, which that check does not see. Reading the count keeps
    the detector honest across all five modes and across any later change to
    how the budget is divided.

    Two conditions, and the second is what keeps it from crying wolf:

    * `exported_chars <= MIN_FILING_CHARS` -- the filing got no more than the
      floor Issue #255 guarantees, out of a 240,000-character per-symbol
      ceiling. Exactly the floor counts: a reservation is what a filing is
      handed when there was nothing left to share, not an adequate read.
    * `exported_chars < original_chars` -- something was actually left behind.
      A short 8-K (the observed ones ran 4,074 and 6,670 characters) exported
      whole is complete input, not a degraded one, however few characters it
      is; calling that starved would fire on the routine case and drown the
      real one.

    The pair leaves one honest edge: a filing barely longer than the floor and
    cut to it is counted, though little was lost. It *was* allocated nothing
    but its reservation, which is the condition being counted, and the
    alternative -- a second ratio threshold -- buys that edge with a constant
    nothing else justifies.

    This adds no case to `_has_gap`: `exported_chars < original_chars` is how
    `is_truncated` is computed at export, so a starved row is already a gap.
    """
    return (
        record.exported_chars <= MIN_FILING_CHARS
        and record.exported_chars < record.original_chars
    )


def _filing_coverage(record: AnalysisSourceCoverageRecord) -> FilingCoverage:
    """Rebuild strict coverage metadata from its normalized DB row.

    Faithful to what the row holds, which is less than the exported document
    held: sections keep only name/status. A row written before
    `exhibit_truncated` became a column carries `None`, which collapses to the
    schema's "not recorded" `False` -- the same reading `FilingCoverage`
    documents for that default, and the reason `_input_coverage_summary`
    counts such a symbol as `unknown` rather than as gap-free (Issue #157).
    """
    return FilingCoverage(
        original_chars=record.original_chars,
        exported_chars=record.exported_chars,
        is_truncated=record.is_truncated,
        selection_mode=cast("FilingSelectionMode", record.selection_mode),
        exhibit_truncated=bool(record.exhibit_truncated),
        sections=[
            FilingSectionCoverage(name=name, status=cast("FilingSectionStatus", status))
            for name, status in record.sections
        ],
    )


def _find_verdict(store: StateStore, run_id: UUID, symbol: str) -> VerdictRecord | None:
    """Return the archived verdict for one symbol of one run, if still present.

    `verdict_outcomes` can outlive its `verdicts` row when a re-`collect`
    picked up a corrected result that no longer analyzes the symbol. That is a
    fail-soft skip with a note, not an export-wide failure.
    """
    return next(
        (row for row in store.get_run_verdicts(run_id) if row.symbol == symbol), None
    )


def _max_adverse_return(
    market_store: MarketStore, symbol: str, run_date: date, through: date
) -> float | None:
    """Return the worst close-to-close drawdown from the run's close, in percent.

    Close-based, like `compute_forward_return`, so the dossier's drawdown and
    its horizon returns are the same kind of number; an intraday low would be
    a second, quietly incompatible measure of "how bad did it get".

    Returns:
        `None` when the run's own close or every later close is missing --
        a data-quality gap, reported as unknown rather than as zero.
    """
    bars = market_store.read_bars([symbol], run_date, through, through)
    if bars.empty:
        return None
    run_rows = bars[bars["date"] == run_date]
    later = bars[bars["date"] > run_date]
    if run_rows.empty or later.empty:
        return None
    run_close = float(run_rows.iloc[0]["close"])
    if run_close == 0:
        return None
    return (float(later["close"].min()) - run_close) / run_close * 100


def _config_snapshot(settings: Settings) -> ConfigSnapshot:
    """Snapshot the proposal-relevant settings and hash them.

    Both halves come from `config.py` (Issue #189) so this dossier's snapshot
    and the `config_versions` row `pipeline/daily_runner.py` writes are
    provably the same eight sections hashed the same way.
    """
    sections = config_snapshot_sections(settings)
    return ConfigSnapshot(sections=sections, config_hash=config_snapshot_hash(sections))


def read_proposals_ledger(path: Path) -> ProposalsLedger:
    """Read the proposal ledger's closed RP-IDs, tolerating its absence.

    Parsing lives in `retro/ledger.py`, which `ingest` also uses: the IDs this
    dossier reports as closed and the keys the re-proposal guard blocks have to
    come from one reading of one file, or the guard the skill sees and the
    guard ingest enforces could disagree.

    Args:
        path: Ledger location, typically `docs/retro/proposals.md`.

    Returns:
        The reference the dossier carries: the path, whether it exists, and
        the sorted RP-IDs a re-proposal must justify reopening.
    """
    state = read_ledger(path)
    return ProposalsLedger(
        path=str(path),
        exists=state.exists,
        rejected_proposal_ids=sorted(state.closed_rp_ids()),
    )
