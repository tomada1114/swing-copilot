"""Build and atomically write `analysis_input.json` (FR-08).

This is the only thing the daily batch does for qualitative analysis: it hands
a skill everything it needs -- code-owned decision context plus the untrusted
news/filing text already collected in step 5 -- and stops. Nothing here calls a
model, so the step is cheap and always safe to run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from swing_copilot.analysis.context import (
    format_decision_history,
    format_market_regime,
    format_performance_summary,
    format_prior_verdicts,
    format_risk_constraints,
    format_score_breakdown,
)
from swing_copilot.analysis.filing_selection import select_filing_inputs
from swing_copilot.analysis.news_supply import (
    DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS,
    measure_news_supply,
)
from swing_copilot.analysis.schemas import (
    INPUT_SCHEMA_VERSION,
    AnalysisContextBlocks,
    AnalysisInput,
    CalendarEventInput,
    CandidateInput,
    FilingInput,
    NewsInput,
    canonical_json_digest,
)
from swing_copilot.io_atomic import write_json_atomically, write_text_atomically

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from uuid import UUID

    from swing_copilot.paper.journal import PerformanceSummary
    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.regime.gate import RegimeSnapshot
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate
    from swing_copilot.storage.paper_records import DecisionHistoryEntry
    from swing_copilot.storage.verdict_records import PriorVerdictRecord
    from swing_copilot.text.base import TextItem

# `write_json_atomically` / `write_text_atomically` now live in the
# dependency-zero `swing_copilot.io_atomic` (Issue #193) because atomic
# replacement is a repository-wide invariant rather than an `analysis`
# concern. They stay importable from here for the callers and design docs
# that have always named this module.
__all__ = [
    "ANALYSIS_INPUT_FILENAME",
    "ANALYSIS_RESULT_FILENAME",
    "ExportCandidate",
    "ExportRequest",
    "TextExportLimits",
    "build_analysis_input",
    "write_analysis_input",
    "write_json_atomically",
    "write_text_atomically",
]

ANALYSIS_INPUT_FILENAME = "analysis_input.json"
ANALYSIS_RESULT_FILENAME = "analysis_result.json"
_UNKNOWN_FORM_TYPE = "unknown"


@dataclass(frozen=True, slots=True)
class TextExportLimits:
    """Bounds on how much untrusted text is exported (per-symbol or run-wide).

    Mirrors `settings.analysis.*`. These bound the exported file's size (and
    therefore the reading cost on the skill side); the collection-time bounds
    that decide *which* filings exist at all live in `text/edgar_filings.py`.
    `max_calendar_*` bound `context.calendar_events`, which is run-wide rather
    than per-candidate.
    """

    max_news_items: int
    max_news_chars: int
    max_filing_chars: int
    max_filing_chars_per_symbol: int
    max_calendar_events: int
    max_calendar_chars: int
    #: `news_supply` grading threshold (Issue #191 made it configurable).
    #: Defaulted so existing callers that only bound sizes keep working.
    sufficient_news_mention_items: int = DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS


@dataclass(frozen=True, slots=True)
class ExportCandidate:
    """One candidate's deterministic context and its collected text items."""

    candidate: Candidate
    risk_assessment: RiskAssessment
    text_items: tuple[TextItem, ...]
    # Empty for dry-run/`--as-of` reruns: prior human decisions are only
    # injected for a live run of the current day (point-in-time invariant).
    decision_history: tuple[DecisionHistoryEntry, ...] = ()
    # The analysis layer's own earlier judgements on this symbol (Issue
    # #191), gated by the same point-in-time rule as `decision_history`.
    prior_verdicts: tuple[PriorVerdictRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ExportRequest:
    """Everything `build_analysis_input()` needs, grouped to stay under 3 args."""

    as_of: date
    run_id: UUID
    strategy_key: str
    generated_at: datetime
    regime_snapshot: RegimeSnapshot
    exposure_decision: ExposureDecision
    performance_summary: PerformanceSummary | None
    candidates: tuple[ExportCandidate, ...]
    limits: TextExportLimits
    # Run-wide macro/economic-calendar `TextItem`s (`symbol is None`), disjoint
    # from every `ExportCandidate.text_items`. Defaults to empty so callers
    # without a calendar source (or existing tests) need not pass it.
    calendar_events: tuple[TextItem, ...] = ()


def build_analysis_input(request: ExportRequest) -> AnalysisInput:
    """Assemble the strict `AnalysisInput` for one run.

    Args:
        request: Screening/risk/regime state plus this run's collected text.

    Returns:
        The validated analysis input. Candidates with no news and no filings
        are still included: their screening assessment and verdict are just as
        required as any other candidate's.
    """
    market_regime = format_market_regime(
        request.regime_snapshot, request.exposure_decision
    )
    performance = format_performance_summary(request.performance_summary)
    context = AnalysisContextBlocks(
        market_regime=market_regime or None,
        performance_summary=performance or None,
        calendar_events=_calendar_event_inputs(request.calendar_events, request.limits),
    )
    candidates = [_candidate_input(item, request.limits) for item in request.candidates]
    unsigned_payload: dict[str, object] = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "run_id": str(request.run_id),
        "as_of": request.as_of.isoformat(),
        "strategy_key": request.strategy_key,
        "generated_at": request.generated_at.isoformat(),
        "context": context.model_dump(mode="json"),
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
    }
    return AnalysisInput.model_validate(
        {
            **unsigned_payload,
            "input_digest": canonical_json_digest(
                unsigned_payload, excluded_field="input_digest"
            ),
        }
    )


