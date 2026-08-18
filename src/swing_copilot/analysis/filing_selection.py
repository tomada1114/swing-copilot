"""Deterministic filing-text selection shared by daily and retrospective export."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.analysis.schemas import (
    FilingCoverage,
    FilingInput,
    FilingSectionCoverage,
    FilingSectionOmissionShape,
    FilingSelectionMode,
)
from swing_copilot.text.base import has_exhibit_loss_marker

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.analysis.schemas import FilingSectionStatus
    from swing_copilot.text.base import TextItem

_TEN_Q_FORMS = frozenset({"10-Q", "10-Q/A"})
_SECTION_TARGETS = (
    ("part_i_item_1", 50_000),
    ("part_i_item_2", 40_000),
    ("part_ii_item_1a", 20_000),
    ("part_ii_item_1", 10_000),
)
_TOTAL_SECTION_QUOTA = sum(quota for _, quota in _SECTION_TARGETS)
#: "part_ii" is checked first since "part_ii_item_1a" also starts with the
#: shorter "part_i" prefix.
_PART_II_PREFIX = "part_ii"
# A truncated section keeps its head and its tail rather than the head alone.
# The decision-relevant passages of a 10-Q sit at the end of a section: Part I
# Item 1's commitments/contingencies and legal notes follow the statements, and
# results-of-operations discussion sits past MD&A's opening overview. The
# marker is fixed-width so the kept length stays deterministic.
_SECTION_OMISSION_MARKER = "\n[... omitted middle of section ...]\n"
_SECTION_HEAD_SHARE = (3, 5)

_EIGHT_K_FORMS = frozenset({"8-K", "8-K/A"})
#: The header `data/edgar.py` writes before each appended `EX-99*` exhibit
#: (`\n\n[EXHIBIT <document_type> <document>]\n`). It is the only structure an
#: 8-K's collected text carries, so it is what exhibit-priority selection
#: splits on -- there is no `filing_sections` equivalent for this form.
_EXHIBIT_HEADER_PATTERN = re.compile(
    r"^\[EXHIBIT (?P<document_type>[^\s\]]+)[^\]]*\]$\n?", re.MULTILINE
)
_PRIMARY_PART_NAME = "exhibit_primary"
_PART_NAME_SEPARATOR_PATTERN = re.compile(r"[^a-z0-9]+")
#: Exhibit types that carry the earnings press release itself. An earnings 8-K
#: furnishes it as 99.1 (or as a bare 99 when it is the only exhibit); later
#: numbers are supplemental packages -- valuable, but not before the release.
_PRESS_RELEASE_TYPES = frozenset({"ex-99", "ex-99.1", "ex-99.01"})
#: Relative share of the budget for the primary document and the press
#: release, versus every supplemental exhibit. 4:1 keeps a supplement present
#: (HST/WELL put segment detail there) without letting it crowd out the
#: release, which is where revenue, EPS, and guidance live (Issue #165).
_EXHIBIT_PRIORITY_QUOTA = 4
_EXHIBIT_SUPPLEMENT_QUOTA = 1
#: Marks every place a lower-value passage was dropped from an exhibit, so a
#: reader never mistakes the join for continuous text. Fixed width, like
#: `_SECTION_OMISSION_MARKER`, so the kept length stays deterministic.
_PASSAGE_OMISSION_MARKER = "[... omitted lower-value exhibit passage ...]"
_BLOCK_SEPARATOR = "\n\n"
_BLANK_LINE_PATTERN = re.compile(r"(?:\n[ \t]*){2,}")
_TABLE_LINE_PREFIX = "|"
# Drop order inside one exhibit: boilerplate first, tables last. The figures
# a reader cannot reconstruct from anywhere else are in the markdown tables
# (income statement, non-GAAP reconciliation), and those sit at the *end* of a
# press release -- exactly what head-only truncation used to drop first
# (Issue #157's GOOG non-GAAP FX table). Prose sits between the two: it is
# management's commentary and guidance, worth more than a disclaimer and less
# than a statement no other source restates.
_RANK_TABLE = 0
_RANK_PROSE = 1
_RANK_BOILERPLATE = 2
#: Passages that repeat verbatim in every release of every issuer, carrying no
#: quarter-specific fact: the safe-harbor disclaimer, the call/webcast notice,
#: and the contact block.
_BOILERPLATE_PHRASES = (
    "forward-looking statement",
    "forward looking statement",
    "safe harbor",
    "private securities litigation reform act",
    "webcast",
    "conference call",
    "investor relations",
    "media relations",
    "investor contact",
    "media contact",
    "press contact",
)
#: The "About <Issuer>" corporate boilerplate heading, which carries no phrase
#: of its own to match on.
_ABOUT_HEADING_PATTERN = re.compile(r"^[#*>\s]*about\s+\S", re.IGNORECASE)

#: Order in which a symbol's per-filing budgets are allocated out of the
#: per-symbol ceiling. The earnings 8-K leads: it carries the quarter's press
#: release, the single most decision-relevant document a symbol publishes, and
#: allocating it after every 10-Q let it reach a zero budget and export as
#: `omitted_symbol_budget` (Issue #191). Only allocation order changes -- the
#: exported list stays in document order, newest first.
_TIER_EARNINGS_EIGHT_K = 0
_TIER_TEN_Q = 1
_TIER_OTHER = 2
#: Any `EX-99*` document type. Broader than `_PRESS_RELEASE_TYPES`, which ranks
#: exhibits *within* one filing: here the question is only whether the filing
#: is an earnings 8-K at all, and a supplemental 99.2 answers that too.
_EX_99_PREFIX = "ex-99"
#: Form 8-K Item 2.02, "Results of Operations and Financial Condition" -- the
#: item an issuer files its quarterly results under. Matched only in the
#: primary document, where the item list lives.
_EARNINGS_ITEM_PATTERN = re.compile(r"item\s+2\.02", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FilingTextSelection:
    """Selected text plus code-owned coverage metadata."""

    text: str
    coverage: FilingCoverage


@dataclass(frozen=True, slots=True)
class _ShapedSection:
    """One part's exported text plus the deficit its coverage must report.

    A part is a 10-Q section or an 8-K exhibit; both are shaped under one
    allocation and report the same triple.

    `exported_chars` counts only content characters -- excluding the omission
    markers, and excluding the blank lines an exhibit's surviving blocks are
    rejoined with -- so it can be compared against the part's original length.
    `omission_shape` is `None` when nothing was dropped (or when nothing was
    kept), because the shape only describes a surviving excerpt.
    """

    text: str
    exported_chars: int
    omission_shape: FilingSectionOmissionShape | None


@dataclass(frozen=True, slots=True)
class _ExhibitPart:
    """One component of a collected 8-K: its primary document or one exhibit.

    `header` is the `[EXHIBIT ...]` line introducing `body` (empty for the
    primary document); keeping the two apart lets shaping cut into the body
    while the header always survives, so an excerpt never loses which exhibit
    it came from. `quota` is the part's share of the budget and `order` its
    position in the filing, which together decide allocation.
    """

    name: str
    header: str
    body: str
    quota: int
    order: int


def select_filing_inputs(
    items: Sequence[TextItem], *, per_filing_chars: int, per_symbol_chars: int
) -> list[FilingInput]:
    """Return newest-first filing inputs under both character ceilings.

    The per-symbol ceiling is consumed in priority order -- earnings 8-K, then
    10-Q, then everything else, each tier newest first -- so a filing late in
    that order is the one that runs out of budget and exports as
    `omitted_symbol_budget`. The returned list is independent of that order:
    it is always the filings in document order, newest first.

    Args:
        items: This symbol's collected text items; non-filing sources are
            ignored.
        per_filing_chars: Ceiling on one filing's exported excerpt.
        per_symbol_chars: Ceiling on every exported excerpt for this symbol
            together.

    Returns:
        One `FilingInput` per filing, newest first.
    """
    filings = sorted(
        (item for item in items if item.source_type == "filing"),
        key=lambda item: (item.published_at, item.source_id),
        reverse=True,
    )
    form_types = [_form_type(item.title) for item in filings]
    allocation_order = sorted(
        range(len(filings)),
        key=lambda index: (
            _allocation_tier(filings[index], form_types[index]),
            index,
        ),
    )
    remaining = per_symbol_chars
    selected: dict[int, FilingInput] = {}
    for index in allocation_order:
        item = filings[index]
        form_type = form_types[index]
        selection = select_filing_text(
            item, form_type, min(per_filing_chars, remaining)
        )
        selected[index] = FilingInput(
            source_id=item.source_id,
            form_type=form_type,
            filed_at=item.published_at,
            text=selection.text,
            url=item.source_url,
            coverage=selection.coverage,
        )
        remaining -= len(selection.text)
    return [selected[index] for index in range(len(filings))]


def _allocation_tier(item: TextItem, form_type: str) -> int:
    """Rank one filing for budget allocation: earnings 8-K, 10-Q, then the rest.

    Args:
        item: The collected filing.
        form_type: Its EDGAR form type, as `_form_type` read it.

    Returns:
        The tier constant; index within a tier keeps the newest-first order.
    """
    if _is_earnings_eight_k(item, form_type):
        return _TIER_EARNINGS_EIGHT_K
    if form_type in _TEN_Q_FORMS:
        return _TIER_TEN_Q
    return _TIER_OTHER


def _is_earnings_eight_k(item: TextItem, form_type: str) -> bool:
    """Whether an 8-K plausibly carries the quarter's earnings release.

    Deliberately conservative, and deliberately built from what the collected
    text already carries rather than from a headline classifier: an 8-K is
    treated as earnings-related only when it furnishes an `EX-99*` exhibit --
    where the press release and its financial-statement tables live, which is
    the whole reason `_split_exhibit_parts` exists -- or when its primary
    document names Item 2.02, the item an issuer reports quarterly results
    under. A merger or officer-departure 8-K matches neither, so it stays in
    the trailing tier; misjudging one the other way merely allocates its
    budget slightly earlier.

    Args:
        item: The collected filing.
        form_type: Its EDGAR form type, as `_form_type` read it.

    Returns:
        `True` for an 8-K variant carrying either signal.
    """
    if form_type not in _EIGHT_K_FORMS:
        return False
    content = item.content_text
    matches = list(_EXHIBIT_HEADER_PATTERN.finditer(content))
    if any(
        match.group("document_type").lower().startswith(_EX_99_PREFIX)
        for match in matches
    ):
        return True
    # Everything before the first exhibit header is the primary document; a
    # filing with no exhibit at all is primary document throughout.
    primary_end = matches[0].start() if matches else len(content)
    return _EARNINGS_ITEM_PATTERN.search(content, 0, primary_end) is not None


def select_filing_text(
    item: TextItem, form_type: str, budget: int
) -> FilingTextSelection:
    """Select one filing under `budget`, preferring its highest-value parts.

    A 10-Q is composed from its priority sections, an 8-K from its primary
    document and its `EX-99*` exhibits (Issue #181); any other form, and any
    filing whose structure could not be recovered, falls back to the historic
    leading slice. The collected filing remains in `TextItem.content_text` for
    audit/storage: this function only shapes the copy offered to a
    qualitative-analysis context, and a parser miss is fail-soft and visible.

    Fitting the export budget is this function's job, not the collecting
    adapter's (Issue #180): a cut made here is redone on every export, whereas
    `data/edgar.py` persists what it collects. Its remaining exhibit limits --
    a character safety valve and a count cap -- are for pathological filings,
    so `content_text` is usually but not always the whole filing. The resulting
    coverage therefore reports export-stage loss (`is_truncated`) and
    collection-stage loss (`exhibit_truncated`) separately -- see
    `FilingCoverage`.
    """
    original = item.content_text
    if budget <= 0:
        return _selection("", original, "omitted_symbol_budget")
    if len(original) <= budget:
        return _selection(original, original, "full")
    if form_type in _EIGHT_K_FORMS:
        return _select_exhibit_text(original, budget)
    if form_type not in _TEN_Q_FORMS or not item.filing_sections:
        return _selection(original[:budget], original, "head_fallback")
    return _select_ten_q_text(item, budget)


def _select_ten_q_text(item: TextItem, budget: int) -> FilingTextSelection:
    """Compose a 10-Q from its priority sections under `budget`.

    Args:
        item: The collected filing, whose `filing_sections` the caller has
            already found non-empty.
        budget: Characters the exported excerpt may occupy.

    Returns:
        The section-priority selection, or the leading-slice fallback when no
        *priority* section was parsed or the budget cannot even hold the
        section headers.
    """
    original = item.content_text
    sections = {
        section.name: section.content_text.strip()
        for section in item.filing_sections
        if section.content_text.strip()
    }
    available = [
        (name, quota, sections[name])
        for name, quota in _SECTION_TARGETS
        if name in sections
    ]
    if not available:
        return _selection(original[:budget], original, "head_fallback")

    headers = {name: f"[SECTION {name}]\n" for name, _, _ in available}
    header_chars = sum(len(header) for header in headers.values()) + 2 * (
        len(headers) - 1
    )
    content_budget = max(0, budget - header_chars)
    if content_budget == 0:
        return _selection(original[:budget], original, "head_fallback")
    allocated = _allocate_section_chars(available, content_budget)
    shaped = {
        name: _shape_section(content, allocated[name]) for name, _, content in available
    }
    parts = [
        f"{headers[name]}{shaped[name].text}"
        for name, _, _ in available
        if allocated[name] > 0
    ]
    selected = "\n\n".join(parts)[:budget]
    parsed_parts = {_part_group(name) for name, _, _ in available}
    coverage = tuple(
        _section_coverage(
            name,
            sections.get(name),
            shaped.get(name),
            part_has_a_parsed_sibling=_part_group(name) in parsed_parts,
        )
        for name, _ in _SECTION_TARGETS
    )
    mode: FilingSelectionMode = (
        "section_priority"
        if all(section.status == "full" for section in coverage)
        else "section_priority_partial"
    )
    return _selection(selected, original, mode, coverage)


def _select_exhibit_text(original: str, budget: int) -> FilingTextSelection:
    """Compose an 8-K from its primary document and exhibits under `budget`.

    An earnings 8-K carries its substance in the exhibits `data/edgar.py`
    appended, and a head slice of the concatenation drops the last exhibit
    whole and the end of the first one -- which is where the financial
    statements and the non-GAAP reconciliation sit (Issue #165's HST/WELL
    filings ran 4-6x the per-filing budget). Selection therefore happens twice:
    across exhibits, where the press release outranks a supplemental package,
    and inside one, where a markdown table outlives a disclaimer.

    Args:
        original: The collected filing text, longer than `budget`.
        budget: Characters the exported excerpt may occupy.

    Returns:
        The exhibit-priority selection, or the leading-slice fallback when the
        text carries no `[EXHIBIT ...]` header (nothing to prioritize) or the
        budget cannot even hold the headers.

        The mode is always `section_priority_partial`: the parts partition
        `original` exactly, so a filing that reached this function -- longer
        than `budget` -- must lose something from at least one of them.
    """
    parts = _split_exhibit_parts(original)
    if parts is None:
        return _selection(original[:budget], original, "head_fallback")
    content_budget = budget - sum(len(part.header) for part in parts)
    if content_budget <= 0:
        return _selection(original[:budget], original, "head_fallback")
    allocated = _allocate_exhibit_chars(parts, content_budget)
    shaped = {
        part.name: _shape_exhibit(part.body, allocated[part.name]) for part in parts
    }
    selected = "".join(f"{part.header}{shaped[part.name].text}" for part in parts)
    coverage = tuple(
        FilingSectionCoverage(
            name=part.name,
            status=(
                "full"
                if shaped[part.name].exported_chars >= len(part.body)
                else "partial"
            ),
            original_chars=len(part.body),
            exported_chars=shaped[part.name].exported_chars,
            omission_shape=shaped[part.name].omission_shape,
        )
        for part in parts
    )
    return _selection(selected[:budget], original, "section_priority_partial", coverage)


def _split_exhibit_parts(content: str) -> list[_ExhibitPart] | None:
    """Split collected 8-K text into its primary document and exhibits.

    The parts partition `content` exactly -- each exhibit body runs to the next
    header, and the primary part is everything before the first one -- so the
    reported per-part character counts add up to `FilingCoverage`'s.

    Args:
        content: The collected filing text.

    Returns:
        The parts in document order, or `None` when no `[EXHIBIT ...]` header
        is present: an 8-K whose exhibits were never fetched has no structure
        to prioritize, and falls back to the historic leading slice.
    """
    matches = list(_EXHIBIT_HEADER_PATTERN.finditer(content))
    if not matches:
        return None
    parts: list[_ExhibitPart] = []
    primary_body = content[: matches[0].start()]
    used = {_PRIMARY_PART_NAME}
    if primary_body:
        parts.append(
            _ExhibitPart(
                name=_PRIMARY_PART_NAME,
                header="",
                body=primary_body,
                quota=_EXHIBIT_PRIORITY_QUOTA,
                order=0,
            )
        )
    document_types = [match.group("document_type").lower() for match in matches]
    # An exhibit set with no recognizable press-release number still has one
    # leading exhibit, and it is the closest thing to the release there is.
    has_press_release = any(kind in _PRESS_RELEASE_TYPES for kind in document_types)
    for ordinal, match in enumerate(matches):
        body_end = (
            matches[ordinal + 1].start() if ordinal + 1 < len(matches) else len(content)
        )
        is_priority = document_types[ordinal] in _PRESS_RELEASE_TYPES or (
            not has_press_release and ordinal == 0
        )
        parts.append(
            _ExhibitPart(
                name=_exhibit_part_name(document_types[ordinal], used),
                header=match.group(),
                body=content[match.end() : body_end],
                quota=(
                    _EXHIBIT_PRIORITY_QUOTA
                    if is_priority
                    else _EXHIBIT_SUPPLEMENT_QUOTA
                ),
                order=len(parts),
            )
        )
    return parts


def _exhibit_part_name(document_type: str, used: set[str]) -> str:
    """Return a stable coverage name for one exhibit, unique within a filing.

    Args:
        document_type: The exhibit's EDGAR document type, lowercased
            (`ex-99.1`).
        used: Names already taken by earlier parts of the same filing. Updated
            in place, because a filing may furnish two exhibits of the same
            type and coverage names must stay distinct.

    Returns:
        `exhibit_ex_99_1` for `ex-99.1`, suffixed with `_2`, `_3`, ... on a
        repeat.
    """
    base = f"exhibit_{_PART_NAME_SEPARATOR_PATTERN.sub('_', document_type).strip('_')}"
    name, ordinal = base, 1
    while name in used:
        ordinal += 1
        name = f"{base}_{ordinal}"
    used.add(name)
    return name


def _allocate_exhibit_chars(
    parts: Sequence[_ExhibitPart], budget: int
) -> dict[str, int]:
    """Allocate scaled quotas, then hand the slack out in priority order.

    Deliberately unlike `_allocate_section_chars`, which redistributes by
    shortage ratio: a supplemental package is routinely several times the size
    of the press release, so "most under-served first" would hand the slack to
    the supplement and invert the priority this function exists to enforce.
    Priority parts (the primary document and the press release) therefore take
    all they can use before any supplement gets a character beyond its quota,
    and document order breaks ties inside a tier.

    Args:
        parts: The filing's parts in document order.
        budget: Characters available for part bodies, headers already paid for.

    Returns:
        Characters allocated per part name, summing to at most `budget`.
    """
    total_quota = sum(part.quota for part in parts)
    allocated = {
        part.name: min(len(part.body), budget * part.quota // total_quota)
        for part in parts
    }
    remaining = budget - sum(allocated.values())
    for part in sorted(parts, key=lambda part: (-part.quota, part.order)):
        if remaining <= 0:
            break
        extra = min(len(part.body) - allocated[part.name], remaining)
        allocated[part.name] += extra
        remaining -= extra
    return allocated


def _shape_exhibit(body: str, allocated: int) -> _ShapedSection:
    """Return `allocated` characters of `body`, keeping its most useful parts.

    The exhibit is cut at blank lines into blocks and the blocks are kept in
    value order (tables, prose, boilerplate) until the allocation is full, then
    rejoined in document order with every gap marked. A block that does not fit
    is skipped rather than ending the pass, so a long disclaimer cannot hide a
    short table behind it.

    Args:
        body: One part's full text, header excluded.
        allocated: Characters this part may occupy, markers included.

    Returns:
        `body` unchanged when it fits. Otherwise the surviving blocks, or --
        when not even the highest-value block fits, as for an exhibit that has
        no blank lines to cut at -- a leading slice, which the shape reports as
        `head_only` exactly as `_shape_section` does.
    """
    if allocated >= len(body):
        return _ShapedSection(body, len(body), None)
    blocks = [block for block in _BLANK_LINE_PATTERN.split(body) if block.strip()]
    kept = _select_exhibit_blocks(blocks, allocated)
    if not kept:
        sliced = body[:allocated]
        return _ShapedSection(sliced, len(sliced), "head_only" if sliced else None)
    return _ShapedSection(
        _BLOCK_SEPARATOR.join(_assemble_blocks(blocks, kept)),
        sum(len(blocks[index]) for index in kept),
        "value_selected",
    )


def _select_exhibit_blocks(blocks: Sequence[str], allocated: int) -> list[int]:
    """Return the indexes to keep, highest value first, within `allocated`.

    Args:
        blocks: The exhibit's blocks in document order.
        allocated: Characters the assembled result may occupy, including the
            omission markers its gaps will need.

    Returns:
        The kept indexes in ascending order, empty when nothing fits. Ties in
        value break on document order, so the result depends on the input
        alone.
    """
    order = sorted(
        range(len(blocks)), key=lambda index: (_block_rank(blocks[index]), index)
    )
    kept: set[int] = set()
    chars = 0
    # Nothing kept yet, so the whole exhibit is one omitted run: one marker.
    markers = 1
    for index in order:
        candidate_markers = markers + _marker_delta(kept, index, len(blocks))
        candidate_chars = chars + len(blocks[index])
        if (
            _assembled_length(candidate_chars, len(kept) + 1, candidate_markers)
            <= allocated
        ):
            kept.add(index)
            chars, markers = candidate_chars, candidate_markers
    return sorted(kept)


def _marker_delta(kept: set[int], index: int, block_count: int) -> int:
    """Return how keeping `index` changes the number of omitted runs.

    Markers are counted per omitted run, edges included, so the count moves
    with the run `index` is being taken out of: a lone omitted block closes
    its run, a block inside a longer run splits it in two, and a block at
    either end of one merely shortens it.
    """
    left_omitted = index > 0 and index - 1 not in kept
    right_omitted = index + 1 < block_count and index + 1 not in kept
    if left_omitted and right_omitted:
        return 1
    if left_omitted or right_omitted:
        return 0
    return -1


def _assemble_blocks(blocks: Sequence[str], kept: Sequence[int]) -> list[str]:
    """Return the kept blocks in document order, with each gap marked."""
    pieces: list[str] = []
    previous = -1
    for index in kept:
        if index != previous + 1:
            pieces.append(_PASSAGE_OMISSION_MARKER)
        pieces.append(blocks[index])
        previous = index
    if previous != len(blocks) - 1:
        pieces.append(_PASSAGE_OMISSION_MARKER)
    return pieces


def _assembled_length(chars: int, block_count: int, markers: int) -> int:
    """Return how long `_assemble_blocks` would make a selection.

    Counted rather than built, because the selection pass asks once per
    candidate block and an exhibit can run to hundreds of thousands of
    characters. Markers and the blank lines joining every piece are part of
    the length: they are what the allocation has to pay for, even though they
    are not content.
    """
    return (
        chars
        + markers * len(_PASSAGE_OMISSION_MARKER)
        + len(_BLOCK_SEPARATOR) * (block_count + markers - 1)
    )


def _block_rank(block: str) -> int:
    """Rank one block by export value: tables first, boilerplate last."""
    if _is_table_block(block):
        return _RANK_TABLE
    if _is_boilerplate_block(block):
        return _RANK_BOILERPLATE
    return _RANK_PROSE


def _is_table_block(block: str) -> bool:
    """Whether a block reads as a markdown table.

    A majority of pipe-prefixed lines, rather than all of them, so a caption or
    a footnote sharing the block does not disqualify the table it belongs to.
    `data/edgar.py` converts exhibits to markdown precisely so these keep every
    digit (Issue #156), which is what makes them worth protecting here.
    """
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    pipe_lines = sum(line.startswith(_TABLE_LINE_PREFIX) for line in lines)
    return bool(lines) and 2 * pipe_lines > len(lines)


def _is_boilerplate_block(block: str) -> bool:
    """Whether a block is issuer boilerplate rather than quarter-specific text."""
    lowered = block.lower()
    return any(phrase in lowered for phrase in _BOILERPLATE_PHRASES) or bool(
        _ABOUT_HEADING_PATTERN.match(block)
    )


def _part_group(name: str) -> str:
    """Return the Part-level group ("part_i" or "part_ii") a section belongs to."""
    return "part_ii" if name.startswith(_PART_II_PREFIX) else "part_i"


def _section_coverage(
    name: str,
    content: str | None,
    piece: _ShapedSection | None,
    *,
    part_has_a_parsed_sibling: bool,
) -> FilingSectionCoverage:
    """Report one priority section's status together with its deficit.

    Args:
        name: The priority section's canonical name.
        content: The parsed section text, or `None` when the parser found no
            such section in this filing.
        piece: What `_shape_section` kept of `content`, paired with `content`.
        part_has_a_parsed_sibling: Whether another priority section in the
            same Part (`part_i_*` / `part_ii_*`) was parsed. Only consulted
            when this section itself was not (P8-122).

    Returns:
        Coverage carrying character counts and an omission shape whenever the
        section existed. When it did not: `absent_from_filing` if the Part's
        structure is otherwise readable (a sibling parsed, so the section
        itself is likely genuinely not in the filing), else `not_parsed`
        (the Part's structure was not recovered at all, so presence is
        undetermined). Neither carries character counts -- there is no
        original length to report.
    """
    if content is None or piece is None:
        status: FilingSectionStatus = (
            "absent_from_filing" if part_has_a_parsed_sibling else "not_parsed"
        )
        return FilingSectionCoverage(name=name, status=status)
    return FilingSectionCoverage(
        name=name,
        status="full" if piece.exported_chars >= len(content) else "partial",
        original_chars=len(content),
        exported_chars=piece.exported_chars,
        omission_shape=piece.omission_shape,
    )


def _shape_section(content: str, allocated: int) -> _ShapedSection:
    """Return `allocated` characters of `content`, keeping its head and tail.

    Head-only truncation silently dropped whatever sat at the end of a section,
    which is where a 10-Q puts the passages this project cares most about
    (commitments/contingencies and legal notes at the end of Part I Item 1,
    results-of-operations discussion past MD&A's opening overview). Keeping
    both ends costs the middle instead, and the omission is marked inline so a
    reader never mistakes the join for continuous text.

    Args:
        content: The full section text.
        allocated: Characters this section may occupy, marker included.

    Returns:
        `content` unchanged when it fits, otherwise its head and tail joined by
        `_SECTION_OMISSION_MARKER`, exactly `allocated` characters long. A
        section too short to hold the marker plus a tail degrades to a leading
        slice, which the shape reports as `head_only`.
    """
    if allocated >= len(content):
        return _ShapedSection(content, len(content), None)
    head_share, total_share = _SECTION_HEAD_SHARE
    kept = allocated - len(_SECTION_OMISSION_MARKER)
    head = kept * head_share // total_share
    tail = kept - head
    if kept <= 0 or tail <= 0:
        sliced = content[:allocated]
        return _ShapedSection(sliced, len(sliced), "head_only" if sliced else None)
    return _ShapedSection(
        f"{content[:head]}{_SECTION_OMISSION_MARKER}{content[len(content) - tail :]}",
        kept,
        "head_and_tail",
    )


def _allocate_section_chars(
    available: list[tuple[str, int, str]], budget: int
) -> dict[str, int]:
    """Allocate scaled minimum quotas, then reuse slack by shortage ratio.

    The redistribution pass (P8-122) visits sections in descending
    `len(content) / max(1, allocated[name])` -- how many times over its
    current allocation a section's content actually runs -- so a heavily
    under-served section (e.g. a Part II item at 7x its scaled quota) gets
    the leftover before one only slightly short. Ties break on ascending
    section name for determinism. `max(1, ...)` avoids a zero division when
    a scaled quota floors to 0 on a very small budget.
    """
    allocated: dict[str, int] = {}
    contents: dict[str, str] = {}
    for name, quota, content in available:
        scaled_quota = budget * quota // _TOTAL_SECTION_QUOTA
        allocated[name] = min(len(content), scaled_quota)
        contents[name] = content

    remaining = budget - sum(allocated.values())
    order = sorted(
        contents,
        key=lambda name: (-(len(contents[name]) / max(1, allocated[name])), name),
    )
    for name in order:
        if remaining <= 0:
            break
        extra = min(len(contents[name]) - allocated[name], remaining)
        allocated[name] += extra
        remaining -= extra
    return allocated


def _selection(
    text: str,
    original: str,
    mode: FilingSelectionMode,
    sections: tuple[FilingSectionCoverage, ...] = (),
) -> FilingTextSelection:
    """Pair `text` with the coverage that describes what it left behind.

    Args:
        text: The excerpt offered to the analysis context.
        original: The collected filing text `text` was cut from. Passed whole
            rather than as a length because the collection-stage loss signals
            are markers *inside* it (Issues #157/#163); reading it here keeps
            every branch of `select_filing_text` from having to remember to
            report them.
        mode: How `text` was chosen.
        sections: Per-section coverage, empty outside section-priority mode.
    """
    return FilingTextSelection(
        text=text,
        coverage=FilingCoverage(
            original_chars=len(original),
            exported_chars=len(text),
            is_truncated=len(text) < len(original),
            selection_mode=mode,
            # Detected on the collected text, not on `text`: a head slice can
            # drop the trailing marker, and the exhibit loss it reports is a
            # property of the filing as collected either way.
            exhibit_truncated=has_exhibit_loss_marker(original),
            sections=list(sections),
        ),
    )


def _form_type(title: str | None) -> str:
    return (title or "unknown").split(" - ")[0]
