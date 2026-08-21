"""Deterministic per-expert input slices cut from `analysis_input.json` (#260).

The `swing-daily` orchestrator does not hand a whole `analysis_input.json` to a
subagent: a 1.4 MB document would be re-read once per expert and once per
symbol, and the filings text of unrelated candidates would fill contexts that
never look at it. So each expert gets a read-only *slice* -- the run identity,
the run-wide context that expert actually reads, and one candidate's own
fields.

Until now the orchestrator cut those slices by hand, 21 of them in the
2026-08-13 run, at a measured 5.2 minutes and with no defence against a
dropped or duplicated source. This module makes the cut a deterministic
function of the input instead.

Two properties carry the contract:

* **Verbatim.** Every value is copied out of the *document's own JSON*, not
  re-serialized from a parsed model, so `source_id`s and bodies reach the
  expert byte for byte after a filing's ordered `text_chunks` are joined, and
  the provenance check at ingest still compares the same strings.
* **Byte-stable.** The same input always produces the same files: fixed
  top-level key order, verbatim nested order, UTF-8, LF, one trailing newline,
  and nothing environment-dependent (no wall clock, no paths, no host) in the
  payload. Issue #261 hashes these bodies to decide reuse, which only works if
  an unchanged input cannot produce a different byte.

The grouping reproduces what `.claude/skills/swing-daily/SKILL.md` Step 2
already assigns: `news` for candidates that have news, `filings` for those that
have filings, `screening` for every candidate, with the run-wide context blocks
going to the screening expert alone -- it is the only one whose skill reads
them.

A filings slice additionally carries `filing_body_digests`, the only value in
any slice that is computed rather than copied. It digests the bodies the slice
itself hands over, and the expert copies it into its fragment as the key that
decides whether a later run may reuse that reading (Issue #261). The expert
cannot compute it -- the workflow forbids hand-rolled contract scripts (Issue
#132) -- so the deterministic step that already reads every body is where it
belongs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from swing_copilot.analysis.fragment import PAYLOAD_FIELD_BY_KIND, FragmentKind
from swing_copilot.analysis.schemas import (
    AnalysisInput,
    CalendarEventInput,
    FilingCoverage,
    NewsInput,
    NewsSupply,
    NonBlankText,
    Sha256Digest,
    SourceId,
    filing_body_digest,
)
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.io_atomic import write_json_batch_atomically

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from swing_copilot.analysis.schemas import CandidateInput

__all__ = [
    "MAX_FILING_SLICE_TEXT_LINE_CHARS",
    "SLICE_FILENAME_PREFIX",
    "InputSlice",
    "SliceCandidate",
    "SliceContext",
    "SliceDocument",
    "SliceExportError",
    "build_slices",
    "write_slices",
]

#: `slice-<kind>-<SYMBOL>.json`. Prefixed so a slice can never be mistaken for
#: an `analysis_work/<kind>-<SYMBOL>.json` fragment: the two documents share a
#: naming shape but not a schema, and only fragments are merged into a result.
SLICE_FILENAME_PREFIX: Final = "slice"

#: The Read tool is line-oriented and caps a single line at roughly 25,000
#: tokens. Keep each serialized filing-text value far below that cap, including
#: JSON escaping overhead, while leaving the original `analysis_input.json`
#: string untouched for provenance and digest checks.
MAX_FILING_SLICE_TEXT_LINE_CHARS: Final = 8_000
_MAX_FILING_SLICE_TEXT_JSON_CHARS: Final = MAX_FILING_SLICE_TEXT_LINE_CHARS - 16
_MAX_FILING_SLICE_TEXT_CHUNK_CHARS: Final = 8_000

#: A symbol becomes part of a filename, so it may not carry a path separator,
#: a leading dash, or anything else that would resolve elsewhere.
_SAFE_SYMBOL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: Which `candidates[]` keys each expert's slice carries. Copying a key the
#: expert does not read would put another symbol's budget, or a long filing
#: body, into a context that has no use for it.
_CANDIDATE_KEYS_BY_KIND: Final[dict[FragmentKind, tuple[str, ...]]] = {
    "news": ("news", "news_supply"),
    "filings": ("filings",),
    "screening": ("score_breakdown", "risk_constraints", "prior_verdicts"),
}

#: The subset of the above that every parsable input carries, and that a slice
#: of that kind therefore must too. `CandidateInput` gives these fields no
#: default, so a document missing one never reaches slicing at all -- which is
#: precisely why they belong here: this is the check that catches the *slicing*
#: dropping a field. Only `news_supply` and `prior_verdicts` stay optional:
#: both were added after `analysis-input-v3` was frozen, so an archived input
#: can legitimately lack them, and a slice must not invent a key its input
#: never had.
_REQUIRED_CANDIDATE_KEYS_BY_KIND: Final[dict[FragmentKind, frozenset[str]]] = {
    "news": frozenset({"news"}),
    "filings": frozenset({"filings"}),
    "screening": frozenset({"score_breakdown", "risk_constraints"}),
}

#: Run-wide context per expert. Only the screening skill reads these blocks
#: (`interpret-screening/SKILL.md`), which is also how SKILL.md Step 2 assigns
#: them; the news and filings experts get an empty context object rather than
#: an absent one, so the slice states that nothing run-wide was withheld by
#: accident.
_CONTEXT_KEYS_BY_KIND: Final[dict[FragmentKind, tuple[str, ...]]] = {
    "news": (),
    "filings": (),
    "screening": ("market_regime", "calendar_events"),
}


class SliceExportError(SwingCopilotError):
    """Raised when slices cannot be cut from the given document at all."""


class _StrictModel(BaseModel):
    """Reject unknown fields, like both directions of the analysis contract."""

    model_config = ConfigDict(extra="forbid")


class SliceContext(_StrictModel):
    """The run-wide blocks one expert is given, or nothing at all."""

    market_regime: str | None = None
    calendar_events: list[CalendarEventInput] = []


class SliceFiling(_StrictModel):
    """One filing in a transport slice, with its body split into short lines.

    `analysis_input.json` continues to carry `FilingInput.text` as one string.
    A slice is a separate work artifact read through a line-oriented tool, so
    it carries the same body as ordered `text_chunks`; joining them without a
    separator recreates the exported filing exactly.
    """

    source_id: SourceId
    form_type: str
    filed_at: datetime
    text_chunks: list[str]
    url: str
    coverage: FilingCoverage | None = None

    @model_validator(mode="after")
    def _verify_text_chunks(self) -> Self:
        """Reject an empty or lossy transport body before it reaches a skill."""
        if not self.text_chunks or any(not chunk for chunk in self.text_chunks):
            msg = "text_chunks must contain at least one non-empty chunk"
            raise ValueError(msg)
        if not "".join(self.text_chunks).strip():
            msg = "text_chunks must preserve a non-blank filing body"
            raise ValueError(msg)
        return self


class SliceCandidate(_StrictModel):
    """One candidate's fields, restricted to what the expert analyzes.

    Every field but `symbol` is optional because which ones are present *is*
    the grouping: `InputSlice` then requires exactly the set its `kind` owns,
    so a slice carrying a stray filing body fails validation instead of
    reaching a subagent.
    """

    symbol: NonBlankText
    score_breakdown: str | None = None
    risk_constraints: str | None = None
    prior_verdicts: str | None = None
    news: list[NewsInput] | None = None
    news_supply: NewsSupply | None = None
    filings: list[SliceFiling] | None = None


class InputSlice(_StrictModel):
    """One `slice-<kind>-<SYMBOL>.json`: one expert, one symbol, one run.

    The three identity fields are verbatim copies of `analysis_input.json`, so
    a subagent can prove its slice answers the same run before analyzing it and
    can copy them into the fragment it writes. `input_digest` stays the *whole
    input's* digest and is never recomputed over the slice.
    """

    run_id: UUID
    as_of: date
    input_digest: Sha256Digest
    kind: FragmentKind
    context: SliceContext
    candidate: SliceCandidate
    #: Only on a filings slice: `source_id` -> `filing_body_digest(text)` for
    #: every body this slice carries. The filings expert copies the map
    #: verbatim into its fragment, where it becomes the key that decides
    #: whether a later run may reuse the reading (Issue #261). It is computed
    #: rather than copied -- the input document has no such field -- but it is
    #: a pure function of bodies that *are* copied verbatim, so the slice stays
    #: byte-stable.
    filing_body_digests: dict[SourceId, Sha256Digest] | None = None

    @model_validator(mode="after")
    def _verify_payload_matches_kind(self) -> Self:
        """Hold the slice to exactly the fields its expert is assigned.

        Presence is judged by what the document set, not by the value: a
        candidate legitimately has `prior_verdicts: null`, and dropping that
        key would be a different statement than carrying it.
        """
        provided = self.candidate.model_fields_set - {"symbol"}
        allowed = set(_CANDIDATE_KEYS_BY_KIND[self.kind])
        unexpected = sorted(provided - allowed)
        if unexpected:
            msg = (
                f"a {self.kind} slice must not carry candidate fields "
                f"{', '.join(unexpected)}"
            )
            raise ValueError(msg)
        missing = sorted(_REQUIRED_CANDIDATE_KEYS_BY_KIND[self.kind] - provided)
        if missing:
            msg = (
                f"a {self.kind} slice must carry candidate fields {', '.join(missing)}"
            )
            raise ValueError(msg)
        context_keys = sorted(
            self.context.model_fields_set - set(_CONTEXT_KEYS_BY_KIND[self.kind])
        )
        if context_keys:
            msg = (
                f"a {self.kind} slice must not carry run-wide context "
                f"{', '.join(context_keys)}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _verify_filing_body_digests(self) -> Self:
        """Hold the reuse keys to exactly the bodies this slice hands over.

        The expert copies this map into its fragment and never recomputes it,
        so a map that named a body the slice does not carry would let a reading
        claim coverage it never had -- and the claim would be checked against
        the *input*, which agrees with the map rather than with the slice.
        """
        if self.kind != "filings":
            if self.filing_body_digests is not None:
                msg = f"a {self.kind} slice must not carry filing_body_digests"
                raise ValueError(msg)
            return self
        expected = {
            item.source_id: filing_body_digest("".join(item.text_chunks))
            for item in self.candidate.filings or []
        }
        if self.filing_body_digests != expected:
            msg = (
                "filing_body_digests must be the digest of every filing body "
                "this slice carries, and of nothing else"
            )
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class SliceDocument:
    """One slice, ready to be written.

    `source_chars` counts the characters of the reconstructed bodies this slice
    actually hands the expert, so the orchestrator can bin-pack symbols against
    SKILL.md Step 2's per-agent ceiling without opening the files.
    """

    kind: FragmentKind
    symbol: str
    # Any: the verbatim JSON sub-objects copied out of the input document;
    # their shape is already proven by `AnalysisInput` and `InputSlice`.
    payload: dict[str, Any]
    source_chars: int

    @property
    def filename(self) -> str:
        """The slice's contracted `slice-<kind>-<SYMBOL>.json` name."""
        return f"{SLICE_FILENAME_PREFIX}-{self.kind}-{self.symbol}.json"


