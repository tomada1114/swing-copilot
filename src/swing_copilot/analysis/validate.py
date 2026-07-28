"""Machine verification of skill-produced `analysis_result.json` (FR-08, CON-03).

Nothing a skill writes is trusted. Before any of it can reach a report this
module proves, per symbol:

1. the document parses under the strict schema (`schemas.py`);
2. every cited `source_id` was actually supplied for that symbol, and every
   fact cites at least one;
3. no user-visible text violates CON-03 (`safety.py`).

Rules 2 and 3 are enforced **fail-closed per symbol**: a violating symbol's
qualitative section is withheld and the failure is logged, with no retry. A
malformed document or an `as_of` that disagrees with the input is a hard
failure for the whole run -- there is no safe partial reading of a file that
may describe a different trading day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from swing_copilot.analysis.safety import ForbiddenLanguageError, check_display_texts
from swing_copilot.analysis.schemas import AnalysisInput, AnalysisResult
from swing_copilot.exceptions import SwingCopilotError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from datetime import date
    from pathlib import Path

    from swing_copilot.analysis.schemas import (
        CandidateInput,
        FilingAnalysis,
        NewsSummary,
        ScreeningAssessment,
        SymbolAnalysis,
        Verdict,
    )

logger = logging.getLogger(__name__)

#: Rendered in place of a symbol's qualitative section when verification fails.
WITHHELD_MESSAGE = "検証不合格のため非表示"


class AnalysisIngestError(SwingCopilotError):
    """Raised when an analysis document cannot be read at all (hard failure)."""


@dataclass(frozen=True, slots=True)
class ResolvedFiling:
    """One filing analysis joined back to its code-owned identifying metadata."""

    form_type: str
    filed_at: date
    analysis: FilingAnalysis


@dataclass(frozen=True, slots=True)
class SymbolOutcome:
    """One symbol's verification result.

    `error` is non-`None` exactly when verification failed; every analysis
    field is then empty, so a caller cannot accidentally render withheld
    content by forgetting to check the flag.
    """

    symbol: str
    news_summary: NewsSummary | None = None
    filings: tuple[ResolvedFiling, ...] = ()
    screening_assessment: ScreeningAssessment | None = None
    verdict: Verdict | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedAnalysis:
    """Every verified symbol outcome from one `analysis_result.json`."""

    as_of: date
    no_trade: bool
    no_trade_reason: str | None
    outcomes: tuple[SymbolOutcome, ...]
    #: `source_id` -> URL for every citable ID (per-symbol news/filings plus
    #: the run-wide calendar events), taken from the *input* file so the report
    #: never has to trust a skill-supplied link (and ingest never touches the
    #: database).
    source_urls: Mapping[str, str]

    def for_symbol(self, symbol: str) -> SymbolOutcome | None:
        """Return this symbol's outcome, or `None` if it was never analyzed."""
        return next(
            (outcome for outcome in self.outcomes if outcome.symbol == symbol), None
        )


def load_analysis_input(path: Path) -> AnalysisInput:
    """Read and strictly validate `analysis_input.json`.

    Args:
        path: Path to the exported analysis input.

    Returns:
        The parsed input.

    Raises:
        AnalysisIngestError: The file is missing, is not JSON, or violates the
            input schema.
    """
    return _load(path, AnalysisInput)


def load_analysis_result(path: Path) -> AnalysisResult:
    """Read and strictly validate `analysis_result.json`.

    Args:
        path: Path to the skill-produced analysis result.

    Returns:
        The parsed result.

    Raises:
        AnalysisIngestError: The file is missing, is not JSON, or violates the
            result schema (including unknown fields).
    """
    return _load(path, AnalysisResult)


def validate_analysis(
    analysis_input: AnalysisInput, result: AnalysisResult
) -> ValidatedAnalysis:
    """Verify `result` against the input it claims to answer.

    Args:
        analysis_input: The exported input the skill was given.
        result: The skill's parsed answer.

    Returns:
        Per-symbol outcomes, with failing symbols withheld rather than
        rendered, plus the run-level no-trade flag.

    Raises:
        AnalysisIngestError: `result.as_of` disagrees with the input's
            `as_of`, meaning the two documents describe different trading days.
    """
    if result.as_of != analysis_input.as_of:
        msg = (
            f"analysis_result as_of {result.as_of.isoformat()} does not match "
            f"analysis_input as_of {analysis_input.as_of.isoformat()}"
        )
        raise AnalysisIngestError(msg)

    candidates = {item.symbol: item for item in analysis_input.candidates}
    calendar_ids = frozenset(
        item.source_id for item in analysis_input.context.calendar_events
    )
    outcomes = tuple(
        _verify_symbol(analysis, candidates.get(analysis.symbol), calendar_ids)
        for analysis in result.symbols
    )
    return ValidatedAnalysis(
        as_of=result.as_of,
        no_trade=result.no_trade,
        no_trade_reason=_verified_no_trade_reason(result.no_trade_reason),
        outcomes=outcomes,
        source_urls=_source_urls(analysis_input),
    )


