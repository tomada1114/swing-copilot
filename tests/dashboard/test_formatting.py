"""The NULL vocabulary and the value formatters that resolve it."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from swing_copilot.dashboard import formatting as fmt


class TestIsMissing:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(None, id="none"),
            pytest.param(float("nan"), id="nan"),
            pytest.param(pd.NA, id="pandas-na"),
            pytest.param(pd.NaT, id="pandas-nat"),
        ],
    )
    def test_recognizes_every_sentinel_duckdb_produces(self, value: object) -> None:
        assert fmt.is_missing(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(0, id="zero-int"),
            pytest.param(0.0, id="zero-float"),
            pytest.param("", id="empty-string"),
            pytest.param(False, id="false"),
            pytest.param(math.inf, id="infinity"),
        ],
    )
    def test_a_falsy_value_is_present(self, value: object) -> None:
        # The whole point of the token vocabulary: zero is a measurement.
        assert fmt.is_missing(value) is False


class TestNumber:
    def test_absent_value_carries_the_named_token_and_its_explanation(self) -> None:
        cell = fmt.number(None, key="immature")

        assert cell.absence == "immature"
        assert cell.text == "未成熟"
        assert cell.title == fmt.NULL_TOKENS["immature"].explanation

    def test_zero_renders_as_a_value_not_as_a_token(self) -> None:
        cell = fmt.number(0.0)

        assert cell.absence is None
        assert cell.text == "0.00"

    @pytest.mark.parametrize(
        ("value", "expected_text", "expected_tone"),
        [
            pytest.param(3.5, "+3.50%", "pos", id="gain"),
            pytest.param(-1.25, "-1.25%", "neg", id="loss"),
            pytest.param(0.0, "+0.00%", "", id="flat"),
        ],
    )
    def test_signed_values_carry_direction_in_text_and_tone(
        self, value: float, expected_text: str, expected_tone: str
    ) -> None:
        cell = fmt.number(value, suffix="%", signed=True)

        assert (cell.text, cell.tone) == (expected_text, expected_tone)

    def test_infinity_is_treated_as_unrenderable(self) -> None:
        assert fmt.number(math.inf, key="absent").absence == "absent"


class TestText:
    def test_blank_string_reads_as_the_named_absence(self) -> None:
        assert fmt.text("   ", key="unrecorded").absence == "unrecorded"

    def test_value_is_stripped(self) -> None:
        assert fmt.text("  READY ").text == "READY"


class TestIntegerAndTones:
    def test_integer_formats_with_thousands_separator(self) -> None:
        assert fmt.integer(1200000).text == "1,200,000"

    def test_integer_absent_uses_the_named_token(self) -> None:
        assert fmt.integer(pd.NA, key="untracked").absence == "untracked"

    def test_unknown_state_falls_back_to_the_neutral_tone(self) -> None:
        assert fmt.tone_of(fmt.RUN_STATUS_TONES, "invented") == "quiet"

    def test_none_state_falls_back_to_the_neutral_tone(self) -> None:
        assert fmt.tone_of(fmt.GATE_TONES, None) == "quiet"


class TestLegend:
    def test_resolves_declared_keys_in_order(self) -> None:
        tokens = fmt.legend(("immature", "not_ingested"))

        assert [token.key for token in tokens] == ["immature", "not_ingested"]

    def test_an_unknown_key_fails_loudly(self) -> None:
        with pytest.raises(KeyError):
            fmt.legend(("no_such_token",))
