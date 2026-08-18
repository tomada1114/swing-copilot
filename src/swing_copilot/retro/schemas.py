"""Strict schemas for the retrospective's two documents (P8-31/P8-32).

`retro_input.json` is written by `copilot-retro export` and read by the
`swing-retro` skill; `retro_result.json` is the skill's answer, validated
back by `copilot-retro ingest` (design §5.3/§5.4).

Held to the same rules as `analysis-input-v3` (E31.2):

* `extra="forbid"` everywhere, so a renamed or invented field fails loudly
  instead of being silently dropped on either side.
* `schema_version` is a `Literal` constant, not a free string.
* `input_digest` binds the document to its own bytes, and the skill copies it
  verbatim into its result so P8-32 can prove both halves refer to the same
  export.

Every aggregate, surprise, and cited source carries an ID. That is not
decoration: `retro-result-v1`'s `evidence_refs` must be provable subsets of
the identifiers supplied here (E32.4), and an evidence space that cannot be
named cannot be checked.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from swing_copilot.analysis.schemas import (
    FilingCoverage,
    FilingInput,
    NewsInput,
    NewsSupply,
    NonBlankText,
    Sha256Digest,
    SourceId,
    canonical_json_digest,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

RETRO_INPUT_SCHEMA_VERSION: Final[Literal["retro-input-v1"]] = "retro-input-v1"
RETRO_RESULT_SCHEMA_VERSION: Final[Literal["retro-result-v1"]] = "retro-result-v1"

#: Design §7's closed set of reasons a verdict went wrong. Closed on purpose:
#: an open vocabulary cannot be counted, and it is the *repetition* of one
#: class across retrospectives that promotes a single miss into a structural
#: proposal (§8.1's qualitative evidence gate).
FailureClass = Literal[
    "information_absent",
    "information_present_missed",
    "interpretation_error",
    "exogenous",
    "threshold_artifact",
]
#: Design §8.1's proposal scope: parameter change, composition change, design review.
ProposalLevel = Literal["L1", "L2", "L3"]
EvidenceBasis = Literal["quantitative", "qualitative", "mixed"]

#: Levels the skill applies itself, so a check the application step can run is
#: mandatory for them (design §8.1). L3 is a design review, decided by
#: `AskUserQuestion` before anything is written, so it may carry none.
_VERIFICATION_PLAN_REQUIRED_LEVELS = frozenset({"L1", "L2"})


class _StrictModel(BaseModel):
    """Base for the dossier: reject unknown fields at every level."""

    model_config = ConfigDict(extra="forbid")


class EvaluationSettings(_StrictModel):
    """The thresholds the classification and the aggregates actually used.

    Copied into the document rather than left implicit so a dossier read
    months later still says which boundaries produced its numbers -- and so
    a proposal to change one of them can cite the value it is changing.
    """

    horizon_5d_weight: float
    horizon_20d_weight: float
    neutral_threshold_pct: float
    severe_threshold_pct: float
    preliminary_sample_threshold: int
    lookback_window_days: int
    proceed_severe_miss_watch_rate: float


class MetricEntry(_StrictModel):
    """One horizon's (or the weighted headline's) value for a metric.

    The dispersion fields arrived with Issue #190 and default to `None`, so a
    dossier archived before them parses unchanged and keeps its digest. A
    `None` there means "no spread is defined for this value" (fewer than two
    observations, or the weight-composed headline, whose horizons are not
    independent samples) -- never "the estimate is exact".
    """

    metric_id: NonBlankText
    #: `None` marks the weight-composed headline rather than one horizon.
    horizon_days: int | None
    #: `None` means "not measurable from this window", never "zero".
    value: float | None
    sample_size: int = Field(ge=0)
    is_preliminary: bool
    stderr: float | None = None
    #: Two-sided 95% interval. An interval spanning 0 means the window cannot
    #: tell the effect's sign, which the L1 evidence gate now requires.
    ci_low: float | None = None
    ci_high: float | None = None
    #: Paired metrics only: run days dropped for carrying just one verdict
    #: side, so a difference averaged over 3 of 20 days cannot pass as one
    #: averaged over 20.
    excluded_day_count: int | None = Field(default=None, ge=0)


class RateMetricEntry(_StrictModel):
    """A rate metric with the same-period baseline it is judged against.

    `ci_low`/`ci_high` are a Wilson score interval (Issue #190), absent on
    dossiers archived before it and on the weight-composed headline.
    """

    metric_id: NonBlankText
    horizon_days: int | None
    value: float | None
    baseline_value: float | None
    is_flagged: bool
    sample_size: int = Field(ge=0)
    is_preliminary: bool
    ci_low: float | None = None
    ci_high: float | None = None


class VerdictMixEntry(_StrictModel):
    """Whether the window's verdicts could produce `proceed` at all (P8-120).

    No baseline and no `horizon_days`: unlike the rate metrics above, a
    verdict is not tied to a horizon, and there is nothing to compare a mix
    against. A single entry, not a list -- one value per window.
    """

    metric_id: NonBlankText
    run_count: int = Field(ge=0)
    verdict_count: int = Field(ge=0)
    proceed_count: int = Field(ge=0)
    skip_count: int = Field(ge=0)
    proceed_ratio: float | None
    is_flagged: bool


class NewsSupplyCellEntry(_StrictModel):
    """One `(news supply level, recommendation)` cell of the supply cross-tab."""

    cell_id: NonBlankText
    #: The graded level, or `unrecorded` for verdicts collected from archives
    #: written before the measurement existed. Deliberately not the
    #: `NewsSupplyLevel` literal: the fourth value is retro's own.
    level: NonBlankText
    recommendation: NonBlankText
    verdict_count: int = Field(ge=0)
    #: `None` only in the `unrecorded` cell, which has no counts to describe.
    min_symbol_mention_items: int | None
    max_symbol_mention_items: int | None
    mean_symbol_mention_items: float | None


class NewsSupplySummaryEntry(_StrictModel):
    """Whether the `sufficient` threshold matches what verdicts did (#154).

    Optional on `AggregateMetrics` so dossiers archived before Issue #154
    keep parsing and keep their digest; absent means "this export did not
    measure it", never "no verdict had thin supply".
    """

    metric_id: NonBlankText
    #: The `symbol_mention_items` floor the levels were graded at, copied in
    #: so a proposal to move it can cite the value it is changing.
    sufficient_threshold: int = Field(ge=1)
    verdict_count: int = Field(ge=0)
    recorded_verdict_count: int = Field(ge=0)
    unrecorded_verdict_count: int = Field(ge=0)
    cells: list[NewsSupplyCellEntry]


class ExitReasonCountEntry(_StrictModel):
    """How many of a stratum's closed shadow positions ended a given way."""

    reason: NonBlankText
    count: int = Field(ge=0)


class TrackedPerformanceEntry(_StrictModel):
    """One verdict side's realized record in the shadow-tracking ledger (#190).

    The counterfactual the retrospective was missing: `proceed` and `skip`
    positions carried under identical exit rules, plus the pooled `all` arm.
    Every monetary figure is a percentage point rather than a dollar -- a
    shadow position never had a share count decided for it.
    """

    metric_id: NonBlankText
    #: `proceed`, `skip`, or `all` for the pooled arm.
    recommendation: NonBlankText
    closed_count: int = Field(ge=0)
    open_count: int = Field(ge=0)
    #: `None` when the stratum has no closed position yet, never `0.0`.
    win_rate: float | None
    profit_factor: float | None
    expectancy_pct: float | None
    avg_r_multiple: float | None
    avg_holding_days: float | None
    exit_reason_counts: list[ExitReasonCountEntry]


class AggregateMetrics(_StrictModel):
    """Design §3.4's headline measures of the qualitative layer.

    The three optional lists arrived with Issue #190 and default to `None` so
    an archived dossier keeps parsing and keeps its digest. `separation` is
    kept unchanged beside them rather than replaced: its metric IDs are cited
    by proposals already in the ledger, and the point of publishing the paired
    and excess versions is that a reader can see whether the three agree.
    """

    separation: list[MetricEntry]
    proceed_severe_miss_rate: list[RateMetricEntry]
    skip_hit_rate: list[RateMetricEntry]
    verdict_mix: VerdictMixEntry
    news_supply: NewsSupplySummaryEntry | None = None
    #: Separation differenced inside each run day, so the market's own move
    #: cancels instead of confounding the comparison.
    separation_paired: list[MetricEntry] | None = None
    #: The same pairing over benchmark-relative returns.
    separation_paired_excess: list[MetricEntry] | None = None
    #: The tracking ledger's realized record, stratified by verdict side.
    tracked_performance: list[TrackedPerformanceEntry] | None = None


class SignalPerformanceEntry(_StrictModel):
    """One signal's P2-11 hit-rate row, included verbatim for one overview.

    `signal_outcomes` is not reinterpreted here (design §5.3 item 2): the
    retrospective shows signal and verdict performance side by side so a
    proposal can tell "the signal was wrong" from "the reading was wrong".
    """

    signal_name: NonBlankText
    true_positive_count: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    neutral_count: int = Field(ge=0)
    hit_rate: float | None
    n: int = Field(ge=0)
    is_preliminary: bool


class AlignmentEntry(_StrictModel):
    """One `(decision, recommendation, horizon)` cell of the human cross-tab."""

    cell_id: NonBlankText
    decision: NonBlankText
    recommendation: NonBlankText
    horizon_days: int
    count: int = Field(ge=0)
    mean_forward_return_pct: float
    hit_count: int = Field(ge=0)
    severe_miss_count: int = Field(ge=0)


class BasisContributionEntry(_StrictModel):
    """One evidence kind's verdict tally and hit share (Issue #191).

    `basis` is `analysis.schemas.VerdictBasis`'s closed vocabulary plus the
    `untagged` bucket, so the values a dossier can carry stay enumerable even
    though the tag itself is optional upstream.
    """

    basis_id: NonBlankText
    basis: NonBlankText
    verdict_count: int = Field(ge=0)
    hit_count: int = Field(ge=0)
    miss_count: int = Field(ge=0)
    neutral_count: int = Field(ge=0)
    hit_citation_ratio: float | None


class SourceContributionEntry(_StrictModel):
    """One `(source_type, provider)` group's citation tally and hit share."""

    contribution_id: NonBlankText
    source_type: NonBlankText
    provider: NonBlankText
    citation_count: int = Field(ge=0)
    hit_citation_count: int = Field(ge=0)
    miss_citation_count: int = Field(ge=0)
    neutral_citation_count: int = Field(ge=0)
    hit_citation_ratio: float | None


class InputCoverageSummary(_StrictModel):
    """Code-counted relationship between input gaps and severe misses.

    `truncated_filing_count` counts export-stage truncation only.
    `exhibit_truncated_filing_count` counts filings whose 8-K exhibits were cut
    short or dropped whole earlier, while being collected, which the character
    counts cannot see (Issues #157/#163). It defaults to 0 so retrospective
    dossiers archived before it existed keep parsing; 0 there means "not
    counted", not "none occurred".
    """

    filing_count: int = Field(ge=0)
    truncated_filing_count: int = Field(ge=0)
    exhibit_truncated_filing_count: int = Field(default=0, ge=0)
    fallback_filing_count: int = Field(ge=0)
    omitted_filing_count: int = Field(ge=0)
    severe_miss_symbol_count_with_gap: int = Field(ge=0)
    severe_miss_symbol_count_without_gap: int = Field(ge=0)
    severe_miss_symbol_count_unknown: int = Field(ge=0)


class ArchivedFilingCoverage(_StrictModel):
    """One original analysis input's filing coverage, keyed by source."""

    source_id: SourceId
    coverage: FilingCoverage


class VerdictReasonEntry(_StrictModel):
    """One reason the verdict gave at the time, with what it cited."""

    text: NonBlankText
    #: May be empty: a reason resting only on deterministic pipeline values
    #: has no news/filing source to cite (mirrors `analysis.VerdictReason`).
    source_ids: list[SourceId]


class SurpriseOutcomeEntry(_StrictModel):
    """One matured horizon of a surprise symbol's realized path."""

    horizon_days: int
    #: The maturity session the return was fixed at, not the observation date.
    maturity_as_of: date
    forward_return_pct: float
    classification: NonBlankText


class FreshnessEntry(_StrictModel):
    """What the text adapters report about the symbol *now* (design §5.3).

    The material for telling `information_absent` from `exogenous`: news and
    filings published after the reviewed run but on or before the
    retrospective's `as_of`. `fetch_failed` marks an attempted fetch that
    raised, which is why an empty list here is not evidence of silence.
    """

    news: list[NewsInput]
    filings: list[FilingInput]
    fetch_failed: bool


class SurpriseDossier(_StrictModel):
    """One severe miss's complete evidence packet."""

    surprise_id: NonBlankText
    run_id: UUID
    symbol: NonBlankText
    run_as_of: date
    strategy_key: NonBlankText
    recommendation: NonBlankText
    no_trade: bool
    reasons: list[VerdictReasonEntry]
    cited_source_ids: list[SourceId]
    outcomes: list[SurpriseOutcomeEntry]
    #: Worst close-to-close drawdown from the run's close inside the evaluated
    #: window; `None` when the bars needed to compute it are missing.
    max_adverse_return_pct: float | None
    input_filing_coverage: list[ArchivedFilingCoverage] = []
    #: The news supply this verdict was made under (Issue #154). `None` when
    #: the archive predates the measurement -- the material for telling a
    #: `sufficient` grade that was still too thin from one that held up.
    news_supply: NewsSupply | None = None
    freshness: FreshnessEntry


class SurpriseBundle(_StrictModel):
    """The capped surprise selection, with what the cap left out.

    `dropped_count` exists so truncation is always visible: a reader must be
    able to tell "these were the only severe misses" from "these were the
    five largest of eleven" (design §5.3, no silent cap).
    """

    max_surprises: int = Field(ge=1)
    dropped_count: int = Field(ge=0)
    items: list[SurpriseDossier]


class ConfigSnapshot(_StrictModel):
    """The settings a proposal could target, plus their hash.

    `config_hash` makes a proposal say *which* configuration it was written
    against, so an outdated proposal cannot be applied to settings that have
    since moved.
    """

    sections: dict[str, JsonValue]
    config_hash: Sha256Digest


class FailureClassCountEntry(_StrictModel):
    """How often one `failure_class` recurred, and whether that clears L2.

    `meets_l2_gate` is computed by deterministic code, not counted by the
    skill (Issue #189): a gate the reader tallies by hand is a gate that
    quietly drifts, and the counting inputs -- past retrospectives' narrations
    -- now live in the database instead of in gitignored reports.
    """

    #: Citable identifier, so an L2 proposal can name the gate row it rests
    #: on instead of restating the count in prose.
    count_id: NonBlankText
    failure_class: FailureClass
    #: Narrations carrying this class across `FailureClassHistory.sessions`.
    count: int = Field(ge=0)
    #: How many of those sessions contributed at least one.
    session_count: int = Field(ge=0)
    meets_l2_gate: bool


class FailureClassHistoryEntry(_StrictModel):
    """The trailing cross-tab design §8.1's L2 qualitative gate reads.

    Counted over *ingested* retrospectives only. The current one is not among
    them -- its narrations do not exist until `copilot-retro ingest` accepts
    them -- so `count` is a floor: today's reading can only add to it. That is
    deliberate, and it is the only definition that makes the number
    reproducible for a given `as_of`.
    """

    gate_window_sessions: int = Field(ge=1)
    gate_min_count: int = Field(ge=1)
    #: The `retro_as_of` of each session counted, newest first.
    sessions: list[date]
    counts: list[FailureClassCountEntry]


class ConfigVersionAggregateEntry(_StrictModel):
    """One configuration's own slice of the window's separation (Issue #189).

    Exists so "did the numbers move because the configuration moved" stops
    being unanswerable. Reading it needs the same care as any subgroup: the
    split can only shrink the sample, so `separation`'s `is_preliminary` and
    `sample_size` govern, and a difference between two configurations is a
    hypothesis to verify, never a finding on its own.
    """

    #: `runs.config_hash` verbatim: the full effective-run fingerprint.
    #: Deliberately not typed as `Sha256Digest` -- that column is a free
    #: VARCHAR, and refusing to build the whole dossier because some archived
    #: run recorded a non-digest there would fail hard on an archive's shape.
    config_hash: NonBlankText
    #: Digest of the proposal-relevant sections alone. Two entries sharing it
    #: differ only in settings no proposal targets (delivery, scheduling).
    #: `None` when the configuration predates the `config_versions` ledger.
    snapshot_hash: Sha256Digest | None
    #: `None` for the same reason: never recorded, not "first run today".
    first_seen_run_date: date | None
    run_count: int = Field(ge=0)
    outcome_count: int = Field(ge=0)
    separation: list[MetricEntry]


class ProposalsLedger(_StrictModel):
    """Where the proposal ledger lives and which proposals are closed.

    `rejected_proposal_ids` feeds P8-32's re-proposal guard: an RP-ID already
    rejected (or whose verification failed) may only come back with an
    explicit reopening justification.
    """

    path: str
    exists: bool
    rejected_proposal_ids: list[str]


class RetroInput(_StrictModel):
    """`retro_input.json`: everything the retrospective skill is given.

    Deliberately a closed document. The skill reads only this file and the
    ledger it names; nothing it produces may cite evidence that is not
    identified somewhere in here.
    """

    schema_version: Literal["retro-input-v1"]
    as_of: date
    #: Wall-clock provenance from the injected `Clock`. Never a substitute for
    #: `as_of`, which is the only point-in-time cutoff any query used.
    generated_at: datetime
    window_start: date
    evaluation: EvaluationSettings
    aggregates: AggregateMetrics
    signal_performance: list[SignalPerformanceEntry]
    human_alignment: list[AlignmentEntry]
    source_contribution: list[SourceContributionEntry]
    #: Hit rate per evidence kind (Issue #191). Defaults to empty so
    #: `retro_input.json` documents archived before it existed keep parsing;
    #: empty there means "not computed", not "no verdict cited anything".
    basis_contribution: list[BasisContributionEntry] = []
    input_coverage: InputCoverageSummary | None = None
    #: Issue #189: the L2 qualitative gate's inputs and verdict, computed from
    #: the persisted narrations of earlier retrospectives. `None` means no
    #: retrospective has been ingested at or before `as_of` -- including every
    #: dossier archived before the table existed.
    failure_class_history: FailureClassHistoryEntry | None = None
    #: Issue #189: the window's separation split by the configuration each run
    #: executed under. Empty means "not computed" (a dossier from before the
    #: field, or a window with no run at all), never "one configuration".
    aggregates_by_config: list[ConfigVersionAggregateEntry] = []
    surprises: SurpriseBundle
    config_snapshot: ConfigSnapshot
    proposals_ledger: ProposalsLedger
    #: Fail-soft data-quality remarks from the export (missing bars, a failed
    #: freshness fetch). Part of the evidence: a metric computed over a window
    #: with gaps should be read knowing that.
    notes: list[str]
    input_digest: Sha256Digest

    @model_validator(mode="after")
    def _verify_input_digest(self) -> Self:
        """Reject a document whose body was edited after it was written."""
        expected = retro_input_digest(self.model_dump(mode="json"))
        if self.input_digest != expected:
            msg = "input_digest does not match canonical retro input JSON"
            raise ValueError(msg)
        return self


def retro_input_digest(payload: dict[str, object]) -> str:
    """Hash a retro input while preserving pre-coverage v1 compatibility."""
    normalized = cast("dict[str, object]", _drop_legacy_defaults(payload))
    return canonical_json_digest(normalized, excluded_field="input_digest")


#: Keys whose `None` is the "this export did not measure it" form of a field
#: added by Issue #190. Dropping them keeps an archived dossier's digest
#: reproducible: the document that never carried the field must hash exactly
#: as it did before the field existed.
_ISSUE_190_OPTIONAL_KEYS = frozenset(
    {
        "stderr",
        "ci_low",
        "ci_high",
        "excluded_day_count",
        "separation_paired",
        "separation_paired_excess",
        "tracked_performance",
    }
)


def _drop_legacy_defaults(value: object) -> object:
    """Omit defaults introduced after the original retro-input-v1 contract.

    Each entry is a field added later whose absent form must hash exactly as
    the document that never had the field did, or every archived dossier's
    `input_digest` would stop verifying the day the field was added.
    """
    if isinstance(value, dict):
        return {
            key: _drop_legacy_defaults(child)
            for key, child in value.items()
            if not (
                (key == "input_coverage" and child is None)
                or (key == "input_filing_coverage" and child == [])
                or (key == "news_supply" and child is None)
                # Issue #190: dispersion fields on every metric entry, and the
                # three aggregate blocks added with them.
                or (key in _ISSUE_190_OPTIONAL_KEYS and child is None)
                # Issue #191: per-basis hit rates, absent before the field.
                or (key == "basis_contribution" and child == [])
                # Issue #189: the L2 gate cross-tab and the per-config split,
                # absent on every dossier written before the two ledgers
                # existed -- and on any window that still has neither.
                or (key == "failure_class_history" and child is None)
                or (key == "aggregates_by_config" and child == [])
            )
        }
    if isinstance(value, list):
        return [_drop_legacy_defaults(child) for child in value]
    return value


def _duplicate_value(values: Iterable[str]) -> str | None:
    """Return the first repeated value, preserving the document's order."""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


#: An identifier the dossier supplied. `ingest` proves each one is a member of
#: that space (E32.4); the type only guarantees it is not blank.
EvidenceRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SurpriseNarration(_StrictModel):
    """The skill's re-reading of one surprise symbol (design §7).

    `failure_class` is a single mandatory value rather than a list: the point
    of the classification is to count how often one cause repeats, and a
    narration that hedges across three classes cannot be counted at all.
    """

    surprise_id: NonBlankText
    failure_class: FailureClass
    narrative: NonBlankText
    #: Non-empty by construction, mirroring `SourcedFact.source_ids`: a reading
    #: that cites nothing cannot be checked against what was actually supplied.
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class Proposal(_StrictModel):
    """One improvement proposal with design §8.1's mandatory fields.

    `verification_plan` is required rather than defaulted so a proposal has to
    state one explicitly -- including stating `null` for an L3 design review,
    which the validator below allows only at that level.
    """

    proposal_key: NonBlankText
    level: ProposalLevel
    #: What the proposal changes: a config path, a module, or an area.
    target: NonBlankText
    title: NonBlankText
    claim: NonBlankText
    expected_effect: NonBlankText
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    evidence_basis: EvidenceBasis
    verification_plan: NonBlankText | None
    #: Non-empty: "no risk" is a claim, and an unstated one cannot be reviewed.
    risks: list[NonBlankText] = Field(min_length=1)
    #: Required only when reopening a proposal the ledger closed (E32.2).
    reopen_justification: NonBlankText | None = None

    @model_validator(mode="after")
    def _verify_verification_plan_is_present_when_applied(self) -> Self:
        if (
            self.level in _VERIFICATION_PLAN_REQUIRED_LEVELS
            and self.verification_plan is None
        ):
            msg = f"verification_plan is required for a {self.level} proposal"
            raise ValueError(msg)
        return self


class RetroResult(_StrictModel):
    """`retro_result.json`: the skill's narration and proposals, untrusted.

    Nothing here is believed on sight. `as_of` and `input_digest` must match
    the dossier this answers, every `evidence_refs` entry must be an
    identifier that dossier supplied, and every user-visible string passes
    CON-03 before it can reach a report or the ledger (`retro/validate.py`).
    """

    schema_version: Literal["retro-result-v1"]
    as_of: date
    #: Copied verbatim from `retro_input.json` so ingest can prove both halves
    #: describe the same export.
    input_digest: Sha256Digest
    #: Design §6 step 4 / D9: the answer to "was there an L2/L3-level
    #: structural observation?", stated every time -- "再点検の上でなし"
    #: included. A required field because a discipline that is only written in
    #: skill instructions is the one that quietly stops happening.
    structural_review_note: NonBlankText
    narrations: list[SurpriseNarration]
    proposals: list[Proposal]

    @model_validator(mode="after")
    def _verify_unique_item_identities(self) -> Self:
        """Reject ambiguous identities the ledger and the guard key on."""
        duplicate_surprise = _duplicate_value(
            narration.surprise_id for narration in self.narrations
        )
        if duplicate_surprise is not None:
            msg = f"narration surprise_id must be unique: {duplicate_surprise!r}"
            raise ValueError(msg)
        duplicate_key = _duplicate_value(
            proposal.proposal_key for proposal in self.proposals
        )
        if duplicate_key is not None:
            msg = f"proposal_key must be unique within one result: {duplicate_key!r}"
            raise ValueError(msg)
        return self
