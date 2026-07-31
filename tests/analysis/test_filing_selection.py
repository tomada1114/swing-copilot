"""Behavioral tests for deterministic filing-section selection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.filing_selection import select_filing_text
from swing_copilot.analysis.schemas import FilingSectionCoverage
from swing_copilot.text.base import FilingSection, TextItem

_MARKER = "\n[... omitted middle of section ...]\n"


def _item(text: str, sections: tuple[FilingSection, ...] = ()) -> TextItem:
    stamp = datetime(2027, 2, 20, tzinfo=UTC)
    return TextItem(
        source_id="edgar:1",
        symbol="AAPL",
        source_type="filing",
        published_at=stamp,
        title="10-Q - Apple",
        source_url="https://example.test/filing",
        content_text=text,
        fetched_at=stamp,
        filing_sections=sections,
    )


def test_long_ten_q_prioritizes_each_structured_section() -> None:
    sections = (
        FilingSection("part_i_item_1", "financials " * 20),
        FilingSection("part_i_item_2", "mda " * 20),
        FilingSection("part_ii_item_1a", "risks " * 20),
        FilingSection("part_ii_item_1", "legal " * 20),
    )

    selected = select_filing_text(_item("X" * 2_000, sections), "10-Q", 500)

    assert len(selected.text) == 500
    for section in sections:
        assert f"[SECTION {section.name}]" in selected.text
    assert selected.coverage.selection_mode == "section_priority_partial"
    assert selected.coverage.is_truncated is True


def test_truncated_section_keeps_its_tail_not_only_its_head() -> None:
    body = "".join(f"{index:06d}" for index in range(2_000))
    sections = (FilingSection("part_i_item_1", body),)

    selected = select_filing_text(_item("X" * 50_000, sections), "10-Q", 2_000)

    assert body[:100] in selected.text
    assert body[-100:] in selected.text
    assert "[... omitted middle of section ...]" in selected.text
    assert len(selected.text) <= 2_000


def test_production_scale_ten_q_keeps_end_of_financial_statements() -> None:
    """The 2026-07-30 run lost every 10-Q's contingencies note this way.

    Part I Item 1 runs far past its 50,000-character quota for a large filer,
    so head-only truncation dropped the commitments/contingencies note that
    closes the section while the balance sheet at the top always survived.
    """
    statements = "BALANCE SHEET LINE ITEM. " * 8_000
    contingencies = "NOTE 13 COMMITMENTS AND CONTINGENCIES: pending litigation."
    sections = (
        FilingSection("part_i_item_1", statements + contingencies),
        FilingSection("part_i_item_2", "MD&A discussion. " * 4_000),
        FilingSection("part_ii_item_1a", "Risk factors. " * 2_000),
        FilingSection("part_ii_item_1", "Legal proceedings. " * 1_000),
    )

    selected = select_filing_text(_item("X" * 400_000, sections), "10-Q", 120_000)

    assert contingencies in selected.text
    assert selected.text.startswith("[SECTION part_i_item_1]\nBALANCE SHEET")
    assert selected.coverage.selection_mode == "section_priority_partial"
    assert len(selected.text) <= 120_000


def test_section_shorter_than_the_omission_marker_stays_a_leading_slice() -> None:
    sections = (FilingSection("part_i_item_1", "0123456789"),)

    selected = select_filing_text(_item("X" * 5_000, sections), "10-Q", 30)

    assert "[... omitted" not in selected.text
    assert len(selected.text) <= 30


def test_parser_miss_falls_back_to_leading_slice() -> None:
    selected = select_filing_text(_item("0123456789"), "10-Q", 4)

    assert selected.text == "0123"
    assert selected.coverage.selection_mode == "head_fallback"
    assert selected.coverage.sections == []


def test_zero_symbol_budget_keeps_visible_omission_signal() -> None:
    selected = select_filing_text(_item("0123456789"), "10-Q", 0)

    assert selected.text == ""
    assert selected.coverage.selection_mode == "omitted_symbol_budget"
    assert selected.coverage.exported_chars == 0


def test_partial_section_reports_its_head_and_tail_deficit() -> None:
    """A partial section states how much it lost and that the middle is gone."""
    body = "".join(f"{index:06d}" for index in range(2_000))
    sections = (FilingSection("part_i_item_1", body),)

    selected = select_filing_text(_item("X" * 50_000, sections), "10-Q", 2_000)

    section = _coverage_of(selected.coverage.sections, "part_i_item_1")
    exported = selected.text.split("]\n", 1)[1]
    head, tail = exported.split(_MARKER)
    assert section.status == "partial"
    assert section.omission_shape == "head_and_tail"
    assert section.original_chars == len(body)
    assert section.exported_chars == len(head) + len(tail)
    assert body.startswith(head)
    assert body.endswith(tail)


def test_partial_section_too_short_for_a_tail_reports_a_head_only_deficit() -> None:
    """The leading-slice fallback keeps no tail, so the shape must say so."""
    sections = (FilingSection("part_i_item_1", "0123456789"),)

    selected = select_filing_text(_item("X" * 5_000, sections), "10-Q", 30)

    section = _coverage_of(selected.coverage.sections, "part_i_item_1")
    assert selected.text.endswith("012345")
    assert section.status == "partial"
    assert section.omission_shape == "head_only"
    assert section.original_chars == 10
    assert section.exported_chars == 6


def test_complete_section_reports_no_deficit_and_absent_section_reports_no_counts() -> (
    None
):
    sections = (FilingSection("part_i_item_1", "a" * 100),)

    selected = select_filing_text(_item("X" * 50_000, sections), "10-Q", 10_000)

    kept = _coverage_of(selected.coverage.sections, "part_i_item_1")
    absent = _coverage_of(selected.coverage.sections, "part_ii_item_1a")
    assert (kept.status, kept.original_chars, kept.exported_chars) == ("full", 100, 100)
    assert kept.omission_shape is None
    assert absent.status == "missing"
    assert (absent.original_chars, absent.exported_chars) == (None, None)


def test_section_squeezed_out_reports_a_zero_char_partial_without_a_shape() -> None:
    """A section that lost its whole allocation has no surviving excerpt to shape."""
    sections = tuple(
        FilingSection(name, "body " * 100)
        for name in ("part_i_item_1", "part_i_item_2", "part_ii_item_1a")
    )

    selected = select_filing_text(_item("X" * 5_000, sections), "10-Q", 79)

    squeezed = _coverage_of(selected.coverage.sections, "part_i_item_2")
    assert "[SECTION part_i_item_2]" not in selected.text
    assert squeezed.status == "partial"
    assert squeezed.exported_chars == 0
    assert squeezed.original_chars == 499  # the parsed section is stripped
    assert squeezed.omission_shape is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"status": "partial", "original_chars": 100, "exported_chars": 101},
            r"exported_chars cannot exceed original_chars",
            id="exported-above-original",
        ),
        pytest.param(
            {"status": "partial", "exported_chars": 10},
            r"original_chars and exported_chars must be given together",
            id="counts-not-paired",
        ),
        pytest.param(
            {"status": "partial", "original_chars": 100, "exported_chars": 100},
            r"a partial section must export fewer chars than the original",
            id="partial-lost-nothing",
        ),
        pytest.param(
            {"status": "full", "omission_shape": "head_and_tail"},
            r"omission_shape applies only to a partial section",
            id="shape-on-non-partial",
        ),
    ],
)
def test_incoherent_section_coverage_is_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        FilingSectionCoverage(name="part_i_item_1", **overrides)  # type: ignore[arg-type]


def _coverage_of(
    sections: list[FilingSectionCoverage], name: str
) -> FilingSectionCoverage:
    return next(section for section in sections if section.name == name)
