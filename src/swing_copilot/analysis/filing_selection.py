"""Deterministic filing-text selection shared by daily and retrospective export."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.analysis.schemas import (
    FilingCoverage,
    FilingInput,
    FilingSectionCoverage,
    FilingSectionOmissionShape,
    FilingSelectionMode,
)

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


@dataclass(frozen=True, slots=True)
class FilingTextSelection:
    """Selected text plus code-owned coverage metadata."""

    text: str
    coverage: FilingCoverage


@dataclass(frozen=True, slots=True)
class _ShapedSection:
    """One section's exported text plus the deficit its coverage must report.

    `exported_chars` counts only section characters, excluding the omission
    marker, so it can be compared against the section's original length.
    `omission_shape` is `None` when nothing was dropped (or when nothing was
    kept), because the shape only describes a surviving excerpt.
    """

    text: str
    exported_chars: int
    omission_shape: FilingSectionOmissionShape | None


def select_filing_inputs(
    items: Sequence[TextItem], *, per_filing_chars: int, per_symbol_chars: int
) -> list[FilingInput]:
    """Return newest-first filing inputs under both character ceilings."""
    filings = sorted(
        (item for item in items if item.source_type == "filing"),
        key=lambda item: (item.published_at, item.source_id),
        reverse=True,
    )
    quarterly = [
        index
        for index, item in enumerate(filings)
        if _form_type(item.title) in _TEN_Q_FORMS
    ]
    allocation_order = quarterly + [
        index for index in range(len(filings)) if index not in quarterly
    ]
    remaining = per_symbol_chars
    selected: dict[int, FilingInput] = {}
    for index in allocation_order:
        item = filings[index]
        form_type = _form_type(item.title)
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


def select_filing_text(
    item: TextItem, form_type: str, budget: int
) -> FilingTextSelection:
    """Select one filing under `budget`, preferring important 10-Q sections.

    The complete filing remains in `TextItem.content_text` for audit/storage.
    This function only shapes the copy offered to a qualitative-analysis
    context. A parser miss is fail-soft and visibly falls back to the historic
    leading slice.
    """
    original = item.content_text
    if budget <= 0:
        return _selection("", len(original), "omitted_symbol_budget", ())
    if len(original) <= budget:
        return _selection(original, len(original), "full", ())
    if form_type not in _TEN_Q_FORMS or not item.filing_sections:
        return _selection(original[:budget], len(original), "head_fallback", ())

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
        return _selection(original[:budget], len(original), "head_fallback", ())

    headers = {name: f"[SECTION {name}]\n" for name, _, _ in available}
    header_chars = sum(len(header) for header in headers.values()) + 2 * (
        len(headers) - 1
    )
    content_budget = max(0, budget - header_chars)
    if content_budget == 0:
        return _selection(original[:budget], len(original), "head_fallback", ())
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
    return _selection(selected, len(original), mode, coverage)


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
    original_chars: int,
    mode: FilingSelectionMode,
    sections: tuple[FilingSectionCoverage, ...],
) -> FilingTextSelection:
    return FilingTextSelection(
        text=text,
        coverage=FilingCoverage(
            original_chars=original_chars,
            exported_chars=len(text),
            is_truncated=len(text) < original_chars,
            selection_mode=mode,
            sections=list(sections),
        ),
    )


def _form_type(title: str | None) -> str:
    return (title or "unknown").split(" - ")[0]