def build_slices(payload: Mapping[str, Any]) -> tuple[SliceDocument, ...]:
    """Cut every (expert x symbol) slice out of one analysis input document.

    Args:
        payload: The decoded `analysis_input.json`, exactly as it sits on disk.

    Returns:
        The slices, ordered by expert (news, filings, screening) and, within an
        expert, by the input's own candidate order. A candidate with no news
        gets no news slice, and one with no filings gets no filings slice;
        every candidate gets a screening slice, because a screening assessment
        is required for every symbol.

    Raises:
        SliceExportError: The document is not a valid `analysis_input.json`, a
            symbol is unusable as a filename, two symbols would claim the same
            file, or an assembled slice violates its own strict schema.
    """
    source = _SliceSource(_validated_input(payload), payload)
    documents: list[SliceDocument] = []
    for kind in PAYLOAD_FIELD_BY_KIND:
        documents.extend(
            _slice_document(kind, candidate, source)
            for candidate in source.candidates()
            if _has_work(kind, candidate.parsed)
        )
    _verify_distinct_filenames(documents)
    return tuple(documents)


def _verify_distinct_filenames(documents: Sequence[SliceDocument]) -> None:
    """Fail before two slices can land on one file.

    `AnalysisInput` only rejects symbols that repeat exactly, and this project
    runs on a case-insensitive filesystem: `aapl` and `AAPL` would resolve to
    the same path, and the second write would silently replace the first
    expert's material with another symbol's.
    """
    seen: dict[str, str] = {}
    for document in documents:
        claimed = seen.setdefault(document.filename.lower(), document.symbol)
        if claimed != document.symbol:
            msg = (
                f"symbols {claimed!r} and {document.symbol!r} would both write "
                f"{document.filename}"
            )
            raise SliceExportError(msg)


