"""`copilot-retro collect`: archive past verdicts into DuckDB (P8-30).

A daily run's qualitative verdict only ever exists in
`reports/<date>/<run_id>/analysis_result.json`, which is gitignored and never
reaches the database -- `copilot-ingest-analysis` deliberately does not open a
connection. This module is the deferred ingestion path (decision D2): it walks
that directory tree and replaces each run's rows in `verdicts` /
`verdict_sources`, so a corrected re-export is picked up by simply re-running
the scan.

Two rules shape the error handling:

* Code-owned metadata is resolved from `analysis_input.json`, never echoed
  back from the skill's answer. `strategy_key` and every `source_type` come
  from the input side (design.md §4, E30.2).
* The scan is fail-soft per run and per source. An unusable run directory or
  an unresolvable `source_id` is recorded as a note and skipped, because one
  bad archive must not block the rest of the history (E30.2/E30.4). Scanning
  zero runs is a normal success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.analysis.validate import (
    AnalysisIngestError,
    load_analysis_input,
    load_analysis_result,
)
from swing_copilot.retro.adoption import adopt_one_run_per_date
from swing_copilot.storage.verdict_records import (
    AnalysisSourceCoverageRecord,
    VerdictReasonRecord,
    VerdictRecord,
    VerdictSourceRecord,
)

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.analysis.schemas import (
        AnalysisInput,
        AnalysisResult,
        FilingCoverage,
        SymbolAnalysis,
    )
    from swing_copilot.storage.state_store import StateStore

logger = logging.getLogger(__name__)

_NEWS = "news"
_FILING = "filing"
_CALENDAR = "calendar"


@dataclass(frozen=True, slots=True)
class RunDirectory:
    """One `reports/<run_date>/<run_id>/` archive located by the scan."""

    run_date: date
    run_id: UUID
    path: Path


@dataclass(frozen=True, slots=True)
class CollectSummary:
    """What one scan found, wrote, and had to skip."""

    scanned_run_count: int
    collected_run_count: int
    verdict_count: int
    source_count: int
    coverage_count: int
    notes: tuple[str, ...]


def collect_verdicts(state_store: StateStore, reports_root: Path) -> CollectSummary:
    """Scan `reports_root` and replace each archived run's verdict rows.

    Same-day duplicates (P8-119: more than one collectable run directory
    sharing a `run_date`, the "input" side of what #118 now guards at the
    door) are resolved before any write: only the run whose `runs.started_at`
    is latest is collected, and the rest are noted as skipped rather than
    written. A run this scan does not adopt is never touched -- its
    previously collected rows, if any, are left exactly as they were.

    Args:
        state_store: Write target for `verdicts` / `verdict_sources`.
        reports_root: The daily pipeline's output directory (`reports/`).
            A missing or empty directory yields an all-zero summary.

    Returns:
        Per-scan counts plus a note for every run or citation that was
        skipped. Notes are informational: a non-empty `notes` with a non-zero
        `collected_run_count` is a partially successful scan, not a failure.
    """
    notes: list[str] = []
    collected = verdict_count = source_count = coverage_count = 0
    run_directories = find_run_directories(reports_root)
    for run_directory, loaded in _adopted_runs(state_store, run_directories, notes):
        written = _write_run(state_store, run_directory, loaded, notes)
        collected += 1
        verdict_count += written[0]
        source_count += written[1]
        coverage_count += written[2]
    return CollectSummary(
        scanned_run_count=len(run_directories),
        collected_run_count=collected,
        verdict_count=verdict_count,
        source_count=source_count,
        coverage_count=coverage_count,
        notes=tuple(notes),
    )


@dataclass(frozen=True, slots=True)
class _LoadedRun:
    """One run directory's parsed documents, proven collectable."""

    analysis_input: AnalysisInput
    result: AnalysisResult


