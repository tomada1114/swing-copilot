"""Machine verification of skill-produced `analysis_result.json` (FR-08, CON-03).

Nothing a skill writes is trusted. Before any of it can reach a report this
module proves, per symbol:

1. the document parses under the strict schema (`schemas.py`);
2. every cited `source_id` was actually supplied for that symbol, and every
   fact cites at least one;
3. every fact's `evidence_quote` occurs verbatim in a cited source's exported
   body (`evidence.py`), so a fact written from a slice the expert was never
   given fails even though its IDs are correct;
4. no user-visible text violates CON-03 (`safety.py`).

Rules 2 to 4 are enforced **fail-closed per symbol**: a violating symbol's
qualitative section is withheld and the failure is logged, with no retry. A
malformed document or an `as_of` that disagrees with the input is a hard
failure for the whole run -- there is no safe partial reading of a file that
may describe a different trading day.

A fifth check is deliberately *not* fail-closed. A correct quote can still be
restated with the wrong digits (Issue #131), which rule 3 cannot see, so
`numeric_consistency.py` compares the figures on both sides and this module
logs a warning naming them. It stays a warning because the comparison spans
unit systems the input never states: a false positive must cost a second look
by the reviewer, never a withheld analysis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from swing_copilot.analysis.evidence import (
    normalize_evidence_text,
    normalized_source_bodies,
)
from swing_copilot.analysis.numeric_consistency import unsupported_magnitudes
from swing_copilot.analysis.safety import ForbiddenLanguageError, check_display_texts
from swing_copilot.analysis.schemas import (
    RESULT_SCHEMA_VERSION,
    AnalysisInput,
    AnalysisResult,
)
from swing_copilot.documents import read_json_document
from swing_copilot.exceptions import SwingCopilotError

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping
    from datetime import date
    from pathlib import Path
    from uuid import UUID

    from swing_copilot.analysis.schemas import (
        CandidateInput,
        FilingAnalysis,
        NewsSummary,
        ScreeningAssessment,
        SourcedFact,
        SymbolAnalysis,
        Verdict,
    )

logger = logging.getLogger(__name__)

_ALLOWED_SOURCE_URL_SCHEMES = frozenset({"http", "https"})

#: Rendered in place of a symbol's qualitative section when verification fails.
WITHHELD_MESSAGE = "検証不合格のため非表示"


class AnalysisIngestError(SwingCopilotError):
    """Raised when an analysis document cannot be read at all (hard failure)."""


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """Identity fields read from the report-context envelope."""

    run_id: UUID
    as_of: date
    strategy_key: str
    input_digest: str


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
        AnalysisIngestError: The result's schema version, `as_of`, or symbol set
            disagrees with the input, so the documents cannot safely describe
            one analysis.
    """
    if result.schema_version != RESULT_SCHEMA_VERSION:
        msg = (
            f"analysis_result schema_version {result.schema_version} cannot be "
            f"ingested: {RESULT_SCHEMA_VERSION} is required so every fact "
            "carries a verifiable evidence_quote"
        )
        raise AnalysisIngestError(msg)
    if result.as_of != analysis_input.as_of:
        msg = (
            f"analysis_result as_of {result.as_of.isoformat()} does not match "
            f"analysis_input as_of {analysis_input.as_of.isoformat()}"
        )
        raise AnalysisIngestError(msg)

    _verify_complete_symbol_coverage(analysis_input, result)
    candidates = {item.symbol: item for item in analysis_input.candidates}
    calendar_bodies = calendar_source_bodies(analysis_input)
    outcomes = tuple(
        verify_symbol_analysis(
            analysis, candidates.get(analysis.symbol), calendar_bodies
        )
        for analysis in result.symbols
    )
    return ValidatedAnalysis(
        as_of=result.as_of,
        no_trade=result.no_trade,
        no_trade_reason=_verified_no_trade_reason(result.no_trade_reason),
        outcomes=outcomes,
        source_urls=_source_urls(analysis_input),
    )


