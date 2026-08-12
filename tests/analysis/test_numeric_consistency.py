"""Figures in a fact's text reconciled with its quote (Issue #131)."""

from __future__ import annotations

import pytest

from swing_copilot.analysis.numeric_consistency import unsupported_magnitudes

#: The 2026-08-11 JBHT filing excerpt the issue was found in: a press-release
#: sentence rounded to three digits, and the statement table it summarizes.
PRESS_RELEASE_QUOTE = (
    "Total consolidated operating revenues were $3.50 billion for second "
    "quarter 2026, compared with $2.93 billion for second quarter 2025"
)
STATEMENT_QUOTE = (
    "Total operating revenues 3,495,296 2,928,181 Operating income 205,110"
)


class TestTheIssueCase:
    def test_the_misconverted_figure_is_reported(self):
        """35億9,530万 is not reachable from 3,495,296 by any power of ten."""
        assert unsupported_magnitudes(
            "2026年第2四半期の連結営業収益は35億9,530万ドル", STATEMENT_QUOTE
        ) == ("35億9,530万",)

    def test_the_corrected_figure_is_not_reported(self):
        assert (
            unsupported_magnitudes(
                "2026年第2四半期の連結営業収益は34億9,530万ドル", STATEMENT_QUOTE
            )
            == ()
        )

    def test_the_prior_year_figure_is_not_reported(self):
        assert (
            unsupported_magnitudes("前年同期は29億2,818万ドル", STATEMENT_QUOTE) == ()
        )

    def test_a_rounded_quote_still_admits_the_precise_statement(self):
        """`$3.50 billion` agrees with 34億9,530万 at the digits it states."""
        assert (
            unsupported_magnitudes("連結営業収益は34億9,530万ドル", PRESS_RELEASE_QUOTE)
            == ()
        )

    def test_a_rounded_quote_still_reports_the_misconverted_statement(self):
        assert unsupported_magnitudes(
            "連結営業収益は35億9,530万ドル", PRESS_RELEASE_QUOTE
        ) == ("35億9,530万",)


class TestUnitConversions:
    @pytest.mark.parametrize(
        ("text", "quote"),
        [
            pytest.param(
                "営業収益は3,495,296千ドル",
                STATEMENT_QUOTE,
                id="thousands-restated-verbatim",
            ),
            pytest.param(
                "営業収益は約35億ドル",
                STATEMENT_QUOTE,
                id="approximation-to-two-digits",
            ),
            pytest.param(
                "revenues reached $3.50 billion",
                STATEMENT_QUOTE,
                id="english-billion-from-a-thousands-table",
            ),
            pytest.param(
                "revenues reached 3,495.296 million dollars",
                STATEMENT_QUOTE,
                id="english-million",
            ),
            pytest.param(
                "営業利益は2億0,511万ドル",
                STATEMENT_QUOTE,
                id="composite-with-a-zero-term",
            ),
            pytest.param(
                "売上高は1兆2,345億円規模",
                "net sales of 1,234.5 billion yen",
                id="trillion-composite",
            ),
            pytest.param(
                "自社株買い枠は約10億ドル",
                "a share repurchase program of $996 million",
                id="rounding-up-out-of-the-decade",
            ),
        ],
    )
    def test_a_reachable_figure_is_not_reported(self, text, quote):
        assert unsupported_magnitudes(text, quote) == ()

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                "営業収益は3,594,296千ドル", ("3,594,296千",), id="transposed-digits"
            ),
            pytest.param(
                "営業利益は2億5,110万ドル", ("2億5,110万",), id="misplaced-separator"
            ),
            pytest.param(
                "revenues reached $4.50 billion",
                ("4.50 billion",),
                id="wrong-leading-digit",
            ),
        ],
    )
    def test_an_unreachable_figure_is_reported(self, text, expected):
        assert unsupported_magnitudes(text, STATEMENT_QUOTE) == expected