def write_analysis_input(payload: AnalysisInput, output_dir: str | Path) -> Path:
    """Write `analysis_input.json` into `output_dir` via atomic replacement.

    Args:
        payload: The assembled analysis input.
        output_dir: The run's dated report directory.

    Returns:
        The resolved absolute path of the written file.

    Raises:
        OSError: Writing or replacing failed. The previous destination file is
            left untouched and the temporary artifact is removed.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / ANALYSIS_INPUT_FILENAME
    write_json_atomically(destination, payload.model_dump(mode="json"))
    return destination.resolve()


def _candidate_input(item: ExportCandidate, limits: TextExportLimits) -> CandidateInput:
    history = format_decision_history(item.decision_history)
    prior_verdicts = format_prior_verdicts(item.prior_verdicts)
    news = _news_inputs(item.text_items, limits, item.candidate.symbol)
    return CandidateInput(
        symbol=item.candidate.symbol,
        score_breakdown=format_score_breakdown(item.candidate),
        risk_constraints=format_risk_constraints(item.risk_assessment),
        decision_history=history or None,
        prior_verdicts=prior_verdicts or None,
        news=news,
        news_supply=measure_news_supply(
            item.candidate.symbol,
            item.text_items,
            news,
            limits.sufficient_news_mention_items,
        ),
        filings=_filing_inputs(item.text_items, limits),
    )


def _mentions_symbol(item: TextItem, symbol: str) -> bool:
    """Whether the source's own ticker list still covers `symbol` (FR-07).

    An item without ticker metadata counts as on-target: an empty
    `related_symbols` means the source did not declare one, and demoting every
    such article would penalize whole sources rather than off-target content.
    """
    if not item.related_symbols:
        return True
    return symbol.upper() in item.related_symbols


def _news_inputs(
    text_items: Sequence[TextItem], limits: TextExportLimits, symbol: str
) -> list[NewsInput]:
    """Newest-first news items, capped in count and per-item length.

    Articles whose source-declared tickers do not include `symbol` (sector
    round-ups, peer stories, generic market wraps) sort after every on-target
    article, so they stop crowding out material coverage. They are demoted, not
    dropped: a symbol with few on-target articles still fills its
    `max_news_items` budget instead of going empty.

    Within each relevance tier, items with a blank `summary` sort after every
    item that has one, so a summary-less article never displaces one with
    content. Remaining ties break on `published_at` then `source_id`, making
    the selection identical for identical input and `as_of`.
    """
    news = sorted(
        (item for item in text_items if item.source_type == "news"),
        key=lambda item: (
            _mentions_symbol(item, symbol),
            bool(item.content_text.strip()),
            item.published_at,
            item.source_id,
        ),
        reverse=True,
    )
    return [
        NewsInput(
            source_id=item.source_id,
            published_at=item.published_at,
            headline=item.title,
            summary=item.content_text[: limits.max_news_chars],
            url=item.source_url,
            provider=_provider(item.source_id),
        )
        for item in news[: limits.max_news_items]
    ]


def _filing_inputs(
    text_items: Sequence[TextItem], limits: TextExportLimits
) -> list[FilingInput]:
    """Newest-first filing excerpts under per-item and per-symbol budgets."""
    return select_filing_inputs(
        text_items,
        per_filing_chars=limits.max_filing_chars,
        per_symbol_chars=limits.max_filing_chars_per_symbol,
    )


def _calendar_event_inputs(
    calendar_items: Sequence[TextItem], limits: TextExportLimits
) -> list[CalendarEventInput]:
    """Newest-first calendar/macro events, capped in count and per-item length.

    Run-wide, not per-candidate: filtered defensively by `source_type` here,
    mirroring `_news_inputs()`/`_filing_inputs()`, even though callers are
    expected to pass only calendar-typed items.
    """
    events = sorted(
        (item for item in calendar_items if item.source_type == "calendar"),
        key=lambda item: (item.published_at, item.source_id),
        reverse=True,
    )
    return [
        CalendarEventInput(
            source_id=item.source_id,
            published_at=item.published_at,
            title=item.title,
            summary=item.content_text[: limits.max_calendar_chars],
            url=item.source_url,
            provider=_provider(item.source_id),
        )
        for item in events[: limits.max_calendar_events]
    ]


def form_type_of(title: str | None) -> str:
    """Extract the SEC form type from a filing `TextItem.title`.

    `data/edgar.py` writes titles as `"10-Q - <company> (<date>)"`, so the
    leading segment is the form type. Shared with `analysis/validate.py`, which
    resolves the same code-owned metadata back for the report heading.

    Args:
        title: The filing text item's title, possibly `None`.

    Returns:
        The form type, or `"unknown"` when the title is absent.
    """
    return (title or _UNKNOWN_FORM_TYPE).split(" - ")[0]


def _provider(source_id: str) -> str:
    """Return the collecting adapter's name, encoded as the source ID prefix."""
    return source_id.split(":", 1)[0]
