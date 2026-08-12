"""Behavioral tests for deterministic filing-section selection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.filing_selection import (
    _allocate_section_chars,
    select_filing_text,
)
from swing_copilot.analysis.schemas import FilingSectionCoverage, canonical_json_digest
from swing_copilot.analysis.validate import load_analysis_input
from swing_copilot.text.base import FilingSection, TextItem
from tests.analysis.conftest import input_payload

if TYPE_CHECKING:
    from pathlib import Path

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


def test_complete_section_reports_no_deficit_and_unparsed_siblings_report_no_counts() -> (
    None
):
    # Only part_i_item_1 is parsed: its Part I sibling (part_i_item_2) has a
    # parsed neighbor so it reads absent_from_filing, while Part II has zero
    # parsed sections so both of its items read not_parsed (REQ-003/REQ-004).
    sections = (FilingSection("part_i_item_1", "a" * 100),)

    selected = select_filing_text(_item("X" * 50_000, sections), "10-Q", 10_000)

    kept = _coverage_of(selected.coverage.sections, "part_i_item_1")
    absent_sibling = _coverage_of(selected.coverage.sections, "part_i_item_2")
    not_parsed_a = _coverage_of(selected.coverage.sections, "part_ii_item_1a")
    not_parsed_b = _coverage_of(selected.coverage.sections, "part_ii_item_1")
    assert (kept.status, kept.original_chars, kept.exported_chars) == ("full", 100, 100)
    assert kept.omission_shape is None
    assert absent_sibling.status == "absent_from_filing"
    assert (absent_sibling.original_chars, absent_sibling.exported_chars) == (
        None,
        None,
    )
    assert not_parsed_a.status == "not_parsed"
    assert not_parsed_b.status == "not_parsed"
    assert (not_parsed_a.original_chars, not_parsed_a.exported_chars) == (None, None)


def test_part_ii_with_one_parsed_item_reports_the_other_as_absent_from_filing() -> None:
    """REQ-003, Example 2: Part II structure is readable, Item 1A just isn't there."""
    sections = (FilingSection("part_ii_item_1", "Legal proceedings text."),)

    selected = select_filing_text(_item("X" * 50_000, sections), "10-Q", 10_000)

    item_1 = _coverage_of(selected.coverage.sections, "part_ii_item_1")
    item_1a = _coverage_of(selected.coverage.sections, "part_ii_item_1a")
    assert item_1.status == "full"
    assert item_1a.status == "absent_from_filing"


def test_part_ii_with_zero_parsed_items_reports_both_as_not_parsed() -> None:
    """REQ-004, Example 1: the CF run where Part II's structure never parsed."""
    sections = (
        FilingSection("part_i_item_1", "Financial statements."),
        FilingSection("part_i_item_2", "MD&A."),
    )

    selected = select_filing_text(_item("X" * 50_000, sections), "10-Q", 10_000)

    item_1a = _coverage_of(selected.coverage.sections, "part_ii_item_1a")
    item_1 = _coverage_of(selected.coverage.sections, "part_ii_item_1")
    assert item_1a.status == "not_parsed"
    assert item_1.status == "not_parsed"


def test_part_i_grouping_is_symmetric_with_part_ii() -> None:
    """REQ-005: the same rule applies when Part I is the one missing structure."""
    sections = (
        FilingSection("part_ii_item_1a", "Risk factors."),
        FilingSection("part_ii_item_1", "Legal proceedings."),
    )

    selected = select_filing_text(_item("X" * 50_000, sections), "10-Q", 10_000)

    part_i_1 = _coverage_of(selected.coverage.sections, "part_i_item_1")
    part_i_2 = _coverage_of(selected.coverage.sections, "part_i_item_2")
    assert part_i_1.status == "not_parsed"
    assert part_i_2.status == "not_parsed"


def test_all_four_sections_parsed_reports_neither_absent_nor_not_parsed() -> None:
    sections = tuple(
        FilingSection(name, f"{name} body text")
        for name in (
            "part_i_item_1",
            "part_i_item_2",
            "part_ii_item_1a",
            "part_ii_item_1",
        )
    )

    selected = select_filing_text(_item("X" * 50_000, sections), "10-Q", 10_000)

    statuses = {section.status for section in selected.coverage.sections}
    assert statuses == {"full"}