def validate_artifact_identity(
    analysis_input: AnalysisInput,
    result: AnalysisResult,
    context: ArtifactIdentity,
) -> None:
    """Hard-fail when the three artifacts do not describe one exact run.

    This is intentionally separate from per-symbol provenance/safety checks:
    an identity mismatch makes the entire report unsafe to rewrite, whereas a
    bad symbol can be withheld without affecting siblings.
    """
    checks = (
        ("analysis_result run_id", result.run_id, analysis_input.run_id),
        ("report_context run_id", context.run_id, analysis_input.run_id),
        ("analysis_result as_of", result.as_of, analysis_input.as_of),
        ("report_context as_of", context.as_of, analysis_input.as_of),
        (
            "analysis_result strategy_key",
            result.strategy_key,
            analysis_input.strategy_key,
        ),
        (
            "report_context strategy_key",
            context.strategy_key,
            analysis_input.strategy_key,
        ),
        (
            "analysis_result input_digest",
            result.input_digest,
            analysis_input.input_digest,
        ),
        (
            "report_context input_digest",
            context.input_digest,
            analysis_input.input_digest,
        ),
    )
    for document_field, actual, expected in checks:
        if actual != expected:
            msg = f"{document_field} {actual!s} does not match analysis_input"
            raise AnalysisIngestError(msg)


def calendar_source_bodies(analysis_input: AnalysisInput) -> dict[str, str]:
    """Return the run-wide calendar `source_id` -> normalized quotable body.

    Calendar events are not tied to a candidate, so every symbol may cite them.
    Exposed because callers that verify one symbol at a time -- notably the
    fragment checker behind `copilot-verify-analysis` -- need exactly the map
    `validate_analysis` builds, rather than an approximation of it.

    Args:
        analysis_input: The exported input whose events are citable run-wide.

    Returns:
        Normalized bodies ready for a containment check against a quote passed
        through `normalize_evidence_text`.
    """
    return normalized_source_bodies(
        (item.source_id, _text_body(item.title, item.summary))
        for item in analysis_input.context.calendar_events
    )


def read_analysis_document(path: Path) -> object:
    """Read one analysis document off disk and return its parsed JSON.

    Exposed because `copilot-verify-analysis` reads the same documents one step
    earlier, to dispatch on their top-level keys, and its pre-flight verdict
    must not disagree with ingest about which files are readable.

    Args:
        path: The document to read.

    Returns:
        The decoded JSON value, of whatever type the document holds.

    Raises:
        AnalysisIngestError: The file could not be read, decoded as UTF-8, or
            parsed as JSON.
    """
    return read_json_document(
        path, label="Analysis document", error_type=AnalysisIngestError
    )


