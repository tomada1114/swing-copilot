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

from swing_copilot.analysis.export import write_json_atomically
from swing_copilot.analysis.schemas import (
    FilingCoverage,
    FilingSectionCoverage,
    FilingSectionStatus,
    FilingSelectionMode,
    NewsSupply,
    NewsSupplyLevel,
    canonical_json_digest,
)
from swing_copilot.pipeline.postmortem import compute_signal_performance
from swing_copilot.retro.adoption import keep_adopted_rows
from swing_copilot.retro.aggregate import (
    PROCEED_SEVERE_MISS_WATCH_RATE,
    compute_human_alignment,
    compute_news_supply_mix,
    compute_proceed_severe_miss_rate,
    compute_separation,
    compute_skip_hit_rate,
    compute_source_contribution,
    compute_verdict_mix,
)
from swing_copilot.retro.evaluate import MISS_SEVERE
from swing_copilot.retro.ledger import read_ledger
from swing_copilot.retro.schemas import (
    RETRO_INPUT_SCHEMA_VERSION,
    AggregateMetrics,
    AlignmentEntry,
    ArchivedFilingCoverage,
    ConfigSnapshot,
    EvaluationSettings,
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
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path
    from uuid import UUID

    from swing_copilot.clock import Clock
    from swing_copilot.config import Settings
    from swing_copilot.report.daily_brief import SignalPerformanceRow
    from swing_copilot.retro.surprises import SurpriseCandidate
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore
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

#: Settings a retrospective proposal could plausibly target. Delivery and
#: scheduling plumbing (`notification`, `schedule`) and the universe source
#: are excluded: they are not analysis parameters, and a snapshot that
#: includes everything makes `config_hash` churn for unrelated edits.
_SNAPSHOT_SECTIONS = (
    "risk",
    "fundamental_filters",
    "technical_signals",
    "backtest",
    "analysis",
    "postmortem",
    "regime",
    "retro",
)


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


def _evaluated_row_count(document: RetroInput) -> int:
    """Rows behind the window, read off separation's weight-composed entry.

    That entry counts every classification in the window (both horizons,
    both recommendations), which is exactly what the CLI reports as "評価".
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
        OSError: Writing failed. The previous export is left untouched.
    """
    document = build_retro_input(deps, request)
    destination = write_retro_input(
        document, retro_output_dir(request.reports_root, request.as_of)
    )
    return ExportSummary(
        path=destination,
        digest=document.input_digest,
        outcome_count=_evaluated_row_count(document),
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
        "aggregates": _aggregates(verdicts, outcomes, deps.settings).model_dump(
            mode="json"
        ),
        "signal_performance": _signal_entries(signals),
        "human_alignment": [
            AlignmentEntry(**asdict(cell)).model_dump(mode="json")
            for cell in compute_human_alignment(alignment)
        ],
        "source_contribution": [
            SourceContributionEntry(**asdict(row)).model_dump(mode="json")
            for row in compute_source_contribution(citations, outcomes)
        ],
        "input_coverage": _input_coverage_summary(outcomes, coverages).model_dump(
            mode="json"
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
    return RetroInput.model_validate(
        {
            **unsigned,
            "input_digest": retro_input_digest(unsigned),
        }
    )


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


def _aggregates(
    verdicts: Sequence[VerdictRow],
    outcomes: Sequence[VerdictOutcomeRecord],
    settings: Settings,
) -> AggregateMetrics:
    thresholds = settings.postmortem
    return AggregateMetrics(
        separation=[
            MetricEntry(**asdict(row))
            for row in compute_separation(outcomes, thresholds)
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
        news_supply=_news_supply_entry(verdicts),
    )


def _news_supply_entry(verdicts: Sequence[VerdictRow]) -> NewsSupplySummaryEntry:
    """Carry the supply cross-tab across, cell by cell (Issue #154)."""
    summary = compute_news_supply_mix(verdicts)
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
        severe_miss_symbol_count_with_gap=len(with_gap),
        severe_miss_symbol_count_without_gap=len(without_gap),
        severe_miss_symbol_count_unknown=len(severe_keys - with_gap - without_gap),
    )


def _has_gap(record: AnalysisSourceCoverageRecord) -> bool:
    """Whether this filing row reports a known gap, at either stage."""
    return record.is_truncated or record.exhibit_truncated is True


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
    """Snapshot the proposal-relevant settings and hash them."""
    dumped = settings.model_dump(mode="json")
    sections = {name: dumped[name] for name in _SNAPSHOT_SECTIONS}
    return ConfigSnapshot(
        sections=sections,
        config_hash=canonical_json_digest(sections, excluded_field="config_hash"),
    )


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