def write_slices(
    documents: Sequence[SliceDocument], out_dir: str | Path
) -> tuple[Path, ...]:
    """Write the whole set of slices into `out_dir`, or none of it.

    The set is written as one logical write (`write_json_batch_atomically`),
    not slice by slice. A command that reports failure must not have left a
    partial set behind in a directory this workflow never cleans up: the
    orchestrator reads "no output" as "nothing was produced", and stale slices
    from a half-finished run are exactly what an unattended session cannot
    notice.

    Args:
        documents: The slices to write, as returned by `build_slices`.
        out_dir: Destination directory, created when absent. It must not be the
            run directory or its ancestors. The CI-only workflow uses the
            ignored `.swing-daily-scratch` sibling, so slices never sit beside
            report fragments and are discarded with the ephemeral runner.

    Returns:
        The resolved absolute paths, in `documents` order.

    Raises:
        OSError: Writing failed. No destination was changed and no temporary
            artifact remains (see `write_json_batch_atomically` for the one
            residual case, a failure during the rename phase).
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destinations = [directory / document.filename for document in documents]
    write_json_batch_atomically(
        [
            (destination, document.payload)
            for destination, document in zip(destinations, documents, strict=True)
        ]
    )
    return tuple(destination.resolve() for destination in destinations)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One candidate in both forms slicing needs: parsed, and as written."""

    parsed: CandidateInput
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _SliceSource:
    """The input document, parsed for decisions and raw for copying."""

    analysis_input: AnalysisInput
    payload: Mapping[str, Any]

    def candidates(self) -> tuple[_Candidate, ...]:
        """Pair each parsed candidate with the object it was parsed from."""
        return tuple(
            _Candidate(parsed, raw)
            for parsed, raw in zip(
                self.analysis_input.candidates,
                self.payload["candidates"],
                strict=True,
            )
        )

    def identity(self) -> dict[str, Any]:
        """The three run-identity values, verbatim, in their fixed order."""
        return {
            "run_id": self.payload["run_id"],
            "as_of": self.payload["as_of"],
            "input_digest": self.payload["input_digest"],
        }

    def context_for(self, kind: FragmentKind) -> dict[str, Any]:
        """The run-wide blocks this expert reads, verbatim."""
        raw_context: Mapping[str, Any] = self.payload["context"]
        return {
            key: raw_context[key]
            for key in _CONTEXT_KEYS_BY_KIND[kind]
            if key in raw_context
        }