def test_no_generated_coverage_ever_reports_the_legacy_missing_status() -> None:
    # REQ-002, sweeping several scenarios that used to fall back to "missing".
    scenarios = [
        (FilingSection("part_i_item_1", "a" * 100),),
        (FilingSection("part_ii_item_1", "b" * 100),),
        (),
    ]
    for sections in scenarios:
        selected = select_filing_text(_item("X" * 50_000, sections), "10-Q", 10_000)
        assert all(
            section.status != "missing" for section in selected.coverage.sections
        )


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


def test_leftover_budget_favors_the_most_under_served_section() -> None:
    """REQ-006/Example 3: shortage-ratio descending, not declaration order."""
    available = [
        ("part_i_item_1", 50_000, "a" * 600),
        ("part_i_item_2", 40_000, "b" * 300),
        ("part_ii_item_1a", 20_000, "c" * 5_000),
        ("part_ii_item_1", 10_000, "d" * 50),
    ]

    allocated = _allocate_section_chars(available, 1_000)

    assert allocated == {
        "part_i_item_1": 416,
        "part_i_item_2": 300,
        "part_ii_item_1a": 234,
        "part_ii_item_1": 50,
    }
    # REQ-007: never allocate past the budget.
    assert sum(allocated.values()) == 1_000


def test_a_zero_scaled_quota_does_not_raise_a_zero_division() -> None:
    """Boundary: allocated[name] == 0 must use max(1, 0) in the shortage ratio."""
    # budget=1 makes every scaled quota floor to 0 before redistribution.
    available = [
        ("part_i_item_1", 50_000, "a" * 10),
        ("part_ii_item_1", 10_000, "b" * 10),
    ]

    allocated = _allocate_section_chars(available, 1)

    assert sum(allocated.values()) == 1
    assert allocated["part_i_item_1"] + allocated["part_ii_item_1"] == 1


def test_equal_shortage_ratios_tie_break_on_ascending_section_name() -> None:
    # Both sections start with allocated=0 and equal content length, so their
    # shortage ratios are identical; the lexicographically earlier name wins
    # the single leftover character.
    available = [
        ("part_ii_item_1", 1, "a" * 5),
        ("part_i_item_2", 1, "b" * 5),
    ]

    allocated = _allocate_section_chars(available, 1)

    assert allocated == {"part_ii_item_1": 0, "part_i_item_2": 1}


def test_identical_input_produces_identical_output() -> None:
    """REQ-008: determinism."""
    sections = (
        FilingSection("part_i_item_1", "financials " * 20),
        FilingSection("part_i_item_2", "mda " * 20),
        FilingSection("part_ii_item_1a", "risks " * 20),
        FilingSection("part_ii_item_1", "legal " * 20),
    )
    item = _item("X" * 2_000, sections)

    first = select_filing_text(item, "10-Q", 500)
    second = select_filing_text(item, "10-Q", 500)

    assert first.text == second.text
    assert first.coverage == second.coverage


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


@pytest.mark.parametrize(
    "status",
    ["full", "partial", "absent_from_filing", "not_parsed", "missing"],
)
def test_filing_section_status_accepts_all_five_literal_values(status: str) -> None:
    # REQ-001: "missing" stays valid so archived analysis_input.json documents
    # keep parsing (REQ-009/002); new code just never emits it.
    kwargs: dict[str, object] = {"status": status}
    if status == "partial":
        kwargs.update(original_chars=100, exported_chars=50)
    FilingSectionCoverage(name="part_i_item_1", **kwargs)  # type: ignore[arg-type]


def test_an_archived_missing_section_status_still_parses(tmp_path: Path) -> None:
    """REQ-009: a minimal synthetic analysis_input.json with status=missing."""
    payload = input_payload()
    payload["candidates"][0]["filings"][0]["coverage"]["sections"] = [
        {
            "name": "part_ii_item_1",
            "status": "missing",
            "original_chars": None,
            "exported_chars": None,
            "omission_shape": None,
        }
    ]
    payload["input_digest"] = canonical_json_digest(
        payload, excluded_field="input_digest"
    )
    input_path = tmp_path / "analysis_input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_analysis_input(input_path)

    coverage = loaded.candidates[0].filings[0].coverage
    assert coverage is not None
    section = coverage.sections[0]
    assert section.status == "missing"


def _coverage_of(
    sections: list[FilingSectionCoverage], name: str
) -> FilingSectionCoverage:
    return next(section for section in sections if section.name == name)