def _load[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"Analysis document could not be read: {path}"
        raise AnalysisIngestError(msg) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"Analysis document is not valid JSON: {path}"
        raise AnalysisIngestError(msg) from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        msg = f"Analysis document failed schema validation: {path}\n{exc}"
        raise AnalysisIngestError(msg) from exc


def _verify_symbol(
    analysis: SymbolAnalysis,
    candidate: CandidateInput | None,
    calendar_ids: frozenset[str],
) -> SymbolOutcome:
    """Apply the per-symbol provenance and CON-03 rules, fail-closed."""
    if candidate is None:
        return _withheld(analysis.symbol, "symbol is absent from analysis_input.json")
    error = _provenance_error(analysis, candidate, calendar_ids)
    if error is None:
        try:
            check_display_texts(_display_texts(analysis))
        except ForbiddenLanguageError as exc:
            error = f"CON-03 violation: {exc}"
    if error is not None:
        return _withheld(analysis.symbol, error)
    filings_by_id = {item.source_id: item for item in candidate.filings}
    return SymbolOutcome(
        symbol=analysis.symbol,
        news_summary=analysis.news_summary,
        filings=tuple(
            ResolvedFiling(
                form_type=filings_by_id[item.source_id].form_type,
                filed_at=filings_by_id[item.source_id].filed_at.date(),
                analysis=item,
            )
            for item in analysis.filing_analyses
        ),
        screening_assessment=analysis.screening_assessment,
        verdict=analysis.verdict,
    )


def _withheld(symbol: str, reason: str) -> SymbolOutcome:
    logger.warning(
        "analysis for %s withheld (no retry): %s",
        symbol,
        reason,
    )
    return SymbolOutcome(symbol=symbol, error=reason)


def _provenance_error(
    analysis: SymbolAnalysis,
    candidate: CandidateInput,
    calendar_ids: frozenset[str],
) -> str | None:
    """Return why provenance fails for this symbol, or `None` if it holds.

    `calendar_ids` (`analysis_input.context.calendar_events`) is run-wide, not
    per-symbol -- every symbol's analysis may cite any of them, unlike
    news/filing IDs which must belong to that symbol's own candidate.
    """
    known = (
        {item.source_id for item in candidate.news}
        | {item.source_id for item in candidate.filings}
        | calendar_ids
    )
    cited = set(_cited_source_ids(analysis))
    unknown = sorted(cited - known)
    if unknown:
        return f"cites source_ids absent from analysis_input.json: {unknown}"
    filing_ids = {item.source_id for item in candidate.filings}
    missing_filings = sorted(
        {item.source_id for item in analysis.filing_analyses} - filing_ids
    )
    if missing_filings:
        return f"analyzes filings absent from analysis_input.json: {missing_filings}"
    return None


def _cited_source_ids(analysis: SymbolAnalysis) -> Iterator[str]:
    if analysis.news_summary is not None:
        for fact in analysis.news_summary.facts:
            yield from fact.source_ids
    for filing in analysis.filing_analyses:
        for fact in filing.facts:
            yield from fact.source_ids
    for reason in analysis.verdict.reasons:
        yield from reason.source_ids


def _display_texts(analysis: SymbolAnalysis) -> Iterator[str]:
    """Every free-text field of this symbol that a report would render."""
    news = analysis.news_summary
    if news is not None:
        yield from (fact.text for fact in news.facts)
        yield from news.interpretation
        yield from news.risk_flags
    for filing in analysis.filing_analyses:
        yield from (fact.text for fact in filing.facts)
        yield from filing.interpretation
        yield from filing.red_flags
        yield from filing.yoy_changes
    assessment = analysis.screening_assessment
    yield assessment.summary
    yield from assessment.strengths
    yield from assessment.concerns
    yield from (reason.text for reason in analysis.verdict.reasons)


def _verified_no_trade_reason(reason: str | None) -> str | None:
    """Withhold a run-level no-trade reason that violates CON-03."""
    if reason is None:
        return None
    try:
        check_display_texts([reason])
    except ForbiddenLanguageError as exc:
        logger.warning("no_trade_reason withheld (no retry): CON-03 violation: %s", exc)
        return WITHHELD_MESSAGE
    return reason


def _source_urls(analysis_input: AnalysisInput) -> dict[str, str]:
    # Calendar events first: their IDs are citable by *every* symbol
    # (`_provenance_error`), so omitting them here would leave a legitimately
    # cited source rendering as a bare ID instead of a link.
    urls: dict[str, str] = {
        event.source_id: event.url for event in analysis_input.context.calendar_events
    }
    for candidate in analysis_input.candidates:
        for item in candidate.news:
            urls[item.source_id] = item.url
        for filing in candidate.filings:
            urls[filing.source_id] = filing.url
    return urls
