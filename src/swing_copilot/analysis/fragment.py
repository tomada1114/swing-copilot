"""Strict contract for one `analysis_work/` fragment (Issue #132).

An `analysis_result.json` is assembled from per-symbol, per-expert fragments
that each expert subagent writes before the orchestrator merges them. Those
fragments are *not* a subset of `AnalysisResult`: they carry the work metadata
`run_id` / `as_of` / `input_digest` / `ac_check` that the merge deliberately
drops, and they carry exactly one payload key instead of a whole symbol. So
`load_analysis_result` cannot read one, and until this module existed every
expert re-implemented its own pre-flight check -- 15 of them in the 2026-08-11
run, at differing strength (a grep for forbidden phrases does not reproduce the
NFKC normalization `safety.py` applies, so it can report a pass on text that
ingest will withhold).

The envelope is also what decides whether a fragment can be *reused* instead of
re-analyzed, and the two halves of the contract answer that differently
(Issue #261). News and screening readings are `as_of`-dependent, so they are
reusable only within the run that produced them. A filing reading is not:
`filing_body_digests` keys it on the filing bodies themselves, so an unchanged
10-Q is read once rather than once per trading day.

This module supplies the missing half: a strict schema for the fragment
envelope, and a verification path that hands the payload straight to
`validate.verify_symbol_analysis` -- the same function ingest runs. The
pre-flight check is therefore identical to the check that will gate the report,
not an approximation of it. The check still belongs *before* the write, because
ingest is fail-closed with no retry (AC15): a violation found afterwards costs
the day's analysis for that symbol.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Final, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from swing_copilot.analysis.schemas import (
    FilingAnalysis,
    NewsSummary,
    NonBlankText,
    ScreeningAssessment,
    Sha256Digest,
    SourceId,
    SymbolAnalysis,
    Verdict,
    filing_body_digest,
)
from swing_copilot.analysis.validate import (
    calendar_source_bodies,
    verify_symbol_analysis,
)
from swing_copilot.strict_model import StrictModel

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.analysis.schemas import AnalysisInput, CandidateInput

#: Which expert produced a fragment, and the `<kind>` in its filename.
FragmentKind = Literal["news", "filings", "screening"]

#: The one payload key each kind of fragment is allowed to carry. Ordered so
#: error messages list the keys the way `output-schema.md` documents them.
PAYLOAD_FIELD_BY_KIND: Final[dict[FragmentKind, str]] = {
    "news": "news_summary",
    "filings": "filing_analyses",
    "screening": "screening_assessment",
}
_KIND_BY_PAYLOAD_FIELD: Final = {
    field: kind for kind, field in PAYLOAD_FIELD_BY_KIND.items()
}

#: `analysis_work/<kind>-<SYMBOL>.json`, the fixed naming rule of the contract.
_FILENAME_PATTERN: Final = re.compile(
    rf"^(?P<kind>{'|'.join(PAYLOAD_FIELD_BY_KIND)})-(?P<symbol>.+)\.json$"
)

#: Stand-ins for the sections a fragment does not carry. They exist only so the
#: payload can be handed to the whole-symbol checker; both are empty, so they
#: contribute no source ID, no fact, and no displayable text of their own.
_ABSENT_ASSESSMENT: Final = ScreeningAssessment(summary="")
_ABSENT_VERDICT: Final = Verdict(recommendation="skip")


class AnalysisFragment(StrictModel):
    """One expert's `analysis_work/<kind>-<SYMBOL>.json`.

    The three identity fields are copied verbatim from `analysis_input.json`.
    For a news or screening fragment they *are* the reuse key: both readings
    are genuinely `as_of`-dependent (one reads the day's articles, the other
    the day's deterministic score), so only the run that produced them can use
    them, and everything else is yesterday's leftovers.

    A filings fragment is keyed differently (Issue #261). Its reading is a
    function of the filing bodies, which do not move when the trading day
    does, so `filing_body_digests` -- the exported body of every filing the
    expert was handed, hashed -- decides reuse instead, and the three identity
    fields stay purely as a record of which run first produced the reading.

    `ac_check` is the expert's own AC self-check declaration: it is free text
    and no machine reads its content, but its absence means the self-check was
    never declared, so it is required rather than optional.
    """

    run_id: UUID
    as_of: date
    input_digest: Sha256Digest
    symbol: NonBlankText
    ac_check: NonBlankText
    news_summary: NewsSummary | None = None
    filing_analyses: list[FilingAnalysis] = Field(default_factory=list)
    #: `source_id` -> `filing_body_digest(text)` for every filing the expert
    #: was given, copied verbatim from its `slice-filings-<SYMBOL>.json`.
    #: Filings the expert read but wrote nothing about are included on purpose:
    #: the map has to say what the reading *covered*, or an empty
    #: `filing_analyses` would look reusable against a symbol that has since
    #: filed something new.
    filing_body_digests: dict[SourceId, Sha256Digest] | None = None
    screening_assessment: ScreeningAssessment | None = None

    @model_validator(mode="after")
    def _verify_exactly_one_payload(self) -> Self:
        """Hold the fragment to one symbol and one expert.

        Presence is judged by what the document actually set, not by what the
        value is: `news_summary: null` and `filing_analyses: []` are the
        contract's way of saying "analyzed, and empty", which has to stay
        distinguishable from "this expert did not run".
        """
        provided = [
            field
            for field in PAYLOAD_FIELD_BY_KIND.values()
            if field in self.model_fields_set
        ]
        if len(provided) != 1:
            listed = ", ".join(provided) if provided else "none"
            msg = (
                "a fragment must carry exactly one payload key of "
                f"{', '.join(PAYLOAD_FIELD_BY_KIND.values())}, got {listed}"
            )
            raise ValueError(msg)
        if "screening_assessment" in self.model_fields_set and (
            self.screening_assessment is None
        ):
            msg = (
                "screening_assessment must not be null: every symbol requires "
                "one, so an empty reading has to be written out"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _verify_filing_body_digests(self) -> Self:
        """Require the filings expert, and only it, to declare what it read.

        Runs after `_verify_exactly_one_payload`, which is what makes `kind`
        answerable at all. The map is required rather than optional because an
        absent one cannot be told apart from "this filing set happened to be
        empty", and reuse would then rest on an assumption instead of a hash.
        """
        if self.kind != "filings":
            if self.filing_body_digests is not None:
                msg = (
                    "filing_body_digests belongs to a filings fragment alone, "
                    f"not to one carrying {PAYLOAD_FIELD_BY_KIND[self.kind]}"
                )
                raise ValueError(msg)
            return self
        if not self.filing_body_digests:
            msg = (
                "a filings fragment must declare filing_body_digests, copied "
                "verbatim from its input slice: it is what lets a later run "
                "reuse this reading instead of re-reading the filing"
            )
            raise ValueError(msg)
        undeclared = sorted(
            {item.source_id for item in self.filing_analyses}
            - set(self.filing_body_digests)
        )
        if undeclared:
            msg = (
                "filing_body_digests must cover every analyzed filing, missing "
                f"{', '.join(undeclared)}"
            )
            raise ValueError(msg)
        return self

    @property
    def kind(self) -> FragmentKind:
        """Which expert this fragment answers for, from its payload key."""
        field = next(
            field
            for field in PAYLOAD_FIELD_BY_KIND.values()
            if field in self.model_fields_set
        )
        return _KIND_BY_PAYLOAD_FIELD[field]


def as_symbol_analysis(fragment: AnalysisFragment) -> SymbolAnalysis:
    """Lift one fragment into the shape the ingest-time checker accepts.

    The sections this fragment does not own are filled with empty stand-ins
    rather than omitted, because `SymbolAnalysis` requires them. They add
    nothing checkable, so every error the checker then reports is attributable
    to the fragment's own payload.

    Args:
        fragment: The parsed fragment.

    Returns:
        A `SymbolAnalysis` carrying only this fragment's payload.
    """
    return SymbolAnalysis(
        symbol=fragment.symbol,
        news_summary=fragment.news_summary,
        filing_analyses=fragment.filing_analyses,
        screening_assessment=fragment.screening_assessment or _ABSENT_ASSESSMENT,
        verdict=_ABSENT_VERDICT,
    )


def fragment_filename_error(path: Path, fragment: AnalysisFragment) -> str | None:
    """Return why the filename disagrees with the payload, or `None`.

    A fragment whose name says one thing and whose body says another is picked
    up for the wrong symbol at merge time, which the orchestrator would have to
    notice by eye. Names that do not follow `<kind>-<SYMBOL>.json` at all are
    not judged here: a fragment may legitimately be checked from an ad-hoc
    path before it is written to its contracted one.

    Args:
        path: Where the fragment was read from.
        fragment: The parsed fragment.
    """
    matched = _FILENAME_PATTERN.match(path.name)
    if matched is None:
        return None
    if matched["kind"] != fragment.kind:
        return (
            f"filename declares the {matched['kind']} expert but the payload is "
            f"{PAYLOAD_FIELD_BY_KIND[fragment.kind]}"
        )
    if matched["symbol"] != fragment.symbol:
        return (
            f"filename declares symbol {matched['symbol']!r} but the payload "
            f"declares {fragment.symbol!r}"
        )
    return None


def verify_fragment(
    analysis_input: AnalysisInput, fragment: AnalysisFragment
) -> str | None:
    """Return why this fragment would not survive ingest, or `None`.

    Identity is checked first: a fragment that does not answer this input at
    all cannot be verified against it, and reporting a provenance failure for
    it would name the wrong cause.

    Args:
        analysis_input: The exported input the fragment claims to answer.
        fragment: The parsed fragment.

    Returns:
        A single human-readable reason, phrased exactly as ingest would phrase
        it for provenance, evidence, and CON-03 failures.
    """
    candidate = next(
        (item for item in analysis_input.candidates if item.symbol == fragment.symbol),
        None,
    )
    identity = _identity_error(analysis_input, fragment, candidate)
    if identity is not None:
        return identity
    outcome = verify_symbol_analysis(
        as_symbol_analysis(fragment),
        candidate,
        calendar_source_bodies(analysis_input),
    )
    return outcome.error


def _identity_error(
    analysis_input: AnalysisInput,
    fragment: AnalysisFragment,
    candidate: CandidateInput | None,
) -> str | None:
    """Return why this fragment does not answer this input, or `None`.

    Two kinds of fragment, two keys (Issue #261). A news or screening reading
    is genuinely `as_of`-dependent, so it stays bound to the exact run whose
    `run_id` / `as_of` / `input_digest` it copied. A filing reading is not: it
    is written from the filing bodies, which are unchanged from one trading day
    to the next far more often than not, and binding it to a `run_id` forced a
    re-read of an unchanged 10-Q every single day. So a filings fragment is
    keyed on those bodies instead, and may be reused across runs.

    Nothing downstream is relaxed by that. Every fact still has to cite a
    `source_id` this input supplied for this symbol, and its `evidence_quote`
    still has to occur verbatim in the body that ID names *in this input*
    (`validate.py`). A fragment wrongly carried over from a filing whose text
    has since changed therefore fails the evidence check even if its declared
    digests were forged to match.
    """
    if fragment.kind == "filings":
        return _filing_body_error(fragment, candidate)
    checks = (
        ("run_id", str(fragment.run_id), str(analysis_input.run_id)),
        ("as_of", fragment.as_of.isoformat(), analysis_input.as_of.isoformat()),
        ("input_digest", fragment.input_digest, analysis_input.input_digest),
    )
    for name, actual, expected in checks:
        if actual != expected:
            return (
                f"{name} {actual} does not match analysis_input.json "
                f"({expected}): this fragment answers a different run"
            )
    return None


def _filing_body_error(
    fragment: AnalysisFragment, candidate: CandidateInput | None
) -> str | None:
    """Return why this filings fragment reads other bodies than this input's.

    Equality of the whole map is the contract, not per-entry containment: a
    filing this input exports but the fragment never saw is exactly as
    disqualifying as one whose text changed, and both are invisible if only
    the analyzed filings are compared.

    A symbol this input never offered yields `None`, so `verify_symbol_analysis`
    can report that as the cause rather than this function blaming the bodies.
    """
    if candidate is None:
        return None
    expected = {
        item.source_id: filing_body_digest(item.text) for item in candidate.filings
    }
    declared = fragment.filing_body_digests or {}
    if declared == expected:
        return None
    changed = sorted(
        source_id
        for source_id in declared.keys() & expected.keys()
        if declared[source_id] != expected[source_id]
    )
    return (
        "filing_body_digests do not match the filings analysis_input.json "
        f"exports for {fragment.symbol!r}: changed={changed}, "
        f"newly_exported={sorted(expected.keys() - declared.keys())}, "
        f"no_longer_exported={sorted(declared.keys() - expected.keys())}: "
        "this fragment was written from a different filing text"
    )
