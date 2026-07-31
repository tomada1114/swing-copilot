"""Quote normalization shared by the schema bounds and the ingest check."""

from __future__ import annotations

import pytest

from swing_copilot.analysis.evidence import (
    normalize_evidence_text,
    normalized_source_bodies,
)

# Spelled by codepoint: these are exactly the glyphs lint forbids as literals,
# and naming them keeps each case readable at the assertion site.
NBSP = chr(0x00A0)
FULLWIDTH_N = chr(0xFF2E)
RIGHT_SINGLE_QUOTE = chr(0x2019)
LEFT_DOUBLE_QUOTE = chr(0x201C)
RIGHT_DOUBLE_QUOTE = chr(0x201D)
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)
MINUS_SIGN = chr(0x2212)


class TestNormalizeEvidenceText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("Net Revenue", "net revenue", id="case-folded"),
            pytest.param("net\n\t revenue", "net revenue", id="whitespace-collapsed"),
            pytest.param("  net revenue  ", "net revenue", id="stripped"),
            pytest.param(f"net{NBSP}revenue", "net revenue", id="non-breaking-space"),
            pytest.param(f"{FULLWIDTH_N}et", "net", id="nfkc-fullwidth"),
            pytest.param(
                f"the company{RIGHT_SINGLE_QUOTE}s",
                "the company's",
                id="curly-apostrophe",
            ),
            pytest.param(
                f"{LEFT_DOUBLE_QUOTE}guidance{RIGHT_DOUBLE_QUOTE}",
                '"guidance"',
                id="curly-quotes",
            ),
            pytest.param(f"year{EN_DASH}over", "year-over", id="en-dash"),
            pytest.param(f"year{EM_DASH}over", "year-over", id="em-dash"),
            pytest.param(f"{MINUS_SIGN}5.0%", "-5.0%", id="minus-sign"),
        ],
    )
    def test_presentation_differences_normalize_away(self, raw, expected):
        assert normalize_evidence_text(raw) == expected

    def test_empty_text_normalizes_to_empty(self):
        assert normalize_evidence_text("   \n ") == ""

    def test_different_wording_stays_different(self):
        assert normalize_evidence_text("revenue rose") != normalize_evidence_text(
            "revenue fell"
        )


class TestNormalizedSourceBodies:
    def test_every_body_is_indexed_in_normalized_form(self):
        bodies = normalized_source_bodies(
            [("a", "Revenue  Rose"), ("b", f"Costs{EM_DASH}Fell")]
        )

        assert bodies == {"a": "revenue rose", "b": "costs-fell"}

    def test_no_sources_yields_an_empty_index(self):
        assert normalized_source_bodies([]) == {}
