"""Split arithmetic: Yahoo response -> raw bars, raw bars -> as-of prices.

The golden fixture in `TestMnstGolden` is the real 2026-09-02 MNST response
that Issue #413 was diagnosed from, reduced to the two weeks around the
2026-08-11 2:1 split. It is the one case that pins the classification path to
observed provider behavior rather than to a hand-built idea of it.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from swing_copilot.data.adjustments import (
    NormalizationRejection,
    SplitEvent,
    adjust_bars,
    cumulative_split_factors,
    first_mixed_basis_jump,
    has_mixed_basis_signature,
    unadjust_yahoo_bars,
)

#: The Yahoo response (`auto_adjust=False`) reduced to close and volume. The
#: comment on each row is the basis Yahoo actually returned it on.
MNST_RESPONSE = [
    ("2026-07-29", 97.230003, 4552200),  # unadjusted
    ("2026-07-30", 97.650002, 6540900),  # unadjusted
    ("2026-07-31", 48.189999, 7765200),  # adjusted
    ("2026-08-03", 93.550003, 7080600),  # unadjusted
    ("2026-08-04", 94.180000, 6807800),  # unadjusted
    ("2026-08-05", 94.459999, 4333100),  # unadjusted
    ("2026-08-06", 47.080002, 13658800),  # adjusted
    ("2026-08-07", 90.360001, 8504300),  # unadjusted
    ("2026-08-11", 45.529999, 9579000),  # ex-date, raw
    ("2026-08-12", 45.980000, 8544900),
]
MNST_SPLIT = SplitEvent(ex_date=date(2026, 8, 11), factor=2.0)


def _bars(
    symbol: str, rows: list[tuple[str, float, int]], *, volume_ratio: float = 1.0
) -> pd.DataFrame:
    """A `BARS_COLUMNS` frame whose OHLC all track the close."""
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "date": date.fromisoformat(bar_date),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": int(volume * volume_ratio),
            }
            for bar_date, close, volume in rows
        ]
    )


def _flat(symbol: str, closes: list[float], start_day: int = 1) -> pd.DataFrame:
    """One bar per consecutive July 2026 day, at the given closes."""
    return _bars(
        symbol,
        [
            (f"2026-07-{start_day + offset:02d}", close, 1000)
            for offset, close in enumerate(closes)
        ],
    )


class TestHasMixedBasisSignature:
    def test_a_quiet_series_has_no_signature(self) -> None:
        closes = pd.Series([100.0, 101.0, 99.5, 100.2, 103.0])

        assert has_mixed_basis_signature(closes) is False

    def test_a_single_real_shock_is_not_a_signature(self) -> None:
        """MRNA's 2026-08-19 +77% day: one jump, never mirrored back."""
        closes = pd.Series([25.0, 25.4, 45.0, 44.1, 46.0, 45.2])

        assert has_mixed_basis_signature(closes) is False

    def test_a_real_split_in_a_raw_series_is_not_a_signature(self) -> None:
        """A raw series steps down once on the ex-date and stays there."""
        closes = pd.Series([97.2, 97.6, 96.4, 93.5, 45.5, 45.9, 46.2])

        assert has_mixed_basis_signature(closes) is False

    def test_alternating_bases_are_a_signature(self) -> None:
        closes = pd.Series([97.2, 48.6, 97.6, 96.4])

        assert has_mixed_basis_signature(closes) is True

    def test_ordinary_sessions_between_the_two_jumps_still_count(self) -> None:
        """The reversal is looked for in the *jump* sequence, not day to day."""
        closes = pd.Series([97.2, 48.6, 48.7, 48.5, 48.8, 97.6])

        assert has_mixed_basis_signature(closes) is True

    def test_a_single_jump_is_never_a_signature(self) -> None:
        closes = pd.Series([97.2, 48.6, 48.7])

        assert has_mixed_basis_signature(closes) is False

    def test_non_positive_and_non_finite_values_contribute_no_ratio(self) -> None:
        closes = pd.Series([100.0, 0.0, float("nan"), 100.5, 101.0])

        assert has_mixed_basis_signature(closes) is False

    def test_an_empty_series_is_not_a_signature(self) -> None:
        assert has_mixed_basis_signature(pd.Series([], dtype=float)) is False


