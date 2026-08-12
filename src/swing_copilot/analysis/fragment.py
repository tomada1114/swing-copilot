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

from pydantic import BaseModel, ConfigDict, model_validator

from swing_copilot.analysis.schemas import (
    FilingAnalysis,
    NewsSummary,
    NonBlankText,
    ScreeningAssessment,
    Sha256Digest,
    SymbolAnalysis,
    Verdict,
)
from swing_copilot.analysis.validate import (
    calendar_source_bodies,
    verify_symbol_analysis,
)

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.analysis.schemas import AnalysisInput

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


class AnalysisFragment(BaseModel):
    """One expert's `analysis_work/<kind>-<SYMBOL>.json`.

    The three identity fields are copied verbatim from `analysis_input.json`;
    they are what lets a later run tell a reusable fragment from yesterday's
    leftovers. `ac_check` is the expert's own AC self-check declaration: it is
    free text and no machine reads its content, but its absence means the
    self-check was never declared, so it is required rather than optional.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    as_of: date
    input_digest: Sha256Digest
    symbol: NonBlankText
    ac_check: NonBlankText
    news_summary: NewsSummary | None = None
    filing_analyses: list[FilingAnalysis] = []
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

    Identity is checked first: a fragment bound to another run cannot be
    verified against this input at all, and reporting a provenance failure for
    it would name the wrong cause.

    Args:
        analysis_input: The exported input the fragment claims to answer.
        fragment: The parsed fragment.

    Returns:
        A single human-readable reason, phrased exactly as ingest would phrase
        it for provenance, evidence, and CON-03 failures.
    """
    identity = _identity_error(analysis_input, fragment)
    if identity is not None:
        return identity
    candidate = next(
        (item for item in analysis_input.candidates if item.symbol == fragment.symbol),
        None,
    )
    outcome = verify_symbol_analysis(
        as_symbol_analysis(fragment),
        candidate,
        calendar_source_bodies(analysis_input),
    )
    return outcome.error


def _identity_error(
    analysis_input: AnalysisInput, fragment: AnalysisFragment
) -> str | None:
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