def _adopted_runs(
    state_store: StateStore,
    run_directories: tuple[RunDirectory, ...],
    notes: list[str],
) -> list[tuple[RunDirectory, _LoadedRun]]:
    """Return the one run directory to collect per `run_date` (P8-119).

    Collectability -- both documents exist, parse, and `result.run_id`
    matches the directory name -- is decided first and independently of
    deduplication, so a broken later rerun can never hide an earlier good
    run (design Example 2). Only among directories that clear that bar does
    a `run_date` with more than one candidate get narrowed to the single
    latest `runs.started_at` (ties broken on the greater `run_id` string,
    for determinism). A `run_date` with exactly one collectable candidate is
    always adopted, matching prior behavior even when its `started_at`
    cannot be resolved.
    """
    by_date: dict[date, list[tuple[RunDirectory, _LoadedRun]]] = {}
    for run_directory in run_directories:
        loaded = _load_collectable_run(run_directory, notes)
        if loaded is not None:
            by_date.setdefault(run_directory.run_date, []).append(
                (run_directory, loaded)
            )

    adopted: list[tuple[RunDirectory, _LoadedRun]] = []
    for run_date, candidates in by_date.items():
        if len(candidates) == 1:
            # REQ-007: a single collectable candidate is always adopted,
            # unconditionally on `started_at` -- there is nothing to dedupe
            # against, so resolving it would only add a note no prior
            # behavior ever produced.
            adopted.append(candidates[0])
            continue
        started_at_by_run_id: dict[UUID, datetime | None] = {}
        for run_directory, _loaded in candidates:
            started_at = state_store.get_run_started_at(run_directory.run_id)
            started_at_by_run_id[run_directory.run_id] = started_at
            if started_at is None:
                notes.append(
                    f"{run_date.isoformat()}: run {run_directory.run_id} の "
                    "started_at を解決できないため同日重複の判定を適用しない"
                )
        adopted_run_ids = adopt_one_run_per_date(
            ((run_date, run_directory.run_id) for run_directory, _ in candidates),
            started_at_by_run_id,
        )
        winner = next(
            candidate
            for candidate in candidates
            if candidate[0].run_id in adopted_run_ids
        )
        adopted.append(winner)
        for run_directory, _loaded in candidates:
            if run_directory is winner[0]:
                continue
            notes.append(
                f"{run_date.isoformat()}: run {run_directory.run_id} は同日の"
                f"重複のため収集をスキップ (採用: {winner[0].run_id})"
            )
    return adopted


def find_run_directories(reports_root: Path) -> tuple[RunDirectory, ...]:
    """Return every `<date>/<uuid>/` directory under `reports_root`, in order.

    Entries that do not parse as a date or a UUID are not run archives (the
    daily pipeline also writes per-run Markdown alongside them), so they are
    ignored silently rather than noted as skips.

    Public because `report/incomplete_runs.py` (Issue #129) walks the same
    archive tree to detect runs whose analysis phase never finished; both
    readers must agree on what counts as a run directory.

    Args:
        reports_root: The daily pipeline's output directory (`reports/`).
            A missing path yields an empty tuple rather than raising.

    Returns:
        Run archives ordered by `run_date`, then by `run_id` string.
    """
    if not reports_root.is_dir():
        return ()
    found: list[RunDirectory] = []
    for date_directory in sorted(reports_root.iterdir()):
        run_date = _parse_date(date_directory)
        if run_date is None:
            continue
        for run_directory in sorted(date_directory.iterdir()):
            run_id = _parse_uuid(run_directory)
            if run_id is None:
                continue
            found.append(
                RunDirectory(run_date=run_date, run_id=run_id, path=run_directory)
            )
    return tuple(found)


def _parse_date(candidate: Path) -> date | None:
    if not candidate.is_dir():
        return None
    try:
        return date.fromisoformat(candidate.name)
    except ValueError:
        return None


def _parse_uuid(candidate: Path) -> UUID | None:
    if not candidate.is_dir():
        return None
    try:
        return UUID(candidate.name)
    except ValueError:
        return None


def _load_collectable_run(
    run_directory: RunDirectory, notes: list[str]
) -> _LoadedRun | None:
    """Parse and validate one run directory; return `None` if unusable.

    `None` means the run was skipped fail-soft, with the reason appended to
    `notes`. This is "collectability" (P8-119 REQ-003): both documents exist,
    parse under their strict schemas, and `result.run_id` matches the
    directory name -- decided independently of, and before, same-day
    deduplication.
    """
    label = f"{run_directory.run_date.isoformat()}/{run_directory.run_id}"
    input_path = run_directory.path / ANALYSIS_INPUT_FILENAME
    result_path = run_directory.path / ANALYSIS_RESULT_FILENAME
    for path in (input_path, result_path):
        if not path.is_file():
            notes.append(f"{label}: {path.name} が見つからないためスキップ")
            return None

    try:
        analysis_input = load_analysis_input(input_path)
        result = load_analysis_result(result_path)
    except AnalysisIngestError:
        logger.exception("retro collect: %s の解析に失敗", label)
        notes.append(f"{label}: 解析文書を読めなかったためスキップ")
        return None

    if result.run_id != run_directory.run_id:
        notes.append(
            f"{label}: analysis_result.json の run_id "
            f"({result.run_id}) がディレクトリ名と一致しないためスキップ"
        )
        return None

    return _LoadedRun(analysis_input=analysis_input, result=result)