class TestFirstMixedBasisJump:
    """The reporting counterpart: which session `check` tells an operator about."""

    def test_a_clean_series_names_no_session(self) -> None:
        assert first_mixed_basis_jump(pd.Series([100.0, 101.0, 99.5])) is None

    def test_a_single_real_split_names_no_session(self) -> None:
        closes = pd.Series([97.2, 97.6, 96.4, 45.5, 45.9])

        assert first_mixed_basis_jump(closes) is None

    def test_it_names_the_first_row_quoted_on_the_other_basis(self) -> None:
        # Index 1 is the halved row: the jump down lands *on* it, and the
        # jump back up at index 2 is what proves the pair reverses.
        closes = pd.Series([97.2, 48.6, 97.6, 96.4])

        assert first_mixed_basis_jump(closes) == 1

    def test_a_non_reversing_jump_pair_is_walked_past(self) -> None:
        # The first pair of jumps (x2 then x2) compounds rather than
        # reversing, so it is skipped; the x2/x0.5 pair that follows is the
        # flip, and index 2 is where it starts.
        closes = pd.Series([25.0, 50.0, 100.0, 50.0, 100.0])

        assert first_mixed_basis_jump(closes) == 2

    def test_an_empty_series_names_no_session(self) -> None:
        assert first_mixed_basis_jump(pd.Series([], dtype=float)) is None


class TestCumulativeSplitFactors:
    def test_no_splits_leaves_every_row_at_one(self) -> None:
        dates = pd.Series([date(2026, 7, 1), date(2026, 7, 2)])

        factors = cumulative_split_factors(dates, [], as_of=date(2026, 12, 31))

        assert list(factors) == [1.0, 1.0]

    def test_only_rows_before_the_ex_date_carry_the_factor(self) -> None:
        dates = pd.Series([date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)])

        factors = cumulative_split_factors(dates, [MNST_SPLIT], as_of=date(2026, 8, 31))

        assert list(factors) == [2.0, 1.0, 1.0]

    def test_two_splits_multiply(self) -> None:
        dates = pd.Series([date(2026, 1, 2), date(2026, 8, 31)])
        splits = [MNST_SPLIT, SplitEvent(ex_date=date(2026, 3, 2), factor=3.0)]

        factors = cumulative_split_factors(dates, splits, as_of=date(2026, 12, 31))

        assert list(factors) == [6.0, 1.0]

    def test_a_split_after_as_of_contributes_nothing(self) -> None:
        dates = pd.Series([date(2026, 8, 10)])

        factors = cumulative_split_factors(dates, [MNST_SPLIT], as_of=date(2026, 8, 10))

        assert list(factors) == [1.0]


class TestAdjustBars:
    @staticmethod
    def _raw() -> pd.DataFrame:
        return _bars(
            "MNST",
            [
                ("2026-08-10", 90.0, 1000),
                ("2026-08-11", 45.5, 2000),
                ("2026-08-12", 46.0, 3000),
            ],
        )

    def test_the_day_before_the_ex_date_sees_no_split(self) -> None:
        adjusted = adjust_bars(
            self._raw(), {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 10)
        )

        assert adjusted.iloc[0]["close"] == pytest.approx(90.0)
        assert adjusted.iloc[0]["volume"] == 1000

    def test_the_ex_date_itself_applies_the_split_to_earlier_rows(self) -> None:
        adjusted = adjust_bars(
            self._raw(), {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 11)
        )

        assert adjusted.iloc[0]["close"] == pytest.approx(45.0)
        assert adjusted.iloc[0]["volume"] == 2000
        assert adjusted.iloc[1]["close"] == pytest.approx(45.5)

    def test_the_day_after_the_ex_date_is_unchanged_from_the_ex_date(self) -> None:
        adjusted = adjust_bars(
            self._raw(), {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12)
        )

        assert adjusted.iloc[0]["close"] == pytest.approx(45.0)

    def test_prices_divide_while_volume_multiplies(self) -> None:
        adjusted = adjust_bars(
            self._raw(), {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12)
        )
        row = adjusted.iloc[0]

        assert row["open"] == pytest.approx(45.0)
        assert row["high"] == pytest.approx(90.0 * 1.01 / 2)
        assert row["low"] == pytest.approx(90.0 * 0.99 / 2)
        assert row["volume"] == 2000

    def test_a_reverse_split_multiplies_prices_and_divides_volume(self) -> None:
        raw = _bars("REV", [("2026-08-10", 2.0, 10_000), ("2026-08-11", 20.0, 1000)])
        splits = [SplitEvent(ex_date=date(2026, 8, 11), factor=0.1)]

        adjusted = adjust_bars(raw, {"REV": splits}, as_of=date(2026, 8, 11))

        assert adjusted.iloc[0]["close"] == pytest.approx(20.0)
        assert adjusted.iloc[0]["volume"] == 1000

    def test_an_integer_volume_column_stays_integral(self) -> None:
        adjusted = adjust_bars(
            self._raw(), {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12)
        )

        assert pd.api.types.is_integer_dtype(adjusted["volume"].dtype)

    def test_a_symbol_without_splits_is_untouched(self) -> None:
        raw = pd.concat([self._raw(), _bars("AAPL", [("2026-08-10", 200.0, 5)])])

        adjusted = adjust_bars(raw, {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12))

        assert adjusted[adjusted["symbol"] == "AAPL"].iloc[0]["close"] == (
            pytest.approx(200.0)
        )

    def test_the_input_frame_is_never_mutated(self) -> None:
        raw = self._raw()

        adjust_bars(raw, {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12))

        assert raw.iloc[0]["close"] == pytest.approx(90.0)

    def test_an_empty_frame_is_returned_as_is(self) -> None:
        empty = self._raw().iloc[0:0]

        assert adjust_bars(empty, {"MNST": [MNST_SPLIT]}, date(2026, 8, 12)).empty