def _validated_input(payload: Mapping[str, Any]) -> AnalysisInput:
    """Parse the document strictly before anything is cut out of it."""
    try:
        return AnalysisInput.model_validate(payload)
    except ValidationError as exc:
        msg = f"analysis input failed schema validation:\n{exc}"
        raise SliceExportError(msg) from exc


def _has_work(kind: FragmentKind, candidate: CandidateInput) -> bool:
    """Whether this expert has anything to read for this candidate.

    A candidate with an empty `news[]` gets no news slice, even though its
    `news_supply` record would say *why* the news is thin (Issue #130):
    `analyze-news/SKILL.md` and AC14 require the expert to write
    `news_summary: null` whenever the news is empty, so an agent launched for
    that symbol would declare nothing and the run would pay for one subagent
    per newsless symbol. Carrying the supply record through to the report
    means changing the expert's contract, not the slicing, and is tracked
    separately.
    """
    if kind == "news":
        return bool(candidate.news)
    if kind == "filings":
        return bool(candidate.filings)
    return True


def _slice_document(
    kind: FragmentKind, candidate: _Candidate, source: _SliceSource
) -> SliceDocument:
    """Assemble and validate one slice, copying every value verbatim."""
    symbol = candidate.parsed.symbol
    if _SAFE_SYMBOL.fullmatch(symbol) is None:
        msg = f"symbol {symbol!r} cannot be used in a slice filename"
        raise SliceExportError(msg)
    candidate_payload: dict[str, Any] = {"symbol": candidate.raw["symbol"]}
    for key in _CANDIDATE_KEYS_BY_KIND[kind]:
        if key not in candidate.raw:
            continue
        if kind == "filings":
            candidate_payload[key] = [
                _chunk_filing(filing) for filing in candidate.raw[key]
            ]
        else:
            candidate_payload[key] = candidate.raw[key]
    slice_payload: dict[str, Any] = {
        **source.identity(),
        "kind": kind,
        "context": source.context_for(kind),
        "candidate": candidate_payload,
    }
    if kind == "filings":
        slice_payload["filing_body_digests"] = {
            item.source_id: filing_body_digest(item.text)
            for item in candidate.parsed.filings
        }
    try:
        InputSlice.model_validate(slice_payload)
    except ValidationError as exc:
        msg = f"{kind} slice for {symbol} failed schema validation:\n{exc}"
        raise SliceExportError(msg) from exc
    return SliceDocument(
        kind=kind,
        symbol=symbol,
        payload=slice_payload,
        source_chars=_source_chars(kind, candidate.parsed, source.payload["context"]),
    )


