"""Strict JSON schemas for the pipeline <-> skill analysis contract (FR-08, CON-03).

`analysis_input.json` is produced by `copilot-daily` and read by the
`swing-daily` skill; `analysis_result.json` is produced by the skill and read
by `copilot-ingest-analysis`. Both sides are validated here with
`extra="forbid"`, so a renamed or invented field fails loudly instead of being
silently dropped.

Provenance is structural, not advisory: every `SourcedFact` must cite at least
one input `source_id`, and `validate.py` additionally proves those IDs were
actually supplied for that symbol. Separating `facts` from `interpretation`
does not by itself prevent an unsupported claim -- the source IDs are what make
a rendered statement traceable.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

INPUT_SCHEMA_VERSION: Final[Literal["analysis-input-v1"]] = "analysis-input-v1"
RESULT_SCHEMA_VERSION: Final[Literal["analysis-result-v1"]] = "analysis-result-v1"

SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _StrictModel(BaseModel):
    """Base for both directions of the contract: reject unknown fields."""

    model_config = ConfigDict(extra="forbid")


class NewsInput(_StrictModel):
    """One collected news item offered to the analysis skill."""

    source_id: SourceId
    published_at: datetime
    headline: str | None
    summary: str
    url: str
    provider: str


class FilingInput(_StrictModel):
    """One collected filing excerpt offered to the analysis skill."""

    source_id: SourceId
    form_type: str
    filed_at: datetime
    text: str
    url: str


class CandidateInput(_StrictModel):
    """One screened candidate's deterministic context plus its untrusted text.

    `score_breakdown`/`risk_constraints`/`decision_history` are pre-rendered
    text blocks produced by `analysis/context.py` from values the code already
    computed. They exist so the skill's narrative can be checked against the
    code's own quantitative determination, never so the skill can restate or
    override it.
    """

    symbol: str
    score_breakdown: str
    risk_constraints: str
    decision_history: str | None
    news: list[NewsInput]
    filings: list[FilingInput]


class CalendarEventInput(_StrictModel):
    """One collected macro/economic-calendar event, not tied to any symbol.

    Unlike `NewsInput`/`FilingInput`, this is run-wide context (`TextItem.symbol`
    is `None` for a calendar event): any candidate's analysis may cite it, so
    `validate.py` admits these `source_id`s for every symbol rather than just one.
    """

    source_id: SourceId
    published_at: datetime
    title: str | None
    summary: str
    url: str
    provider: str


class AnalysisContextBlocks(_StrictModel):
    """Run-wide (not per-candidate) deterministic context blocks."""

    market_regime: str | None
    performance_summary: str | None
    calendar_events: list[CalendarEventInput] = []


class AnalysisInput(_StrictModel):
    """`analysis_input.json`: everything a skill needs, and nothing it must fetch."""

    schema_version: Literal["analysis-input-v1"]
    as_of: date
    generated_at: datetime
    context: AnalysisContextBlocks
    candidates: list[CandidateInput]


class SourcedFact(_StrictModel):
    """One factual statement tied to the input source(s) it came from."""

    text: NonBlankText
    source_ids: Annotated[list[SourceId], Field(min_length=1)]


class NewsSummary(_StrictModel):
    """Structured news interpretation for one symbol."""

    facts: list[SourcedFact] = []
    interpretation: list[str] = []
    risk_flags: list[str] = []


class FilingAnalysis(_StrictModel):
    """Structured interpretation of one filing, identified by its `source_id`.

    Form type and filing date are deliberately absent: they are code-owned
    `TextItem` metadata that `validate.py` resolves from `analysis_input.json`
    rather than trusting the skill to echo back accurately.
    """

    source_id: SourceId
    facts: list[SourcedFact] = []
    interpretation: list[str] = []
    red_flags: list[str] = []
    yoy_changes: list[str] = []


class ScreeningAssessment(_StrictModel):
    """Qualitative reading of why a candidate survived deterministic screening."""

    summary: str
    strengths: list[str] = []
    concerns: list[str] = []


class VerdictReason(_StrictModel):
    """One reason behind a verdict.

    `source_ids` may be empty, unlike `SourcedFact`: a reason resting only on
    deterministic inputs the code itself computed (score, sizing constraint)
    has no news/filing source to cite.
    """

    text: NonBlankText
    source_ids: list[SourceId] = []


class Verdict(_StrictModel):
    """The skill's qualitative go/no-go, which never edits screening numbers."""

    recommendation: Literal["proceed", "skip"]
    reasons: list[VerdictReason] = []


class SymbolAnalysis(_StrictModel):
    """One symbol's complete qualitative analysis."""

    symbol: str
    news_summary: NewsSummary | None = None
    filing_analyses: list[FilingAnalysis] = []
    screening_assessment: ScreeningAssessment
    verdict: Verdict


class AnalysisResult(_StrictModel):
    """`analysis_result.json`: the skill's answer, before any machine checks."""

    schema_version: Literal["analysis-result-v1"]
    as_of: date
    generated_by: str
    symbols: list[SymbolAnalysis] = []
    no_trade: bool = False
    no_trade_reason: str | None = None