def _write_run(
    state_store: StateStore,
    run_directory: RunDirectory,
    loaded: _LoadedRun,
    notes: list[str],
) -> tuple[int, int, int]:
    """Replace one adopted run's rows; return `(verdicts, sources, coverages)`."""
    label = f"{run_directory.run_date.isoformat()}/{run_directory.run_id}"
    analysis_input, result = loaded.analysis_input, loaded.result
    source_types = _SourceTypeIndex(analysis_input)
    verdicts: list[VerdictRecord] = []
    sources: list[VerdictSourceRecord] = []
    coverages = [
        AnalysisSourceCoverageRecord(
            run_id=run_directory.run_id,
            symbol=candidate.symbol,
            source_id=filing.source_id,
            original_chars=coverage.original_chars,
            exported_chars=coverage.exported_chars,
            is_truncated=coverage.is_truncated,
            selection_mode=coverage.selection_mode,
            sections=tuple(
                (section.name, section.status) for section in coverage.sections
            ),
            exhibit_truncated=_recorded_exhibit_truncation(coverage),
        )
        for candidate in analysis_input.candidates
        for filing in candidate.filings
        if (coverage := filing.coverage) is not None
    ]
    for analysis in result.symbols:
        verdicts.append(
            VerdictRecord(
                run_id=run_directory.run_id,
                symbol=analysis.symbol,
                as_of=run_directory.run_date,
                strategy_key=analysis_input.strategy_key,
                recommendation=analysis.verdict.recommendation,
                reasons=tuple(
                    VerdictReasonRecord(
                        text=reason.text, source_ids=tuple(reason.source_ids)
                    )
                    for reason in analysis.verdict.reasons
                ),
                no_trade=result.no_trade,
            )
        )
        sources.extend(
            _resolve_sources(
                analysis,
                source_types,
                run_id=run_directory.run_id,
                label=label,
                notes=notes,
            )
        )

    state_store.replace_run_verdicts(run_directory.run_id, verdicts, sources, coverages)
    return len(verdicts), len(sources), len(coverages)


def _recorded_exhibit_truncation(coverage: FilingCoverage) -> bool | None:
    """Return the archived exhibit-truncation signal, or `None` if absent.

    `FilingCoverage.exhibit_truncated` defaults to `False`, so the parsed model
    cannot tell an archive that measured "no marker" apart from one written
    before the field existed. `model_fields_set` can, and the distinction is
    the point of the field (Issue #157): persisting an old archive's default
    as `False` would let the retrospective count that run's input as known to
    be complete. Only what the document actually stated is stored.
    """
    if "exhibit_truncated" not in coverage.model_fields_set:
        return None
    return coverage.exhibit_truncated


class _SourceTypeIndex:
    """Resolves a cited `source_id` to its code-owned `source_type`.

    News and filing IDs are scoped to the candidate they were exported for;
    calendar events are run-wide, so any symbol's analysis may cite them
    (the same admission rule `analysis/validate.py` applies to provenance).
    """

    def __init__(self, analysis_input: AnalysisInput) -> None:
        self._per_symbol = {
            candidate.symbol: {
                **{item.source_id: _NEWS for item in candidate.news},
                **{item.source_id: _FILING for item in candidate.filings},
            }
            for candidate in analysis_input.candidates
        }
        self._calendar = {
            item.source_id: _CALENDAR for item in analysis_input.context.calendar_events
        }

    def resolve(self, symbol: str, source_id: str) -> str | None:
        """Return the source's type, or `None` if the input never supplied it."""
        per_symbol = self._per_symbol.get(symbol, {})
        return per_symbol.get(source_id) or self._calendar.get(source_id)


def _cited_source_ids(analysis: SymbolAnalysis) -> tuple[str, ...]:
    """Return every `source_id` this symbol's analysis referenced, deduplicated.

    Facts, filing analyses, and verdict reasons are unioned (design §4): the
    contribution table asks which sources informed *the judgement*, not which
    section happened to name them.
    """
    cited: list[str] = []
    if analysis.news_summary is not None:
        for fact in analysis.news_summary.facts:
            cited.extend(fact.source_ids)
    for filing in analysis.filing_analyses:
        cited.append(filing.source_id)
        for fact in filing.facts:
            cited.extend(fact.source_ids)
    for reason in analysis.verdict.reasons:
        cited.extend(reason.source_ids)
    return tuple(dict.fromkeys(cited))


def _resolve_sources(
    analysis: SymbolAnalysis,
    source_types: _SourceTypeIndex,
    *,
    run_id: UUID,
    label: str,
    notes: list[str],
) -> list[VerdictSourceRecord]:
    """Map one symbol's citations to rows, dropping the unresolvable ones."""
    resolved: list[VerdictSourceRecord] = []
    for source_id in _cited_source_ids(analysis):
        source_type = source_types.resolve(analysis.symbol, source_id)
        if source_type is None:
            notes.append(
                f"{label}: {analysis.symbol} が引用した source_id "
                f"{source_id!r} は analysis_input.json に無いため除外"
            )
            continue
        resolved.append(
            VerdictSourceRecord(
                run_id=run_id,
                symbol=analysis.symbol,
                source_id=source_id,
                source_type=source_type,
            )
        )
    return resolved
