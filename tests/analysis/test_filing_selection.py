"""Behavioral tests for deterministic filing-section selection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.filing_selection import (
    _MIN_FILING_CHARS,
    _allocate_section_chars,
    _shape_exhibit,
    select_filing_inputs,
    select_filing_text,
)
from swing_copilot.analysis.schemas import (
    FilingInput,
    FilingSectionCoverage,
    canonical_json_digest,
)
from swing_copilot.analysis.validate import load_analysis_input
from swing_copilot.storage.verdict_records import AnalysisSourceCoverageRecord
from swing_copilot.text.base import (
    EXHIBIT_OMISSION_MARKER,
    EXHIBIT_TRUNCATION_MARKER,
    FilingSection,
    TextItem,
)
from tests.analysis.conftest import input_payload

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.storage.state_store import StateStore

_MARKER = "\n[... omitted middle of section ...]\n"
_EXHIBIT_PASSAGE_MARKER = "[... omitted lower-value exhibit passage ...]"
#: One marker plus the blank line joining it to its neighbour.
_MARKER_ROOM = len(_EXHIBIT_PASSAGE_MARKER) + 2
_PRIMARY_TEXT = (
    "Item 2.02 Results of Operations and Financial Condition.\n"
    "On February 20, 2027 the registrant issued a press release.\n\n"
)
_STATEMENT_TABLE = (
    "| (In thousands, except per share amounts) | Q4 2026 | Q4 2025 |\n"
    "| --- | --- | --- |\n"
    "| Total revenues | 1,543,210 | 1,402,118 |\n"
    "| Operating income | 312,004 | 268,551 |\n"
    "| Diluted earnings per share | 2.41 | 2.02 |"
)
_NON_GAAP_TABLE = (
    "| Reconciliation of GAAP to non-GAAP | Q4 2026 |\n"
    "| --- | --- |\n"
    "| GAAP operating income | 312,004 |\n"
    "| Constant-currency adjustment | (8,115) |\n"
    "| Non-GAAP operating income | 303,889 |"
)
_GUIDANCE_PROSE = (
    "Revenues rose 10% year over year, and the company now expects full-year "
    "2027 revenues of $6.1 billion to $6.2 billion."
)
_SEGMENT_PROSE = (
    "Segment operating margin expanded 80 basis points, driven by pricing and "
    "a lower mix of promotional volume."
)
_DISCLAIMER = (
    "Forward-Looking Statements: this release contains forward-looking "
    "statements within the meaning of the Private Securities Litigation "
    "Reform Act of 1995, and actual results may differ materially."
)
_CALL_NOTICE = (
    "The company will host a conference call today at 5:00 p.m. Eastern Time. "
    "A live webcast will be available on the investor relations website."
)
_ABOUT_SECTION = (
    "About Example Corp\n"
    "Example Corp is a leading provider of example services worldwide."
)
_CONTACTS = "Investor Contact: Jane Doe.\nMedia Contact: John Roe."
#: An 8-K that is not about the quarter: no `EX-99*` exhibit, and an item
#: other than 2.02 in the primary document.
_NON_EARNINGS_EIGHT_K = (
    "Item 5.02 Departure of Directors or Certain Officers.\n"
    "On February 25, 2027 the registrant appointed a new principal officer.\n\n"
    "[EXHIBIT EX-10.1 agreement.htm]\n"
    "The employment agreement is filed herewith as an exhibit."
)


def _paragraphs(count: int, label: str) -> str:
    """Build a blank-line separated body of `count` distinct prose blocks."""
    return "\n\n".join(
        f"{label} paragraph {index:04d} describing the quarter."
        for index in range(count)
    )


def _press_release_body() -> str:
    """One earnings release: prose, boilerplate, and the tables that matter.

    Ordered the way an issuer writes it -- the statements and the non-GAAP
    reconciliation last, after the disclaimer and the "About" block, which is
    exactly why head-only truncation dropped them first.
    """
    return "\n\n".join(
        [
            _GUIDANCE_PROSE,
            _DISCLAIMER,
            _SEGMENT_PROSE,
            _CALL_NOTICE,
            _STATEMENT_TABLE,
            _NON_GAAP_TABLE,
            _ABOUT_SECTION,
            _CONTACTS,
        ]
    )


def _eight_k(exhibits: list[tuple[str, str, str]]) -> str:
    """Assemble collected 8-K text exactly as `data/edgar.py` concatenates it."""
    return _PRIMARY_TEXT + "".join(
        f"\n\n[EXHIBIT {document_type} {document}]\n{body}"
        for document_type, document, body in exhibits
    )


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


def _filing(source_id: str, title: str, day: int, content: str) -> TextItem:
    """One collected filing of `title`'s form, published on 2027-02-`day`."""
    return TextItem(
        source_id=source_id,
        symbol="AAPL",
        source_type="filing",
        published_at=datetime(2027, 2, day, tzinfo=UTC),
        title=title,
        source_url=f"https://example.test/{source_id}",
        content_text=content,
        fetched_at=datetime(2027, 3, 1, tzinfo=UTC),
        filing_sections=(),
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


class TestCollectionStageExhibitTruncation:
    """Issue #157: a loss that happened before export must still be visible.

    An earnings 8-K's `EX-99*` exhibits are cut off at `data/edgar.py`'s
    per-filing ceiling, so the truncated text *is* `content_text`. Export then
    copies it whole and the character triple honestly reports "nothing lost
    here" -- which a reader took for "nothing missing" (the HST/UNH/GOOG/TROW/
    WELL filings of run 43358613).
    """

    def test_an_export_that_kept_everything_still_reports_the_exhibit_cut(
        self,
    ) -> None:
        # The reported shape: exhibits ran past the collection ceiling, the
        # whole collected text fits the export budget.
        collected = (
            "Item 2.02 Results of Operations. See Exhibit 99.1."
            "\n\n[EXHIBIT EX-99.1 release.htm]\n"
            + "x" * 60_000
            + EXHIBIT_TRUNCATION_MARKER
        )

        selected = select_filing_text(_item(collected), "8-K", 120_000)

        coverage = selected.coverage
        assert selected.text == collected
        assert coverage.original_chars == coverage.exported_chars
        assert coverage.is_truncated is False
        assert coverage.selection_mode == "full"
        assert coverage.exhibit_truncated is True

    def test_a_filing_without_the_marker_reports_no_exhibit_truncation(self) -> None:
        collected = (
            "Item 2.02 Results of Operations. See Exhibit 99.1."
            "\n\n[EXHIBIT EX-99.1 release.htm]\n" + "x" * 1_000
        )

        selected = select_filing_text(_item(collected), "8-K", 120_000)

        assert selected.coverage.exhibit_truncated is False
        assert selected.coverage.is_truncated is False

    def test_an_export_slice_that_drops_the_marker_still_reports_the_cut(self) -> None:
        # `head_fallback` keeps a leading slice, so the trailing marker does
        # not survive into `text`. The exhibit loss is a property of the
        # collected filing, so it must be reported from the collected text.
        collected = "x" * 60_000 + EXHIBIT_TRUNCATION_MARKER

        selected = select_filing_text(_item(collected), "8-K", 500)

        assert EXHIBIT_TRUNCATION_MARKER not in selected.text
        assert selected.coverage.selection_mode == "head_fallback"
        assert selected.coverage.is_truncated is True
        assert selected.coverage.exhibit_truncated is True

    def test_a_symbol_budget_omission_still_reports_the_cut(self) -> None:
        selected = select_filing_text(
            _item("x" * 100 + EXHIBIT_TRUNCATION_MARKER), "8-K", 0
        )

        assert selected.coverage.selection_mode == "omitted_symbol_budget"
        assert selected.coverage.exhibit_truncated is True

    def test_a_section_priority_ten_q_reports_the_cut_it_inherited(self) -> None:
        # 10-Q exhibits are not collected, but the field is filing-level and
        # must not silently stop being computed on the section-priority path.
        sections = (FilingSection("part_i_item_1", "financials " * 200),)
        collected = "X" * 50_000 + EXHIBIT_TRUNCATION_MARKER

        selected = select_filing_text(_item(collected, sections), "10-Q", 2_000)

        assert selected.coverage.selection_mode == "section_priority_partial"
        assert selected.coverage.exhibit_truncated is True

    def test_an_archived_coverage_without_the_field_parses_as_false(
        self, tmp_path: Path
    ) -> None:
        # The default keeps historical analysis-input-v2/v3 archives readable
        # by the P8 collect path; `false` there means "not recorded".
        payload = input_payload()
        del payload["candidates"][0]["filings"][0]["coverage"]["exhibit_truncated"]
        payload["input_digest"] = canonical_json_digest(
            payload, excluded_field="input_digest"
        )
        input_path = tmp_path / "analysis_input.json"
        input_path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = load_analysis_input(input_path)

        coverage = loaded.candidates[0].filings[0].coverage
        assert coverage is not None
        assert coverage.exhibit_truncated is False


class TestCollectionStageExhibitOmission:
    """Issue #163: the exhibit *count* cap is the same blind spot as #157.

    Exhibits past `_MAX_EXHIBITS_PER_FILING` are never fetched, so they leave
    no text of their own to shorten -- without the marker the filing reports
    itself complete exactly as a character-capped one used to.
    """

    def test_an_omitted_exhibit_is_reported_like_a_truncated_one(self) -> None:
        collected = (
            "Item 2.02 Results of Operations. See Exhibit 99.1."
            "\n\n[EXHIBIT EX-99.1 release.htm]\npress release" + EXHIBIT_OMISSION_MARKER
        )

        selected = select_filing_text(_item(collected), "8-K", 120_000)

        assert selected.text == collected
        assert selected.coverage.is_truncated is False
        assert selected.coverage.selection_mode == "full"
        assert selected.coverage.exhibit_truncated is True

    def test_an_export_slice_that_drops_the_omission_marker_still_reports_it(
        self,
    ) -> None:
        collected = "x" * 60_000 + EXHIBIT_OMISSION_MARKER

        selected = select_filing_text(_item(collected), "8-K", 500)

        assert EXHIBIT_OMISSION_MARKER not in selected.text
        assert selected.coverage.selection_mode == "head_fallback"
        assert selected.coverage.exhibit_truncated is True

    def test_both_collection_markers_report_one_gap(self) -> None:
        # A filing that hit both ceilings reports the same single boolean;
        # which cap applied stays readable in the text itself.
        collected = (
            "\n\n[EXHIBIT EX-99.1 release.htm]\n"
            + "x" * 60_000
            + EXHIBIT_TRUNCATION_MARKER
            + EXHIBIT_OMISSION_MARKER
        )

        selected = select_filing_text(_item(collected), "8-K", 120_000)

        assert selected.coverage.exhibit_truncated is True


class TestEightKExhibitSelection:
    """Issue #181: an 8-K is composed from its exhibits, not sliced off the top.

    A head slice of the collected 8-K drops the last exhibit whole and the end
    of the first one -- which is where the financial statements and the
    non-GAAP reconciliation sit. Issue #165's replay measured HST at 375,403
    characters and WELL at 264,246 against a 120,000 budget, so the cut is the
    normal case for an earnings 8-K, not an outlier.
    """

    def test_budget_pressure_serves_the_press_release_before_a_supplement(
        self,
    ) -> None:
        content = _eight_k(
            [
                ("EX-99.1", "release.htm", _paragraphs(200, "Press release")),
                ("EX-99.2", "supplement.htm", _paragraphs(900, "Supplemental")),
            ]
        )

        selected = select_filing_text(_item(content), "8-K", 40_000)

        release = _coverage_of(selected.coverage.sections, "exhibit_ex_99_1")
        supplement = _coverage_of(selected.coverage.sections, "exhibit_ex_99_2")
        # The supplement is 4.5x the release, so a fairness rule (or a head
        # slice) would spend the budget on it; priority must win instead.
        assert supplement.original_chars is not None
        assert release.original_chars is not None
        assert supplement.original_chars > release.original_chars
        assert release.status == "full"
        assert supplement.status == "partial"

    def test_every_exhibit_keeps_its_header_so_a_reader_knows_the_source(self) -> None:
        content = _eight_k(
            [
                ("EX-99.1", "release.htm", _paragraphs(200, "Press release")),
                ("EX-99.2", "supplement.htm", _paragraphs(900, "Supplemental")),
            ]
        )

        selected = select_filing_text(_item(content), "8-K", 40_000)

        assert "[EXHIBIT EX-99.1 release.htm]" in selected.text
        assert "[EXHIBIT EX-99.2 supplement.htm]" in selected.text
        assert len(selected.text) <= 40_000

    def test_an_exhibit_that_fits_its_allocation_is_kept_verbatim(self) -> None:
        """Boundary 1 of 3: exactly enough room, so nothing is selected away."""
        body = _press_release_body()

        shaped = _shape_exhibit(body, len(body))

        assert shaped.text == body
        assert shaped.exported_chars == len(body)
        assert shaped.omission_shape is None

    def test_a_slightly_over_allocation_drops_boilerplate_and_keeps_the_tables(
        self,
    ) -> None:
        """Boundary 2 of 3: one character short, so only boilerplate goes.

        Which boilerplate block goes is document order within the tier -- the
        contact block is the last one, so it is the first out.
        """
        body = _press_release_body()

        shaped = _shape_exhibit(body, len(body) - 1)

        assert _STATEMENT_TABLE in shaped.text
        assert _NON_GAAP_TABLE in shaped.text
        assert _GUIDANCE_PROSE in shaped.text
        assert _SEGMENT_PROSE in shaped.text
        assert _CONTACTS not in shaped.text
        assert shaped.omission_shape == "value_selected"
        assert len(shaped.text) <= len(body) - 1

    def test_a_far_over_allocation_keeps_the_tables_after_everything_else(
        self,
    ) -> None:
        """Boundary 3 of 3: room for the tables alone, and they are what stays."""
        body = _press_release_body()
        allocated = len(_STATEMENT_TABLE) + len(_NON_GAAP_TABLE) + 3 * _MARKER_ROOM

        shaped = _shape_exhibit(body, allocated)

        assert _STATEMENT_TABLE in shaped.text
        assert _NON_GAAP_TABLE in shaped.text
        assert _GUIDANCE_PROSE not in shaped.text
        assert _DISCLAIMER not in shaped.text
        assert _ABOUT_SECTION not in shaped.text
        assert _EXHIBIT_PASSAGE_MARKER in shaped.text
        assert len(shaped.text) <= allocated

    def test_the_drop_order_holds_through_the_public_entry_point(self) -> None:
        """The tables outlive the boilerplate for a whole 8-K, not just a body."""
        body = _press_release_body()
        content = _eight_k([("EX-99.1", "release.htm", body)])
        header_room = len("[EXHIBIT EX-99.1 release.htm]\n") + len(_PRIMARY_TEXT)

        wide = select_filing_text(
            _item(content), "8-K", header_room + len(body) - len(_CONTACTS)
        )
        narrow = select_filing_text(
            _item(content),
            "8-K",
            header_room
            + len(_STATEMENT_TABLE)
            + len(_NON_GAAP_TABLE)
            + 3 * _MARKER_ROOM,
        )

        assert _GUIDANCE_PROSE in wide.text
        assert _CONTACTS not in wide.text
        assert _STATEMENT_TABLE in narrow.text
        assert _NON_GAAP_TABLE in narrow.text
        assert _DISCLAIMER not in narrow.text
        assert _CONTACTS not in narrow.text
        assert _EXHIBIT_PASSAGE_MARKER in narrow.text

    @pytest.mark.parametrize("allocated", [0, 47, 91, 260, 512, 700, 1_000])
    def test_a_shaped_exhibit_never_exceeds_its_allocation(
        self, allocated: int
    ) -> None:
        """The markers and the blank lines rejoining blocks are paid for too."""
        body = _press_release_body()

        shaped = _shape_exhibit(body, allocated)

        assert len(shaped.text) <= allocated
        assert shaped.exported_chars <= allocated

    def test_an_exhibit_without_blank_lines_degrades_to_a_leading_slice(self) -> None:
        # No block boundary to cut at: the historic head slice is still better
        # than exporting nothing, and the shape says which one happened.
        body = "x" * 5_000

        shaped = _shape_exhibit(body, 1_000)

        assert shaped.text == "x" * 1_000
        assert shaped.omission_shape == "head_only"
        assert shaped.exported_chars == 1_000

    def test_a_squeezed_out_exhibit_reports_zero_chars_without_a_shape(self) -> None:
        shaped = _shape_exhibit("x" * 5_000, 0)

        assert shaped.text == ""
        assert shaped.exported_chars == 0
        assert shaped.omission_shape is None

    def test_an_eight_k_without_exhibit_headers_keeps_the_leading_slice(self) -> None:
        selected = select_filing_text(_item("x" * 5_000), "8-K", 1_000)

        assert selected.text == "x" * 1_000
        assert selected.coverage.selection_mode == "head_fallback"
        assert selected.coverage.sections == []

    def test_a_budget_too_small_for_the_headers_keeps_the_leading_slice(self) -> None:
        content = _eight_k([("EX-99.1", "release.htm", _paragraphs(50, "Release"))])

        selected = select_filing_text(_item(content), "8-K", 20)

        assert selected.coverage.selection_mode == "head_fallback"
        assert len(selected.text) == 20

    def test_a_filing_with_no_primary_text_reports_only_its_exhibits(self) -> None:
        # `filing.text()` came back empty, so the collected text starts at the
        # first exhibit header: there is no primary part to report.
        content = f"[EXHIBIT EX-99.1 release.htm]\n{_press_release_body()}"

        selected = select_filing_text(_item(content), "8-K", 600)

        names = [section.name for section in selected.coverage.sections]
        assert names == ["exhibit_ex_99_1"]
        assert selected.text.startswith("[EXHIBIT EX-99.1 release.htm]")

    def test_repeated_document_types_get_distinct_coverage_names(self) -> None:
        content = _eight_k(
            [
                ("EX-99", "first.htm", _paragraphs(100, "First")),
                ("EX-99", "second.htm", _paragraphs(100, "Second")),
            ]
        )

        selected = select_filing_text(_item(content), "8-K", 5_000)

        names = [section.name for section in selected.coverage.sections]
        assert names == ["exhibit_primary", "exhibit_ex_99", "exhibit_ex_99_2"]

    def test_an_unrecognized_exhibit_number_still_leads_the_priority_tier(
        self,
    ) -> None:
        # No EX-99/EX-99.1 to anchor on: the first exhibit is the closest
        # thing to the press release there is, so it outranks what follows.
        content = _eight_k(
            [
                ("EX-99.3", "first.htm", _paragraphs(200, "First")),
                ("EX-99.4", "second.htm", _paragraphs(200, "Second")),
            ]
        )

        selected = select_filing_text(_item(content), "8-K", 12_000)

        first = _coverage_of(selected.coverage.sections, "exhibit_ex_99_3")
        second = _coverage_of(selected.coverage.sections, "exhibit_ex_99_4")
        assert first.exported_chars is not None
        assert second.exported_chars is not None
        assert first.exported_chars > second.exported_chars

    def test_part_counts_add_up_to_the_filing_counts(self) -> None:
        # The parts partition the collected text, which is why the mode is
        # always the partial one: a filing that needed selection lost
        # something from at least one part.
        content = _eight_k(
            [
                ("EX-99.1", "release.htm", _paragraphs(200, "Press release")),
                ("EX-99.2", "supplement.htm", _paragraphs(400, "Supplemental")),
            ]
        )

        selected = select_filing_text(_item(content), "8-K", 30_000)

        headers = sum(
            len(f"[EXHIBIT {kind} {document}]\n")
            for kind, document in (
                ("EX-99.1", "release.htm"),
                ("EX-99.2", "supplement.htm"),
            )
        )
        original_total = sum(
            section.original_chars or 0 for section in selected.coverage.sections
        )
        assert original_total + headers == selected.coverage.original_chars
        assert selected.coverage.selection_mode == "section_priority_partial"
        assert selected.coverage.is_truncated is True

    def test_identical_input_produces_identical_exhibit_output(self) -> None:
        content = _eight_k(
            [
                ("EX-99.1", "release.htm", _press_release_body()),
                ("EX-99.2", "supplement.htm", _paragraphs(400, "Supplemental")),
            ]
        )
        item = _item(content)

        first = select_filing_text(item, "8-K", 3_000)
        second = select_filing_text(item, "8-K", 3_000)

        assert first.text == second.text
        assert first.coverage == second.coverage

    def test_a_collection_stage_cut_is_still_reported_under_exhibit_selection(
        self,
    ) -> None:
        # Issue #157's signal is filing-level and must not stop being computed
        # on the new path.
        content = (
            _eight_k([("EX-99.1", "release.htm", _paragraphs(400, "Release"))])
            + EXHIBIT_TRUNCATION_MARKER
        )

        selected = select_filing_text(_item(content), "8-K", 5_000)

        assert selected.coverage.selection_mode == "section_priority_partial"
        assert selected.coverage.exhibit_truncated is True

    def test_exhibit_coverage_survives_the_analysis_source_coverage_round_trip(
        self, state_store: StateStore
    ) -> None:
        """P8 reads the selection back from the DB, as `retro/collect.py` writes it."""
        content = _eight_k(
            [
                ("EX-99.1", "release.htm", _press_release_body()),
                ("EX-99.2", "supplement.htm", _paragraphs(400, "Supplemental")),
            ]
        )
        selected = select_filing_text(_item(content), "8-K", 4_000)
        run_id = uuid4()

        state_store.replace_run_verdicts(
            run_id,
            [],
            [],
            [
                AnalysisSourceCoverageRecord(
                    run_id=run_id,
                    symbol="AAPL",
                    source_id="edgar:1",
                    original_chars=selected.coverage.original_chars,
                    exported_chars=selected.coverage.exported_chars,
                    is_truncated=selected.coverage.is_truncated,
                    selection_mode=selected.coverage.selection_mode,
                    sections=tuple(
                        (section.name, section.status)
                        for section in selected.coverage.sections
                    ),
                    exhibit_truncated=selected.coverage.exhibit_truncated,
                )
            ],
        )

        stored = state_store.get_analysis_source_coverages(run_id, "AAPL")
        assert stored[0].selection_mode == "section_priority_partial"
        # The vocabulary a retrospective reads: which exhibit was complete and
        # which one paid for the budget, not just "the filing was truncated".
        assert stored[0].sections == (
            ("exhibit_primary", "full"),
            ("exhibit_ex_99_1", "full"),
            ("exhibit_ex_99_2", "partial"),
        )


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


class TestPerSymbolBudgetAllocationOrder:
    """Issue #191: the earnings press release must not be the one starved.

    `per_symbol_chars` is consumed in allocation order, so whatever is served
    last exports as `omitted_symbol_budget`. Serving every 10-Q first put the
    earnings 8-K -- the quarter's press release, with the revenue, EPS, and
    guidance figures -- at the back of the queue. Each test below pins the
    symbol budget to exactly the winner's collected length, so the loser's
    budget is exactly zero.
    """

    def test_an_earnings_eight_k_is_served_before_a_newer_ten_q(self) -> None:
        eight_k = _eight_k([("EX-99.1", "release.htm", _press_release_body())])
        items = [
            _filing("edgar:8k", "8-K - Apple", 20, eight_k),
            _filing("edgar:10q", "10-Q - Apple", 25, "X" * 50_000),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=100_000, per_symbol_chars=len(eight_k)
        )

        release = _input_of(inputs, "edgar:8k")
        quarterly = _input_of(inputs, "edgar:10q")
        assert release.text == eight_k
        assert _selection_mode(release) == "full"
        assert quarterly.text == ""
        assert _selection_mode(quarterly) == "omitted_symbol_budget"

    def test_an_item_2_02_eight_k_without_an_ex_99_exhibit_is_served_first(
        self,
    ) -> None:
        # The exhibits were never collected (Issue #163's count cap), so the
        # only signal left is the item the primary document reports under.
        eight_k = _PRIMARY_TEXT + "[EXHIBIT EX-10.1 agreement.htm]\nAn agreement."
        items = [
            _filing("edgar:8k", "8-K - Apple", 20, eight_k),
            _filing("edgar:10q", "10-Q - Apple", 25, "X" * 50_000),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=100_000, per_symbol_chars=len(eight_k)
        )

        assert _selection_mode(_input_of(inputs, "edgar:8k")) == "full"
        assert (
            _selection_mode(_input_of(inputs, "edgar:10q")) == "omitted_symbol_budget"
        )

    def test_a_non_earnings_eight_k_is_still_served_after_the_ten_q(self) -> None:
        # Newer than the 10-Q, and carrying neither signal, so the demotion is
        # what decides it -- an officer change must not cost the quarter.
        quarterly = "Condensed consolidated financial statements. " * 20
        items = [
            _filing("edgar:8k", "8-K - Apple", 25, _NON_EARNINGS_EIGHT_K),
            _filing("edgar:10q", "10-Q - Apple", 20, quarterly),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=100_000, per_symbol_chars=len(quarterly)
        )

        assert _selection_mode(_input_of(inputs, "edgar:10q")) == "full"
        assert _selection_mode(_input_of(inputs, "edgar:8k")) == "omitted_symbol_budget"

    def test_two_ten_qs_are_still_served_newest_first(self) -> None:
        newest = "Newest quarter. " * 10
        items = [
            _filing("edgar:old", "10-Q - Apple", 20, "Older quarter. " * 10),
            _filing("edgar:new", "10-Q - Apple", 25, newest),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=100_000, per_symbol_chars=len(newest)
        )

        assert _selection_mode(_input_of(inputs, "edgar:new")) == "full"
        assert (
            _selection_mode(_input_of(inputs, "edgar:old")) == "omitted_symbol_budget"
        )

    def test_forms_outside_both_priority_tiers_are_served_newest_first(self) -> None:
        # A non-earnings 8-K and a 10-K share the trailing tier, where the
        # historic newest-first order is all that orders them.
        items = [
            _filing("edgar:8k", "8-K - Apple", 25, _NON_EARNINGS_EIGHT_K),
            _filing("edgar:10k", "10-K - Apple", 20, "Annual report text. " * 20),
        ]

        inputs = select_filing_inputs(
            items,
            per_filing_chars=100_000,
            per_symbol_chars=len(_NON_EARNINGS_EIGHT_K),
        )

        assert _selection_mode(_input_of(inputs, "edgar:8k")) == "full"
        assert (
            _selection_mode(_input_of(inputs, "edgar:10k")) == "omitted_symbol_budget"
        )

    def test_the_returned_list_stays_newest_first_whatever_the_allocation_order(
        self,
    ) -> None:
        """Allocation order is 8-K, 10-Q, 10-K; document order is by date."""
        items = [
            _filing("edgar:10q", "10-Q - Apple", 20, "Quarterly report text."),
            _filing(
                "edgar:8k",
                "8-K - Apple",
                22,
                _eight_k([("EX-99.1", "release.htm", "Press release text.")]),
            ),
            _filing("edgar:10k", "10-K - Apple", 26, "Annual report text."),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=1_000, per_symbol_chars=10_000
        )

        assert [entry.source_id for entry in inputs] == [
            "edgar:10k",
            "edgar:8k",
            "edgar:10q",
        ]
        assert [entry.filed_at for entry in inputs] == sorted(
            (entry.filed_at for entry in inputs), reverse=True
        )
        # Budget for everything, so ordering is the only thing under test.
        assert {_selection_mode(entry) for entry in inputs} == {"full"}


class TestPerSymbolMinimumGuarantee:
    """Issue #255: priority decides the share, never whether a filing is read.

    The shipped ceilings are 120,000 per filing and 240,000 per symbol --
    exactly twice -- so two filings that each fill the per-filing ceiling
    consumed the whole symbol budget and the third exported at 10 characters
    (HST, 2026-08-14) or 0 (UDR). Every filing therefore reserves
    `_MIN_FILING_CHARS` (or its own length, when shorter) before the leaders
    are served beyond that reservation.
    """

    @pytest.mark.parametrize(
        ("length", "symbol"),
        [(6_670, "HST"), (4_074, "UDR")],
        ids=["hst", "udr"],
    )
    def test_a_small_third_filing_survives_two_ceiling_filling_filings(
        self, length: int, symbol: str
    ) -> None:
        """The 2026-08-14 run's two starved 8-K bodies, at their real lengths."""
        items = [
            _filing("edgar:8k-earnings", "8-K - Host", 25, _huge_earnings_eight_k()),
            _filing("edgar:10q", "10-Q - Host", 20, _huge_ten_q()),
            _filing("edgar:8k-old", "8-K - Host", 5, _dividend_eight_k(length)),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=120_000, per_symbol_chars=240_000
        )

        old = _input_of(inputs, "edgar:8k-old")
        assert old.text == _dividend_eight_k(length), symbol
        assert _selection_mode(old) == "full"
        assert sum(len(entry.text) for entry in inputs) <= 240_000

    def test_the_leading_filings_still_take_everything_but_the_reservation(
        self,
    ) -> None:
        """The guarantee is a floor for the tail, not a share-out of the budget."""
        items = [
            _filing("edgar:8k-earnings", "8-K - Host", 25, _huge_earnings_eight_k()),
            _filing("edgar:10q", "10-Q - Host", 20, _huge_ten_q()),
            _filing("edgar:8k-old", "8-K - Host", 5, _dividend_eight_k(6_670)),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=120_000, per_symbol_chars=240_000
        )

        release = _input_of(inputs, "edgar:8k-earnings")
        quarterly = _input_of(inputs, "edgar:10q")
        # The earnings 8-K keeps the whole per-filing ceiling; only the 10-Q,
        # served after it, pays for the 6,670 characters held back.
        assert len(release.text) >= 119_000
        assert len(quarterly.text) == 240_000 - len(release.text) - 6_670

    def test_a_long_third_filing_gets_the_minimum_rather_than_nothing(self) -> None:
        """A filing longer than the guarantee is truncated, never omitted."""
        items = [
            _filing("edgar:8k-earnings", "8-K - Host", 25, _huge_earnings_eight_k()),
            _filing("edgar:10q", "10-Q - Host", 20, _huge_ten_q()),
            _filing("edgar:10k", "10-K - Host", 5, "Annual report text. " * 20_000),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=120_000, per_symbol_chars=240_000
        )

        annual = _input_of(inputs, "edgar:10k")
        assert len(annual.text) >= _MIN_FILING_CHARS
        assert _selection_mode(annual) == "head_fallback"
        assert sum(len(entry.text) for entry in inputs) <= 240_000

    def test_a_filing_shorter_than_the_guarantee_reserves_only_its_own_length(
        self,
    ) -> None:
        items = [
            _filing("edgar:10k", "10-K - Host", 25, "Annual report text. " * 20_000),
            _filing("edgar:8k-old", "8-K - Host", 5, _dividend_eight_k(500)),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=120_000, per_symbol_chars=120_000
        )

        assert _selection_mode(_input_of(inputs, "edgar:8k-old")) == "full"
        # 119,500, not 112,000: the tail held back 500, not the full guarantee.
        assert len(_input_of(inputs, "edgar:10k").text) == 120_000 - 500

    def test_a_ceiling_too_small_for_every_guarantee_serves_them_in_priority_order(
        self,
    ) -> None:
        """Deterministic starvation: full minimum, the remainder, then zero."""
        annual = "Annual report text. " * 20_000
        items = [
            _filing("edgar:new", "10-K - Host", 25, annual),
            _filing("edgar:mid", "10-K - Host", 20, annual),
            _filing("edgar:old", "10-K - Host", 15, annual),
        ]

        inputs = select_filing_inputs(
            items, per_filing_chars=100_000, per_symbol_chars=_MIN_FILING_CHARS + 4_000
        )

        assert len(_input_of(inputs, "edgar:new").text) == _MIN_FILING_CHARS
        assert len(_input_of(inputs, "edgar:mid").text) == 4_000
        assert _input_of(inputs, "edgar:old").text == ""
        assert (
            _selection_mode(_input_of(inputs, "edgar:old")) == "omitted_symbol_budget"
        )


def _huge_earnings_eight_k() -> str:
    """An earnings 8-K far past the per-filing ceiling (HST: 380,267 chars)."""
    return _eight_k([("EX-99.1", "release.htm", _paragraphs(8_000, "Release"))])


def _huge_ten_q() -> str:
    """A 10-Q far past the per-filing ceiling (HST: 269,474 chars)."""
    return "Condensed consolidated financial statements. " * 6_000


def _dividend_eight_k(length: int) -> str:
    """A small non-earnings 8-K of exactly `length` characters.

    Item 8.01 and no `EX-99*` exhibit, so it lands in the trailing allocation
    tier -- the position that used to export at 10 characters or fewer.
    """
    body = "Item 8.01 Other Events. The board declared a dividend. "
    return (body * (length // len(body) + 1))[:length]


def _input_of(inputs: list[FilingInput], source_id: str) -> FilingInput:
    return next(entry for entry in inputs if entry.source_id == source_id)


def _selection_mode(filing: FilingInput) -> str:
    assert filing.coverage is not None
    return filing.coverage.selection_mode


def _coverage_of(
    sections: list[FilingSectionCoverage], name: str
) -> FilingSectionCoverage:
    return next(section for section in sections if section.name == name)
