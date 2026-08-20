"""Strict JSON schemas for the pipeline <-> skill analysis contract (FR-08, CON-03).

`analysis_input.json` is produced by `copilot-daily` and read by the
`swing-daily` skill; `analysis_result.json` is produced by the skill and read
by `copilot-ingest-analysis`. Both sides are validated here with
`extra="forbid"`, so a renamed or invented field fails loudly instead of being
silently dropped.

Provenance is structural, not advisory: every `SourcedFact` must cite at least
one input `source_id` and carry the verbatim `evidence_quote` it was written
from, and `validate.py` additionally proves those IDs were supplied for that
symbol and that the quote occurs in one of their exported bodies. Separating
`facts` from `interpretation` does not by itself prevent an unsupported claim
-- the source IDs make a rendered statement traceable, and the quote makes the
trace falsifiable.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from swing_copilot.analysis.evidence import (
    MAX_EVIDENCE_QUOTE_CHARS,
    MIN_EVIDENCE_QUOTE_CHARS,
    normalize_evidence_text,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

INPUT_SCHEMA_VERSION: Final[Literal["analysis-input-v3"]] = "analysis-input-v3"
RESULT_SCHEMA_VERSION: Final[Literal["analysis-result-v3"]] = "analysis-result-v3"

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
    return _canonical_sha256(
        {key: value for key, value in payload.items() if key != excluded_field}
    )


def filing_body_digest(text: str) -> str:
    """Return the SHA-256 that identifies one filing's exported body (Issue #261).

    A filing reading is a function of the filing text, and that text does not
    move when the trading day does: two consecutive runs exported the same 14
    filings for their five shared candidates, accession for accession. Keying
    an `analysis_work/filings-<SYMBOL>.json` fragment on this digest is what
    lets the next run reuse yesterday's reading of an unchanged 10-Q instead of
    re-reading it from scratch (`analysis/fragment.py`).

    The input is deliberately the *exported* body -- what survived
    `filing_selection.py`'s budget and truncation and actually reached the
    skill -- not the collected original. A change in how much of a filing the
    expert is handed changes what the reading could have been written from, so
    it has to invalidate the earlier one.

    Shares its serialization with `canonical_json_digest` rather than hashing
    the string on its own, so this contract has one digest implementation
    instead of two.

    Args:
        text: The filing body exactly as `analysis_input.json` exports it.

    Returns:
        The full 64-character lowercase SHA-256 hex digest.
    """
    return _canonical_sha256({"text": text})


def _canonical_sha256(payload: dict[str, object]) -> str:
    """Hash one payload as canonical JSON: stable key order, compact, UTF-8."""
    canonical = json.dumps(
        _canonicalize_for_digest(payload),
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
# P8-122: "missing" is retained solely so archived analysis_input.json
# documents (written before this change) keep parsing -- new code never
# emits it, reporting "absent_from_filing" or "not_parsed" instead
# (filing_selection.py's Part-grouping rule distinguishes them).
FilingSectionStatus = Literal[
    "full", "partial", "absent_from_filing", "not_parsed", "missing"
]
# Issue #181: "value_selected" is the 8-K exhibit shape -- passages were kept
# or dropped by value (financial tables first, boilerplate first out), so the
# gap is neither the middle nor the tail but wherever the inline marker sits.
FilingSectionOmissionShape = Literal["head_only", "head_and_tail", "value_selected"]


class FilingSectionCoverage(_StrictModel):
    """How much of one priority filing part reached the exported text.

    A "part" is a priority 10-Q section (`part_i_item_1` …) or, for an 8-K,
    one component of the collected text: the primary document
    (`exhibit_primary`) or one appended exhibit (`exhibit_ex_99_1` …,
    Issue #181). Both are chosen deterministically under one budget, so both
    report their deficit the same way.

    `status` alone cannot say how much a `partial` section lost, nor where the
    gap sits. That became load-bearing once truncation started keeping a
    section's head *and* its tail: a reader can no longer assume the missing
    range is the tail. The character pair mirrors `FilingCoverage` at section
    granularity, and `omission_shape` names the retained shape —
    `head_and_tail` means the middle was dropped, `head_only` means everything
    past the head was, `value_selected` means lower-value passages were
    dropped wherever they sat and every gap is marked inline.

    All three stay optional so they can be added without moving off
    `analysis-input-v3`: archived inputs written before these fields existed,
    and coverage rebuilt from `analysis_source_coverage` rows (which persist
    only name/status), legitimately carry `None`. Absent means "not recorded",
    never "nothing was omitted".
    """

    name: NonBlankText
    status: FilingSectionStatus
    original_chars: int | None = Field(default=None, ge=0)
    exported_chars: int | None = Field(default=None, ge=0)
    omission_shape: FilingSectionOmissionShape | None = None

    @model_validator(mode="after")
    def _verify_section_lengths(self) -> Self:
        """Keep the deficit counts consistent with the status they qualify."""
        original, exported = self.original_chars, self.exported_chars
        if (original is None) != (exported is None):
            msg = "original_chars and exported_chars must be given together"
            raise ValueError(msg)
        if self.omission_shape is not None and self.status != "partial":
            msg = "omission_shape applies only to a partial section"
            raise ValueError(msg)
        if original is None or exported is None:
            return self
        if exported > original:
            msg = "exported_chars cannot exceed original_chars"
            raise ValueError(msg)
        if self.status == "partial" and exported == original:
            msg = "a partial section must export fewer chars than the original"
            raise ValueError(msg)
        return self


class FilingCoverage(_StrictModel):
    """Code-owned completeness metadata for one exported filing.

    `original_chars` / `exported_chars` / `is_truncated` describe the *export*
    stage only: how much of the collected `TextItem.content_text` reached this
    document. They cannot see a loss that happened earlier, at collection.
    An 8-K's `EX-99*` exhibits are cut off at a per-filing ceiling while being
    fetched, so the truncated text is already what `content_text` holds; the
    export then copies it whole and reports `is_truncated: false`,
    `selection_mode: full` (Issue #157).

    `exhibit_truncated` closes that blind spot: it is `true` when any marker in
    `EXHIBIT_LOSS_MARKERS` is present in the collected filing text this export
    was derived from -- an exhibit cut at the character ceiling, or one never
    fetched because the filing offered more than `_MAX_EXHIBITS_PER_FILING`
    (Issue #163). It is read from the text rather than from a collection-time
    companion field, because the text is what gets persisted: the signal
    therefore survives a round trip through storage and is recomputed
    identically wherever a `TextItem` is selected again.

    One boolean deliberately covers both causes: what the reader has to decide
    is whether the filing text is complete, and it is not, either way. Which
    cap applied stays legible in the text itself, where the two markers read
    differently, rather than costing a second field and a second column.

    `false` means **no marker is present**, not "nothing is missing" -- the
    same distinction `FilingSectionCoverage` draws for its optional fields.
    A filing collected before the markers existed, one whose exhibits failed to
    download, and one whose exhibit text was elided by the source itself all
    report `false`.
    It defaults to `false` so archived `analysis-input-v2`/`-v3` documents
    written before the field existed keep parsing (the `input_digest` check
    hashes the raw document, so the added field does not invalidate them), and
    so does coverage rebuilt from an `analysis_source_coverage` row whose
    column is `NULL`. Because that `false` is ambiguous on those paths,
    `retro/collect.py` stores only what a document actually stated and
    `retro/export.py` counts a symbol with an unrecorded row as `unknown`
    rather than as gap-free.
    """

    original_chars: int = Field(ge=0)
    exported_chars: int = Field(ge=0)
    is_truncated: bool
    selection_mode: FilingSelectionMode
    exhibit_truncated: bool = False
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


NewsSupplyLevel = Literal["sufficient", "sparse", "none"]


class NewsSupply(_StrictModel):
    """How much of the exported news names this symbol at all (Issue #130).

    Order alone cannot say where a candidate's own material ends: `news[]` is
    sorted by relevance, but the reader sees no boundary between "articles
    about this company" and "sector round-ups that merely arrived in its
    feed". Without that, an analysis cannot tell "nothing bad was reported"
    apart from "almost nothing about this company was supplied", and the
    second was read as the first (Issue #130's J.B. Hunt run).

    The counts are candidate-level aggregates, deliberately not a per-item
    relevance flag: they let a reader qualify a conclusion drawn from the set,
    without offering a per-article score the skill could re-rank on.

    `symbol_mention_items` is a *lower bound* on company-specific supply -- an
    article that discusses the company without ever printing its ticker is not
    counted -- so the level errs toward declaring uncertainty. It stays
    optional so archived `analysis-input-v2`/`-v3` documents written before it
    existed keep parsing; absent means "not measured", never "measured as
    sufficient".
    """

    collected_items: int = Field(ge=0)
    exported_items: int = Field(ge=0)
    symbol_mention_items: int = Field(ge=0)
    level: NewsSupplyLevel

    @model_validator(mode="after")
    def _verify_supply_counts(self) -> Self:
        """Keep the level and its counts from disagreeing with each other."""
        if self.exported_items > self.collected_items:
            msg = "exported_items cannot exceed collected_items"
            raise ValueError(msg)
        if self.symbol_mention_items > self.exported_items:
            msg = "symbol_mention_items cannot exceed exported_items"
            raise ValueError(msg)
        if (self.level == "none") != (self.symbol_mention_items == 0):
            msg = "level 'none' means exactly zero symbol_mention_items"
            raise ValueError(msg)
        return self


class CandidateInput(_StrictModel):
    """One screened candidate's deterministic context plus its untrusted text.

    `score_breakdown`/`risk_constraints`/`prior_verdicts` are pre-rendered
    text blocks produced by `analysis/context.py` from values the code already
    computed. They exist so the skill's narrative can be
    checked against the code's own quantitative determination, never so the
    skill can restate or override it.
    """

    symbol: str
    score_breakdown: str
    risk_constraints: str
    #: This symbol's own earlier verdicts and how they turned out (Issue
    #: #191). Optional so `analysis-input-v3` documents archived before it
    #: existed keep parsing; `None` means "no prior verdict was archived",
    #: which for a first-time candidate is the normal state.
    prior_verdicts: str | None = None
    news: list[NewsInput]
    # Optional by design, unlike `FilingInput.coverage`: requiring it under
    # `analysis-input-v3` would make every v3 document archived before Issue
    # #130 unreadable to P8 collect.
    news_supply: NewsSupply | None = None
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
    """One factual statement tied to the input source(s) it came from.

    `evidence_quote` is the verbatim excerpt of the cited body the statement was
    written from; `validate.py` proves it occurs there. It stays optional on the
    model so archived `analysis-result-v2` documents remain readable by P8
    collect -- a live `analysis-result-v3` ingest withholds any symbol whose
    fact omits it, rather than accepting the fact unverified.
    """

    text: NonBlankText
    source_ids: Annotated[list[SourceId], Field(min_length=1)]
    evidence_quote: str | None = None

    @model_validator(mode="after")
    def _verify_quote_length(self) -> Self:
        """Reject a quote too short to evidence anything or long enough to dump.

        The bounds apply to the normalized form, because that is the text the
        containment check actually runs on.
        """
        if self.evidence_quote is None:
            return self
        length = len(normalize_evidence_text(self.evidence_quote))
        if length < MIN_EVIDENCE_QUOTE_CHARS:
            msg = (
                "evidence_quote must normalize to at least "
                f"{MIN_EVIDENCE_QUOTE_CHARS} characters, got {length}"
            )
            raise ValueError(msg)
        if length > MAX_EVIDENCE_QUOTE_CHARS:
            msg = (
                "evidence_quote must normalize to at most "
                f"{MAX_EVIDENCE_QUOTE_CHARS} characters, got {length}"
            )
            raise ValueError(msg)
        return self


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


#: The closed set of evidence kinds a verdict reason may be tagged with
#: (Issue #191). Kept flat and small on purpose: every additional value
#: splits the same fixed number of matured verdicts into thinner buckets, so
#: a hit rate per basis stops being readable. `market_regime` and
#: `risk_sizing` cover the two code-owned context blocks; the remaining four
#: cover the evidence a skill actually reads.
VerdictBasis = Literal[
    "technical_score",
    "news_catalyst",
    "filing_fundamental",
    "risk_sizing",
    "market_regime",
    "peer_relative",
]
#: The same values as a runtime-iterable tuple, derived from the type rather
#: than restated, so aggregation and the instruction-drift test can never
#: disagree with what the schema actually accepts.
VERDICT_BASES: Final[tuple[str, ...]] = get_args(VerdictBasis)


class VerdictReason(_StrictModel):
    """One reason behind a verdict.

    `source_ids` may be empty, unlike `SourcedFact`: a reason resting only on
    deterministic inputs the code itself computed (score, sizing constraint)
    has no news/filing source to cite.

    `basis` tags *which kind* of evidence the reason rests on. It is a closed
    vocabulary for the same reason `retro.evaluate`'s failure classes are one:
    free text cannot be aggregated, so "decisions justified by an earnings
    surprise" could never be compared against "decisions justified by the
    technical score alone" (Issue #191). Nothing in `validate.py` can check a
    `basis` against the input -- only the writer knows which evidence it
    actually leaned on -- so it is deliberately outside the provenance
    contract: a wrong tag skews an aggregate, it never admits an uncited
    claim. It stays optional so `analysis-result-v3` documents archived
    before this existed keep parsing; absent means "not tagged", never
    "tagged as technical".
    """

    text: NonBlankText
    source_ids: list[SourceId] = []
    basis: VerdictBasis | None = None


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
    """`analysis_result.json`: the skill's answer, before any machine checks.

    `analysis-result-v2` remains parseable so P8 collect can still read runs
    archived before `evidence_quote` existed. It is not ingestible: a live
    ingest requires `analysis-result-v3` (`validate.validate_analysis`), because
    a v2 document carries no quotes to verify.
    """

    schema_version: Literal["analysis-result-v2", "analysis-result-v3"]
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
