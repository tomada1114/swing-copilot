"""The NULL vocabulary and the value formatters that resolve it."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from swing_copilot.dashboard import formatting as fmt
from swing_copilot.regime.distribution import DistributionLevel
from swing_copilot.regime.gate import GateVerdict


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

    def test_non_numeric_value_uses_the_named_token(self) -> None:
        assert fmt.number("not a number", key="absent").absence == "absent"


class TestScalarCoercion:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("not a number", id="text"),
            pytest.param(object(), id="object"),
        ],
    )
    def test_a_non_numeric_column_reads_as_absent(self, value: object) -> None:
        # `record.get()` is typed `object`: a schema change that turned a
        # numeric column into text must not crash a page.
        assert fmt.as_float(value) is None
        assert fmt.as_int(value) is None

    def test_a_numeric_string_is_still_read(self) -> None:
        assert fmt.as_float("2.5") == 2.5
        assert fmt.as_int("2.5") == 2

    def test_an_absent_value_yields_none(self) -> None:
        assert fmt.as_float(pd.NA) is None
        assert fmt.as_int(None) is None


class TestDay:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(date(2026, 7, 29), id="date"),
            pytest.param(datetime(2026, 7, 29, 0, 0, tzinfo=UTC), id="datetime"),
            pytest.param(pd.Timestamp("2026-07-29"), id="pandas-timestamp"),
        ],
    )
    def test_a_date_column_never_renders_a_time(self, value: object) -> None:
        # DuckDB DATE columns arrive as pandas Timestamps; `str()` would
        # append a midnight time that carries no information.
        assert fmt.day(value).text == "2026-07-29"

    def test_an_absent_date_uses_the_named_token(self) -> None:
        assert fmt.day(pd.NaT, key="untracked").absence == "untracked"

    def test_a_non_date_value_reads_as_absent(self) -> None:
        assert fmt.day("not a date").absence == "none"


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

    def test_integer_non_numeric_value_uses_the_named_token(self) -> None:
        assert fmt.integer("not a number", key="absent").absence == "absent"

    def test_unknown_state_falls_back_to_the_neutral_tone(self) -> None:
        assert fmt.tone_of(fmt.RUN_STATUS_TONES, "invented") == "quiet"

    def test_none_state_falls_back_to_the_neutral_tone(self) -> None:
        assert fmt.tone_of(fmt.GATE_TONES, None) == "quiet"


class TestRegimeVocabularies:
    """The tone tables must cover every member of the real enums.

    An unmapped level falls back to the neutral tone, so a missing `SEVERE`
    would paint the worst regime as the mildest — the one mistake a severity
    scale must not make.
    """

    def test_every_distribution_level_has_a_tone(self) -> None:
        assert set(fmt.DD_LEVEL_TONES) == {level.value for level in DistributionLevel}

    def test_every_gate_verdict_has_a_tone(self) -> None:
        assert set(fmt.GATE_TONES) == {verdict.value for verdict in GateVerdict}

    def test_drawdown_tones_rise_with_severity(self) -> None:
        severity = ["NORMAL", "CAUTION", "HIGH", "SEVERE"]
        assert [fmt.DD_LEVEL_TONES[level] for level in severity] == [
            "quiet",
            "warning",
            "serious",
            "critical",
        ]

    def test_an_undeterminable_level_is_not_shown_as_a_mild_value(self) -> None:
        # `distribution_severity` ranks UNKNOWN above SEVERE precisely so it
        # can never loosen a decision; the badge must not look ordinary.
        assert fmt.DD_LEVEL_TONES["UNKNOWN"] == "absent"
        assert fmt.GATE_TONES["UNKNOWN"] == "absent"


class TestLegend:
    def test_resolves_declared_keys_in_order(self) -> None:
        tokens = fmt.legend(("immature", "not_ingested"))

        assert [token.key for token in tokens] == ["immature", "not_ingested"]

    def test_an_unknown_key_fails_loudly(self) -> None:
        with pytest.raises(KeyError):
            fmt.legend(("no_such_token",))