class TestUnadjustYahooBars:
    def test_the_fast_path_undoes_a_clean_adjustment(self) -> None:
        """Every row adjusted, as Yahoo promises: raw is `close x cum`."""
        response = _bars(
            "CLEAN",
            [
                ("2026-08-10", 45.0, 2000),
                ("2026-08-11", 45.5, 2000),
                ("2026-08-12", 46.0, 3000),
            ],
        )

        raw = unadjust_yahoo_bars("CLEAN", response, [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        assert list(raw["close"]) == pytest.approx([90.0, 45.5, 46.0])
        assert list(raw["volume"]) == [1000, 2000, 3000]

    def test_with_no_split_in_the_window_the_response_is_already_raw(self) -> None:
        response = _flat("AAPL", [100.0, 101.0, 100.5])

        raw = unadjust_yahoo_bars("AAPL", response, [])

        assert isinstance(raw, pd.DataFrame)
        assert list(raw["close"]) == pytest.approx([100.0, 101.0, 100.5])

    def test_an_empty_response_stays_empty(self) -> None:
        empty = _flat("AAPL", [100.0]).iloc[0:0]

        raw = unadjust_yahoo_bars("AAPL", empty, [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        assert raw.empty

    def test_rows_are_returned_in_ascending_date_order(self) -> None:
        response = _flat("AAPL", [100.0, 101.0, 100.5]).iloc[::-1]

        raw = unadjust_yahoo_bars("AAPL", response, [])

        assert isinstance(raw, pd.DataFrame)
        assert list(raw["date"]) == sorted(raw["date"])

    def test_a_split_too_small_to_classify_is_rejected(self) -> None:
        """A 1.1:1 split cannot separate the two hypotheses, so fail closed."""
        response = _flat("TINY", [100.0, 55.0, 100.5, 100.2])
        splits = [SplitEvent(ex_date=date(2026, 7, 4), factor=1.1)]

        rejection = unadjust_yahoo_bars("TINY", response, splits)

        assert isinstance(rejection, NormalizationRejection)
        assert rejection.symbol == "TINY"
        assert "分類不能" in rejection.reason
        assert "factor=1.1" in rejection.reason

    def test_a_signature_that_survives_classification_is_rejected(self) -> None:
        """Mixed rows with no split at all: nothing to reclassify them with."""
        response = _flat("BROKE", [100.0, 50.0, 100.5, 50.2, 100.8])

        rejection = unadjust_yahoo_bars("BROKE", response, [])

        assert isinstance(rejection, NormalizationRejection)
        assert "分割イベントが無い" in rejection.reason

    def test_the_rejection_names_the_splits_it_could_not_resolve(self) -> None:
        """Alternating rows the split factor does not explain: still rejected.

        A 3.3x flip is neither hypothesis under a 2:1 split, so no assignment
        of bases removes the signature and the symbol must be withheld.
        """
        response = _flat("ODD", [100.0, 30.0, 100.5, 30.2, 100.8, 100.9])
        splits = [SplitEvent(ex_date=date(2026, 7, 6), factor=2.0)]

        rejection = unadjust_yahoo_bars("ODD", response, splits)

        assert isinstance(rejection, NormalizationRejection)
        assert "ex_date=2026-07-06" in rejection.reason


class TestMnstGolden:
    """Issue #413's real response, reduced (see the module docstring)."""

    @staticmethod
    def _response() -> pd.DataFrame:
        return _bars("MNST", MNST_RESPONSE)

    def test_the_mixed_response_normalizes_to_the_as_traded_series(self) -> None:
        raw = unadjust_yahoo_bars("MNST", self._response(), [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        closes = dict(zip(raw["date"], raw["close"], strict=True))
        # Only the two rows Yahoo had adjusted move; every other row was
        # already as-traded and must be handed back untouched.
        assert closes[date(2026, 7, 31)] == pytest.approx(96.38, abs=1e-4)
        assert closes[date(2026, 8, 6)] == pytest.approx(94.16, abs=1e-4)
        assert closes[date(2026, 7, 29)] == pytest.approx(97.230003)
        assert closes[date(2026, 8, 3)] == pytest.approx(93.550003)
        assert closes[date(2026, 8, 11)] == pytest.approx(45.529999)
        assert closes[date(2026, 8, 12)] == pytest.approx(45.980000)

    def test_the_reclassified_rows_volume_is_un_adjusted_too(self) -> None:
        raw = unadjust_yahoo_bars("MNST", self._response(), [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        volumes = dict(zip(raw["date"], raw["volume"], strict=True))
        assert volumes[date(2026, 8, 6)] == 6_829_400
        assert volumes[date(2026, 8, 3)] == 7_080_600

    def test_the_normalized_series_no_longer_carries_the_signature(self) -> None:
        raw = unadjust_yahoo_bars("MNST", self._response(), [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        assert has_mixed_basis_signature(raw["close"]) is False

    def test_the_untouched_response_does_carry_the_signature(self) -> None:
        assert has_mixed_basis_signature(self._response()["close"]) is True

    def test_reading_it_as_of_the_split_halves_every_earlier_row(self) -> None:
        raw = unadjust_yahoo_bars("MNST", self._response(), [MNST_SPLIT])
        assert isinstance(raw, pd.DataFrame)

        adjusted = adjust_bars(raw, {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12))

        closes = dict(zip(adjusted["date"], adjusted["close"], strict=True))
        assert closes[date(2026, 7, 29)] == pytest.approx(48.615, abs=1e-4)
        assert closes[date(2026, 8, 7)] == pytest.approx(45.18, abs=1e-4)
        assert closes[date(2026, 8, 11)] == pytest.approx(45.529999)


class TestUnadjustEdgeCases:
    def test_the_anchor_is_re_seeded_when_the_window_ends_before_a_split(self) -> None:
        """The newest row is only unambiguous when no split postdates it.

        Here one does (07-10, outside the four-row window), so the backward
        pass seeded as "as-traded" leaves a jump between the last two rows;
        re-seeding the anchor on the adjusted hypothesis resolves the series.
        """
        response = _flat("LATE", [100.0, 50.0, 100.5, 50.2])
        splits = [
            SplitEvent(ex_date=date(2026, 6, 1), factor=1.1),  # before the window
            SplitEvent(ex_date=date(2026, 7, 10), factor=2.0),  # after the window
        ]

        raw = unadjust_yahoo_bars("LATE", response, splits)

        assert isinstance(raw, pd.DataFrame)
        assert list(raw["close"]) == pytest.approx([100.0, 100.0, 100.5, 100.4])

    def test_a_zero_close_never_raises_and_is_left_as_it_is(self) -> None:
        """A zero price takes part in no ratio, so it is carried through."""
        response = _flat("ZERO", [100.0, 50.0, 100.5, 0.0, 50.2, 100.8])
        splits = [SplitEvent(ex_date=date(2026, 7, 10), factor=2.0)]

        raw = unadjust_yahoo_bars("ZERO", response, splits)

        assert isinstance(raw, pd.DataFrame)
        assert raw.iloc[3]["close"] == pytest.approx(0.0)


class TestAdjustBarsColumnHandling:
    def test_a_split_for_a_symbol_with_no_rows_changes_nothing(self) -> None:
        raw = _bars("AAPL", [("2026-08-10", 200.0, 500)])

        adjusted = adjust_bars(raw, {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12))

        assert adjusted.iloc[0]["close"] == pytest.approx(200.0)

    def test_a_float_volume_column_is_scaled_without_rounding(self) -> None:
        raw = _bars("MNST", [("2026-08-10", 90.0, 1001), ("2026-08-11", 45.0, 1000)])
        raw["volume"] = raw["volume"].astype(float)

        adjusted = adjust_bars(raw, {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12))

        assert adjusted.iloc[0]["volume"] == pytest.approx(2002.0)

    def test_only_the_price_columns_present_are_scaled(self) -> None:
        raw = _bars("MNST", [("2026-08-10", 90.0, 1000)])[["symbol", "date", "close"]]

        adjusted = adjust_bars(raw, {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12))

        assert list(adjusted.columns) == ["symbol", "date", "close"]
        assert adjusted.iloc[0]["close"] == pytest.approx(45.0)
