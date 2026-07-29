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

import hashlib
import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

if TYPE_CHECKING:
    from collections.abc import Iterable

INPUT_SCHEMA_VERSION: Final[Literal["analysis-input-v3"]] = "analysis-input-v3"
RESULT_SCHEMA_VERSION: Final[Literal["analysis-result-v2"]] = "analysis-result-v2"

SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_DATETIME_FIELD_NAMES = frozenset({"generated_at", "published_at", "filed_at"})


def canonical_json_digest(payload: dict[str, object], *, excluded_field: str) -> str:
    """Return the full SHA-256 for canonical JSON excluding one digest field.

    The artifact carrying a digest cannot include that digest in its own hash.
    All other fields are serialized with stable key ordering, compact separators,
    and UTF-8 so independently produced documents bind to the same bytes.
    """
    canonical_payload = _canonicalize_for_digest(
        {key: value for key, value in payload.items() if key != excluded_field}
    )
    canonical = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonicalize_for_digest(value: object, field_name: str | None = None) -> object:
    """Normalize schema datetime values before canonical JSON serialization."""
    if isinstance(value, dict):
        return {
            key: _canonicalize_for_digest(nested, key) for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize_for_digest(item) for item in value]
    if field_name in _DATETIME_FIELD_NAMES and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return parsed.isoformat()
    return value


class _StrictModel(BaseModel):
    """Base for both directions of the contract: reject unknown fields."""

    model_config = ConfigDict(extra="forbid")


def _duplicate_value(values: Iterable[str]) -> str | None:
    """Return the first repeated value, preserving the document's order."""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


class NewsInput(_StrictModel):
    """One collected news item offered to the analysis skill."""

    source_id: SourceId
    published_at: datetime
    headline: str | None
    summary: str
    url: str
    provider: str


FilingSelectionMode = Literal[
    "full",
    "section_priority",
    "section_priority_partial",
    "head_fallback",
    "omitted_symbol_budget",
]
FilingSectionStatus = Literal["full", "partial", "missing"]


class FilingSectionCoverage(_StrictModel):
    """How much of one priority 10-Q section reached the exported text."""

    name: NonBlankText
    status: FilingSectionStatus


class FilingCoverage(_StrictModel):
    """Code-owned completeness metadata for one exported filing."""

    original_chars: int = Field(ge=0)
    exported_chars: int = Field(ge=0)
    is_truncated: bool
    selection_mode: FilingSelectionMode
    sections: list[FilingSectionCoverage] = []

    @model_validator(mode="after")
    def _verify_lengths(self) -> Self:
        """Keep the explicit truncation signal consistent with its counts."""
        if self.exported_chars > self.original_chars:
            msg = "exported_chars cannot exceed original_chars"
            raise ValueError(msg)
        if self.is_truncated != (self.exported_chars < self.original_chars):
            msg = "is_truncated must match exported_chars < original_chars"
            raise ValueError(msg)
        return self


class FilingInput(_StrictModel):
    """One collected filing excerpt offered to the analysis skill."""

    source_id: SourceId
    form_type: str
    filed_at: datetime
    text: str
    url: str
    # Historical `analysis-input-v2` archives remain readable by P8 collect.
    # Every newly exported v3 filing is required to carry this field by
    # `AnalysisInput._verify_unique_candidates_and_sources`.
    coverage: FilingCoverage | None = None


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

    schema_version: Literal["analysis-input-v2", "analysis-input-v3"]
    run_id: UUID
    as_of: date
    strategy_key: NonBlankText
    input_digest: Sha256Digest
    generated_at: datetime
    context: AnalysisContextBlocks
    candidates: list[CandidateInput]

    @model_validator(mode="before")
    @classmethod
    def _verify_input_digest(cls, value: object) -> object:
        """Reject a document whose declared digest is not its canonical input."""
        if not isinstance(value, dict):
            return value
        actual = value.get("input_digest")
        if isinstance(actual, str) and actual != canonical_json_digest(
            value, excluded_field="input_digest"
        ):
            msg = "input_digest does not match canonical analysis input JSON"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _verify_unique_candidates_and_sources(self) -> Self:
        """Reject ambiguous candidate and per-candidate source identities."""
        duplicate_symbol = _duplicate_value(
            candidate.symbol for candidate in self.candidates
        )
        if duplicate_symbol is not None:
            msg = f"candidate symbols must be unique: {duplicate_symbol!r}"
            raise ValueError(msg)
        for candidate in self.candidates:
            source_ids = [item.source_id for item in candidate.news]
            source_ids.extend(item.source_id for item in candidate.filings)
            duplicate_source_id = _duplicate_value(source_ids)
            if duplicate_source_id is not None:
                msg = (
                    "candidate source_ids must be unique for "
                    f"{candidate.symbol!r}: {duplicate_source_id!r}"
                )
                raise ValueError(msg)
            if self.schema_version == INPUT_SCHEMA_VERSION and any(
                filing.coverage is None for filing in candidate.filings
            ):
                msg = "analysis-input-v3 requires coverage for every filing"
                raise ValueError(msg)
        return self


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

    schema_version: Literal["analysis-result-v2"]
    run_id: UUID
    as_of: date
    strategy_key: NonBlankText
    input_digest: Sha256Digest
    generated_by: str
    symbols: list[SymbolAnalysis] = []
    no_trade: bool = False
    no_trade_reason: NonBlankText | None = None

    @model_validator(mode="after")
    def _verify_complete_no_trade_contract(self) -> Self:
        """Reject ambiguous result identities and run-level trade state."""
        duplicate_symbol = _duplicate_value(symbol.symbol for symbol in self.symbols)
        if duplicate_symbol is not None:
            msg = f"result symbols must be unique: {duplicate_symbol!r}"
            raise ValueError(msg)
        if self.no_trade and self.no_trade_reason is None:
            msg = "no_trade_reason is required when no_trade is true"
            raise ValueError(msg)
        if not self.no_trade and self.no_trade_reason is not None:
            msg = "no_trade_reason must be null when no_trade is false"
            raise ValueError(msg)
        return self