class TestWhatIsDeliberatelyNotChecked:
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("2026年第2四半期の実績である", id="a-year-and-a-quarter"),
            pytest.param("前年同期比 19.4% の増収となった", id="a-derived-percentage"),
            pytest.param("従業員は34,000人規模である", id="a-bare-count"),
            pytest.param("様式10-Qで提出された", id="a-form-number"),
        ],
    )
    def test_a_figure_without_a_magnitude_or_currency_marker_is_ignored(self, text):
        assert unsupported_magnitudes(text, STATEMENT_QUOTE) == ()

    def test_a_currency_symbol_before_the_number_puts_it_in_scope(self):
        assert unsupported_magnitudes("1株当たり $9.99 と記載", STATEMENT_QUOTE) == (
            "9.99",
        )

    def test_the_same_bare_number_without_a_currency_symbol_is_ignored(self):
        assert unsupported_magnitudes("1株当たり 9.99 と記載", STATEMENT_QUOTE) == ()

    def test_a_fact_without_any_figure_is_ignored(self):
        assert (
            unsupported_magnitudes("増収基調が続いたと記載されている", "no digits here")
            == ()
        )

    def test_a_quote_without_any_figure_cannot_support_a_stated_figure(self):
        assert unsupported_magnitudes(
            "営業収益は34億9,530万ドルだった", "operating revenues increased"
        ) == ("34億9,530万",)

    def test_an_empty_quote_supports_nothing(self):
        assert unsupported_magnitudes("営業収益は34億ドル", "") == ("34億",)

    def test_a_count_expressed_in_a_magnitude_word_is_still_checked(self):
        """The marker, not the noun, decides scope -- 3万 reads as a magnitude."""
        assert (
            unsupported_magnitudes("従業員は3万人", "approximately 30,000 employees")
            == ()
        )


class TestMultipleFigures:
    def test_every_stated_figure_is_matched_against_every_quoted_one(self):
        assert (
            unsupported_magnitudes(
                "営業収益は34億9,530万ドル、前年同期は29億2,818万ドル",
                STATEMENT_QUOTE,
            )
            == ()
        )

    def test_only_the_unreachable_figure_of_several_is_reported(self):
        assert unsupported_magnitudes(
            "営業収益は35億9,530万ドル、前年同期は29億2,818万ドル", STATEMENT_QUOTE
        ) == ("35億9,530万",)

    def test_several_unreachable_figures_are_reported_in_written_order(self):
        assert unsupported_magnitudes(
            "営業収益は48億ドル、営業利益は7億ドル", STATEMENT_QUOTE
        ) == ("48億", "7億")


class TestPresentationDifferences:
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("営業収益は34億9,530万ドル", id="ascii-digits"),
            pytest.param(
                "３４億９,５３０万ドル",
                id="fullwidth-digits",
            ),
            pytest.param("営業収益は 34億 9,530万 ドル", id="spaced-terms"),
            pytest.param("Revenues were $3.50 BILLION", id="uppercase-scale-word"),
        ],
    )
    def test_normalization_precedes_the_comparison(self, text):
        assert unsupported_magnitudes(text, STATEMENT_QUOTE) == ()

    def test_separate_figures_are_not_merged_into_one_composite(self):
        """34億 and 9,530万 only reconcile when written as one 34億9,530万."""
        assert unsupported_magnitudes(
            "34億ドルと9,530万ドルを対比した", STATEMENT_QUOTE
        ) == ("34億", "9,530万")

    def test_a_larger_following_magnitude_does_not_join_the_composite(self):
        """Composite terms shrink, so 4兆 cannot be a term inside 7億."""
        assert unsupported_magnitudes("7億4兆ドル", STATEMENT_QUOTE) == ("7億", "4兆")

    def test_a_following_year_is_not_absorbed_as_a_composite_remainder(self):
        """Only a term carrying its own smaller magnitude joins a composite."""
        assert (
            unsupported_magnitudes("1兆2,345億円は2026年の見通し", "1,234.5 billion")
            == ()
        )