def _chunk_filing(filing: Mapping[str, Any]) -> dict[str, Any]:
    """Replace one raw filing body with an ordered, line-safe chunk list."""
    chunked: dict[str, Any] = {}
    for key, value in filing.items():
        chunked["text_chunks" if key == "text" else key] = (
            _split_filing_text(value) if key == "text" else value
        )
    return chunked


def _split_filing_text(text: str) -> list[str]:
    """Split a body without changing the text after chunk concatenation.

    The limit is measured against the JSON-escaped string, not only the source
    character count, because quotes, backslashes, and control characters grow
    during serialization. A binary search keeps this cheap for 120,000-character
    filings while guaranteeing every emitted array item stays below the line
    budget.
    """
    chunks: list[str] = []
    start = 0
    while start < len(text):
        low = start + 1
        high = min(len(text), start + _MAX_FILING_SLICE_TEXT_CHUNK_CHARS)
        best = start
        while low <= high:
            middle = (low + high) // 2
            candidate = text[start:middle]
            encoded_length = len(json.dumps(candidate, ensure_ascii=False))
            if encoded_length <= _MAX_FILING_SLICE_TEXT_JSON_CHARS:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best == start:
            msg = "could not create a line-safe chunk from filing text"
            raise SliceExportError(msg)
        chunks.append(text[start:best])
        start = best
    return chunks


def _source_chars(
    kind: FragmentKind, candidate: CandidateInput, raw_context: Mapping[str, Any]
) -> int:
    """Count the characters of the bodies this slice hands its expert.

    Only the text the expert has to read counts -- JSON punctuation, URLs and
    identifiers do not -- because the number exists to be compared against
    SKILL.md Step 2's per-agent character ceiling, which is stated over
    the reconstructed `filings[].text_chunks` body.
    """
    if kind == "news":
        return sum(
            len(item.headline or "") + len(item.summary) for item in candidate.news
        )
    if kind == "filings":
        return sum(len(item.text) for item in candidate.filings)
    blocks = (
        candidate.score_breakdown,
        candidate.risk_constraints,
        candidate.prior_verdicts,
        raw_context.get("market_regime"),
    )
    events: Sequence[Mapping[str, Any]] = raw_context.get("calendar_events", [])
    return sum(len(block or "") for block in blocks) + sum(
        len(event.get("title") or "") + len(event["summary"]) for event in events
    )
