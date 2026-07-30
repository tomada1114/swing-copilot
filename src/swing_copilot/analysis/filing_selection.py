"""Deterministic filing-text selection shared by daily and retrospective export."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.analysis.schemas import (
    FilingCoverage,
    FilingInput,
    FilingSectionCoverage,
    FilingSelectionMode,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.text.base import TextItem

_TEN_Q_FORMS = frozenset({"10-Q", "10-Q/A"})
_SECTION_TARGETS = (
    ("part_i_item_1", 50_000),
    ("part_i_item_2", 40_000),
    ("part_ii_item_1a", 20_000),
    ("part_ii_item_1", 10_000),
)
_TOTAL_SECTION_QUOTA = sum(quota for _, quota in _SECTION_TARGETS)
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
    parts = [
        f"{headers[name]}{_shape_section(content, allocated[name])}"
        for name, _, content in available
        if allocated[name] > 0
    ]
    selected = "\n\n".join(parts)[:budget]
    coverage = tuple(
        FilingSectionCoverage(
            name=name,
            status=(
                "missing"
                if name not in sections
                else "full"
                if allocated.get(name, 0) >= len(sections[name])
                else "partial"
            ),
        )
        for name, _ in _SECTION_TARGETS
    )
    mode: FilingSelectionMode = (
        "section_priority"
        if all(section.status == "full" for section in coverage)
        else "section_priority_partial"
    )
    return _selection(selected, len(original), mode, coverage)


def _shape_section(content: str, allocated: int) -> str:
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
        `_SECTION_OMISSION_MARKER`, exactly `allocated` characters long.
    """
    if allocated >= len(content):
        return content
    head_share, total_share = _SECTION_HEAD_SHARE
    kept = allocated - len(_SECTION_OMISSION_MARKER)
    head = kept * head_share // total_share
    tail = kept - head
    if kept <= 0 or tail <= 0:
        return content[:allocated]
    return f"{content[:head]}{_SECTION_OMISSION_MARKER}{content[len(content) - tail :]}"


def _allocate_section_chars(
    available: list[tuple[str, int, str]], budget: int
) -> dict[str, int]:
    """Allocate scaled minimum quotas, then reuse slack deterministically."""
    allocated: dict[str, int] = {}
    for name, quota, content in available:
        scaled_quota = budget * quota // _TOTAL_SECTION_QUOTA
        allocated[name] = min(len(content), scaled_quota)

    remaining = budget - sum(allocated.values())
    for name, _, content in available:
        if remaining <= 0:
            break
        extra = min(len(content) - allocated[name], remaining)
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
