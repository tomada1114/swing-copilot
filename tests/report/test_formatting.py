"""Tests for the shared display-layer number formatters (Issue #399).

Every expected string here is a literal, independent of the implementation
under test -- the whole point of this module is that a report medium or a
CLI can no longer drift from these strings by hand-copying its own formula.
"""

from __future__ import annotations

from swing_copilot.report.formatting import (
    format_fraction_pct,
    format_money,
    format_number,
    format_one_r,
    format_ratio,
)


class TestFormatNumber:
    def test_none_is_not_available(self) -> None:
        assert format_number(None) == "N/A"

    def test_zero(self) -> None:
        assert format_number(0) == "0.00"

    def test_negative(self) -> None:
        assert format_number(-1234.5678) == "-1,234.57"

    def test_large_value_gets_thousands_separator(self) -> None:
        assert format_number(1234.5678) == "1,234.57"

    def test_fraction(self) -> None:
        assert format_number(0.1234) == "0.12"

    def test_custom_digits(self) -> None:
        assert format_number(1234.5678, digits=1) == "1,234.6"


class TestFormatMoney:
    def test_none_is_not_available(self) -> None:
        assert format_money(None) == "N/A"

    def test_zero(self) -> None:
        assert format_money(0) == "$0.00"

    def test_negative(self) -> None:
        assert format_money(-1234.5678) == "$-1,234.57"

    def test_large_value_gets_thousands_separator(self) -> None:
        assert format_money(1234.5678) == "$1,234.57"

    def test_fraction(self) -> None:
        assert format_money(0.1234) == "$0.12"


class TestFormatOneR:
    def test_none_is_not_available(self) -> None:
        assert format_one_r(None) == "N/A"

    def test_zero(self) -> None:
        assert format_one_r(0) == "0.00%"

    def test_negative(self) -> None:
        assert format_one_r(-1234.5678) == "-123456.78%"

    def test_large_value(self) -> None:
        assert format_one_r(1234.5678) == "123456.78%"

    def test_fraction(self) -> None:
        assert format_one_r(0.1234) == "12.34%"


class TestFormatFractionPct:
    def test_none_is_not_available(self) -> None:
        assert format_fraction_pct(None) == "N/A"

    def test_zero_unsigned(self) -> None:
        assert format_fraction_pct(0) == "0.00%"

    def test_negative_unsigned(self) -> None:
        assert format_fraction_pct(-1234.5678) == "-123456.78%"

    def test_large_value_unsigned(self) -> None:
        assert format_fraction_pct(1234.5678) == "123456.78%"

    def test_fraction_unsigned(self) -> None:
        assert format_fraction_pct(0.1234) == "12.34%"

    def test_zero_signed(self) -> None:
        assert format_fraction_pct(0, signed=True) == "+0.00%"

    def test_negative_signed(self) -> None:
        assert format_fraction_pct(-1234.5678, signed=True) == "-123456.78%"

    def test_large_value_signed(self) -> None:
        assert format_fraction_pct(1234.5678, signed=True) == "+123456.78%"

    def test_fraction_signed(self) -> None:
        assert format_fraction_pct(0.1234, signed=True) == "+12.34%"

    def test_none_signed_is_still_not_available(self) -> None:
        assert format_fraction_pct(None, signed=True) == "N/A"

    def test_custom_digits(self) -> None:
        assert format_fraction_pct(0.1234, digits=1) == "12.3%"


class TestFormatRatio:
    def test_none_is_not_available(self) -> None:
        assert format_ratio(None) == "N/A"

    def test_zero(self) -> None:
        assert format_ratio(0) == "0.000"

    def test_negative(self) -> None:
        assert format_ratio(-1234.5678) == "-1234.568"

    def test_large_value(self) -> None:
        assert format_ratio(1234.5678) == "1234.568"

    def test_fraction(self) -> None:
        assert format_ratio(0.1234) == "0.123"

    def test_custom_digits(self) -> None:
        assert format_ratio(1234.5678, digits=2) == "1234.57"