def _load[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    payload = read_analysis_document(path)
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        msg = f"Analysis document failed schema validation: {path}\n{exc}"
        raise AnalysisIngestError(msg) from exc


def verify_symbol_analysis(
    analysis: SymbolAnalysis,
    candidate: CandidateInput | None,
    calendar_bodies: Mapping[str, str],
) -> SymbolOutcome:
    """Apply the per-symbol provenance, evidence, and CON-03 rules, fail-closed.

    This is the single implementation of "does one symbol's qualitative section
    satisfy the contract". `validate_analysis` calls it once per symbol at
    ingest, and `analysis/fragment.py` calls it on one `analysis_work/`
    fragment before that fragment is ever merged, so an expert's pre-flight
    check cannot be weaker than the check that will actually gate the report.

    Args:
        analysis: This symbol's section of the skill's answer.
        candidate: The exported candidate it claims to answer, or `None` when
            the input never offered this symbol.
        calendar_bodies: Run-wide calendar `source_id` -> normalized body, the
            IDs of which every symbol may cite (`calendar_source_bodies`).

    Returns:
        The symbol's outcome, whose `error` is non-`None` exactly when the
        section must be withheld.
    """
    if candidate is None:
        return _withheld(analysis.symbol, "symbol is absent from analysis_input.json")
    error = _provenance_error(analysis, candidate, calendar_bodies.keys())
    if error is None:
        error = _evidence_error(analysis, candidate, calendar_bodies)
    if error is None:
        try:
            check_display_texts(_display_texts(analysis))
        except ForbiddenLanguageError as exc:
            error = f"CON-03 violation: {exc}"
    if error is not None:
        return _withheld(analysis.symbol, error)
    _log_numeric_disagreements(analysis)
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


def _verify_complete_symbol_coverage(
    analysis_input: AnalysisInput, result: AnalysisResult
) -> None:
    """Require the result to explicitly cover every exported candidate.

    A missing analysis is indistinguishable from an accidental omission at the
    trust boundary, so partial results are intentionally not supported.
    """
    candidate_symbols = frozenset(
        candidate.symbol for candidate in analysis_input.candidates
    )
    result_symbols = frozenset(symbol.symbol for symbol in result.symbols)
    if candidate_symbols == result_symbols:
        return
    missing = sorted(candidate_symbols - result_symbols)
    unexpected = sorted(result_symbols - candidate_symbols)
    msg = (
        "analysis_result symbols must exactly match analysis_input candidates: "
        f"missing={missing}, unexpected={unexpected}"
    )
    raise AnalysisIngestError(msg)


def _provenance_error(
    analysis: SymbolAnalysis,
    candidate: CandidateInput,
    calendar_ids: Collection[str],
) -> str | None:
    """Return why provenance fails for this symbol, or `None` if it holds.

    `calendar_ids` (`analysis_input.context.calendar_events`) is run-wide, not
    per-symbol -- every symbol's analysis may cite any of them, unlike
    news/filing IDs which must belong to that symbol's own candidate.
    """
    known = (
        {item.source_id for item in candidate.news}
        | {item.source_id for item in candidate.filings}
        | set(calendar_ids)
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


def _evidence_error(
    analysis: SymbolAnalysis,
    candidate: CandidateInput,
    calendar_bodies: Mapping[str, str],
) -> str | None:
    """Return why a fact's verbatim quote is unsupported, or `None` if all hold.

    Only `SourcedFact` carries a quote. `VerdictReason` is deliberately exempt:
    a reason may rest solely on deterministic values the code computed, so it is
    allowed to cite nothing and has no source body to quote from.

    Call this only after `_provenance_error` has passed, so every cited ID is
    known to have a body here. An unknown ID would fall back to an empty body
    and fail closed anyway.
    """
    bodies = dict(calendar_bodies) | normalized_source_bodies(
        _candidate_source_bodies(candidate)
    )
    for fact in _sourced_facts(analysis):
        if fact.evidence_quote is None:
            return f"fact carries no evidence_quote: {fact.text!r}"
        quote = normalize_evidence_text(fact.evidence_quote)
        if not any(quote in bodies.get(source_id, "") for source_id in fact.source_ids):
            return (
                "evidence_quote is absent from every cited source body "
                f"{sorted(fact.source_ids)}: {fact.evidence_quote!r}"
            )
    return None


def _log_numeric_disagreements(analysis: SymbolAnalysis) -> None:
    """Warn about a fact whose figures its own quote cannot account for.

    Unlike the checks above this one never withholds. `numeric_consistency.py`
    reconciles across unit systems the input does not state, so the reviewer --
    not the pipeline -- decides what an unexplained figure means. Call it only
    once a symbol has passed the fail-closed rules: a withheld symbol renders
    nothing, so warning about its digits would only add noise.
    """
    for fact in _sourced_facts(analysis):
        # `_evidence_error` has already proven every quote is present and
        # occurs in a cited body; the fallback only satisfies the optional type.
        unsupported = unsupported_magnitudes(fact.text, fact.evidence_quote or "")
        if unsupported:
            logger.warning(
                "analysis for %s states figures its evidence_quote does not "
                "account for (still rendered; verify the unit conversion by "
                "hand): %s in %r, quoting %r",
                analysis.symbol,
                ", ".join(unsupported),
                fact.text,
                fact.evidence_quote,
            )


def _candidate_source_bodies(candidate: CandidateInput) -> Iterator[tuple[str, str]]:
    """Yield `(source_id, quotable body)` for everything exported to a symbol."""
    for item in candidate.news:
        yield item.source_id, _text_body(item.headline, item.summary)
    for filing in candidate.filings:
        yield filing.source_id, filing.text


def _text_body(title: str | None, summary: str) -> str:
    """Join an optional headline/title to its summary as one quotable body."""
    return summary if title is None else f"{title}\n{summary}"


def _sourced_facts(analysis: SymbolAnalysis) -> Iterator[SourcedFact]:
    """Every `SourcedFact` this symbol asserts, across news and filings."""
    if analysis.news_summary is not None:
        yield from analysis.news_summary.facts
    for filing in analysis.filing_analyses:
        yield from filing.facts


def _cited_source_ids(analysis: SymbolAnalysis) -> Iterator[str]:
    for fact in _sourced_facts(analysis):
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
    # (`_provenance_error`). Only safe URLs are retained; renderer attribution
    # is omitted when the archived input has no linkable URL.
    urls: dict[str, str] = {}
    for event in analysis_input.context.calendar_events:
        _add_safe_source_url(urls, event.source_id, event.url)
    for candidate in analysis_input.candidates:
        for item in candidate.news:
            _add_safe_source_url(urls, item.source_id, item.url)
        for filing in candidate.filings:
            _add_safe_source_url(urls, filing.source_id, filing.url)
    return urls


def _add_safe_source_url(urls: dict[str, str], source_id: str, raw_url: str) -> None:
    """Add only a web URL that is safe for the report renderer to link."""
    url = raw_url.strip()
    parsed = urlsplit(url)
    if parsed.scheme.lower() in _ALLOWED_SOURCE_URL_SCHEMES and parsed.hostname:
        urls[source_id] = url
