"""Build and atomically write `analysis_input.json` (FR-08).

This is the only thing the daily batch does for qualitative analysis: it hands
a skill everything it needs -- code-owned decision context plus the untrusted
news/filing text already collected in step 5 -- and stops. Nothing here calls a
model, so the step is cheap and always safe to run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from swing_copilot.analysis.context import (
    format_decision_history,
    format_market_regime,
    format_performance_summary,
    format_risk_constraints,
    format_score_breakdown,
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
    from swing_copilot.text.base import TextItem

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
    max_calendar_events: int
    max_calendar_chars: int


@dataclass(frozen=True, slots=True)
class ExportCandidate:
    """One candidate's deterministic context and its collected text items."""

    candidate: Candidate
    risk_assessment: RiskAssessment
    text_items: tuple[TextItem, ...]
    # Empty for dry-run/`--as-of` reruns: prior human decisions are only
    # injected for a live run of the current day (point-in-time invariant).
    decision_history: tuple[DecisionHistoryEntry, ...] = ()


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


def write_json_atomically(destination: Path, payload: object) -> None:
    """Replace `destination` with `payload` as JSON, all-or-nothing.

    Uses a temporary file in the destination's own directory plus
    `os.replace`, so a failure mid-write preserves the previous destination
    and leaves no temporary artifact behind.

    Args:
        destination: Final path to (re)write.
        payload: Any JSON-serializable object.

    Raises:
        OSError: Serialization/write/replace failed.
    """
    tmp_path = destination.with_name(f".{destination.name}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, destination)  # noqa: PTH105 - atomic replace by design
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _candidate_input(item: ExportCandidate, limits: TextExportLimits) -> CandidateInput:
    history = format_decision_history(item.decision_history)
    return CandidateInput(
        symbol=item.candidate.symbol,
        score_breakdown=format_score_breakdown(item.candidate),
        risk_constraints=format_risk_constraints(item.risk_assessment),
        decision_history=history or None,
        news=_news_inputs(item.text_items, limits),
        filings=_filing_inputs(item.text_items, limits),
    )


def _news_inputs(
    text_items: Sequence[TextItem], limits: TextExportLimits
) -> list[NewsInput]:
    """Newest-first news items, capped in count and per-item length."""
    news = sorted(
        (item for item in text_items if item.source_type == "news"),
        key=lambda item: (item.published_at, item.source_id),
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
    """Newest-first filing excerpts, each truncated to the export char budget."""
    filings = sorted(
        (item for item in text_items if item.source_type == "filing"),
        key=lambda item: (item.published_at, item.source_id),
        reverse=True,
    )
    return [
        FilingInput(
            source_id=item.source_id,
            form_type=form_type_of(item.title),
            filed_at=item.published_at,
            text=item.content_text[: limits.max_filing_chars],
            url=item.source_url,
        )
        for item in filings
    ]


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
