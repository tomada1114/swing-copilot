"""Split arithmetic: Yahoo response -> raw bars, raw bars -> as-of prices.

The golden fixtures are the real 2026-09-02 MNST response that Issues #413
and #421 were diagnosed from. `MNST_RESPONSE` is the seven weeks around the
2026-08-11 2:1 split, at full OHLCV; `MNST_SPLIT_BOUNDARIES` is the five
sessions before and the one session on each of MNST's six ex-dates, taken
from the same 1990-2026 response. Together they pin both halves of the
resolution to observed provider behaviour: which splits Yahoo propagated
(only the 2026 one is missing, across 36 years) and which individual rows it
corrected anyway (five, scattered across three weeks).
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar

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

#: The Yahoo response (auto_adjust=False), as returned on 2026-09-02.
MNST_RESPONSE = [
    ("2026-07-15", 98.010002, 99.029999, 97.290001, 97.570000, 6207900),
    ("2026-07-16", 98.050003, 99.980003, 97.570000, 99.940002, 9115800),
    ("2026-07-17", 100.019997, 100.339996, 96.570000, 97.500000, 10755200),
    ("2026-07-20", 48.625000, 48.985001, 47.680000, 47.724998, 10113200),
    ("2026-07-21", 48.040001, 48.040001, 46.889999, 47.230000, 8477600),
    ("2026-07-22", 47.310001, 47.900002, 47.200001, 47.834999, 9663000),
    ("2026-07-23", 94.269997, 94.540001, 93.120003, 93.559998, 3869000),
    ("2026-07-24", 93.459999, 93.989998, 92.949997, 93.489998, 5948800),
    ("2026-07-27", 94.209999, 95.610001, 94.000000, 95.330002, 3774300),
    ("2026-07-28", 97.459999, 99.169998, 97.019997, 97.739998, 7052100),
    ("2026-07-29", 97.449997, 99.290001, 96.959999, 97.230003, 4552200),
    ("2026-07-30", 96.540001, 97.730003, 95.830002, 97.650002, 6540900),
    ("2026-07-31", 48.700001, 48.700001, 48.139999, 48.189999, 7765200),
    ("2026-08-03", 97.349998, 97.349998, 92.639999, 93.550003, 7080600),
    ("2026-08-04", 93.519997, 94.389999, 92.430000, 94.180000, 6807800),
    ("2026-08-05", 94.910004, 95.169998, 93.860001, 94.459999, 4333100),
    ("2026-08-06", 47.000000, 47.599998, 46.404999, 47.080002, 13658800),
    ("2026-08-07", 93.790001, 93.900002, 89.500000, 90.360001, 8504300),
    ("2026-08-11", 46.369999, 46.619999, 45.160000, 45.529999, 9579000),
    ("2026-08-12", 45.430000, 46.009998, 45.049999, 45.980000, 8544900),
    ("2026-08-13", 46.439999, 46.970001, 46.049999, 46.680000, 9858700),
    ("2026-08-14", 46.509998, 46.939999, 46.169998, 46.820000, 7930700),
    ("2026-08-17", 46.500000, 46.630001, 45.320000, 45.520000, 9817800),
    ("2026-08-18", 45.930000, 47.549999, 45.779999, 47.380001, 16318800),
    ("2026-08-19", 47.279999, 47.980000, 47.209999, 47.430000, 11953400),
    ("2026-08-20", 47.070000, 47.910000, 46.959999, 47.490002, 9057100),
    ("2026-08-21", 47.599998, 48.119999, 47.220001, 47.790001, 12531300),
    ("2026-08-24", 48.099998, 49.119999, 48.090000, 48.919998, 11667300),
    ("2026-08-25", 48.860001, 49.240002, 48.340000, 48.730000, 7087600),
    ("2026-08-26", 48.939999, 48.939999, 47.790001, 47.810001, 6220800),
    ("2026-08-27", 46.990002, 47.070000, 46.439999, 46.700001, 7431000),
    ("2026-08-31", 46.509998, 46.660000, 45.779999, 45.919998, 12161200),
    ("2026-09-01", 45.970001, 46.099998, 44.889999, 44.990002, 7278700),
    ("2026-09-02", 45.340000, 45.369999, 44.040001, 44.419998, 10508139),
]

MNST_SPLITS = (
    SplitEvent(ex_date=date(2005, 8, 9), factor=2.0),
    SplitEvent(ex_date=date(2006, 7, 10), factor=4.0),
    SplitEvent(ex_date=date(2012, 2, 16), factor=2.0),
    SplitEvent(ex_date=date(2016, 11, 10), factor=3.0),
    SplitEvent(ex_date=date(2023, 3, 28), factor=2.0),
    SplitEvent(ex_date=date(2026, 8, 11), factor=2.0),
)

MNST_SPLIT_BOUNDARIES = {
    date(2005, 8, 9): [
        ("2005-08-02", 0.971875),
        ("2005-08-03", 1.007500),
        ("2005-08-04", 1.004688),
        ("2005-08-05", 0.952708),
        ("2005-08-08", 1.005833),
        ("2005-08-09", 0.970833),
    ],
    date(2006, 7, 10): [
        ("2006-06-30", 3.966042),
        ("2006-07-03", 4.129375),
        ("2006-07-05", 4.278125),
        ("2006-07-06", 4.243125),
        ("2006-07-07", 4.245833),
        ("2006-07-10", 3.852500),
    ],
    date(2012, 2, 16): [
        ("2012-02-09", 9.025833),
        ("2012-02-10", 9.024167),
        ("2012-02-13", 9.105833),
        ("2012-02-14", 9.189167),
        ("2012-02-15", 8.875833),
        ("2012-02-16", 8.831667),
    ],
    date(2016, 11, 10): [
        ("2016-11-03", 23.368334),
        ("2016-11-04", 22.538334),
        ("2016-11-07", 22.690001),
        ("2016-11-08", 22.666668),
        ("2016-11-09", 22.098333),
        ("2016-11-10", 21.165001),
    ],
    date(2023, 3, 28): [
        ("2023-03-21", 51.895000),
        ("2023-03-22", 51.174999),
        ("2023-03-23", 51.195000),
        ("2023-03-24", 52.040001),
        ("2023-03-27", 52.334999),
        ("2023-03-28", 51.599998),
    ],
    date(2026, 8, 11): [
        ("2026-08-03", 93.550003),
        ("2026-08-04", 94.180000),
        ("2026-08-05", 94.459999),
        ("2026-08-06", 47.080002),
        ("2026-08-07", 90.360001),
        ("2026-08-11", 45.529999),
    ],
}

#: The one split inside the MNST_RESPONSE window.
MNST_SPLIT = MNST_SPLITS[-1]
#: Sessions Yahoo had already halved, against the response's majority basis.
MNST_CORRECTED_SESSIONS = (
    date(2026, 7, 20),
    date(2026, 7, 21),
    date(2026, 7, 22),
    date(2026, 7, 31),
    date(2026, 8, 6),
)


def _mnst_response() -> pd.DataFrame:
    """MNST_RESPONSE as a BARS_COLUMNS frame."""
    return pd.DataFrame(
        [
            {
                "symbol": "MNST",
                "date": date.fromisoformat(bar_date),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
            for bar_date, open_, high, low, close, volume in MNST_RESPONSE
        ]
    )


def _boundary_bars(ex_date: date) -> pd.DataFrame:
    """The real sessions around one MNST ex-date, as a bars frame."""
    return _bars(
        "MNST",
        [(day, close, 1000) for day, close in MNST_SPLIT_BOUNDARIES[ex_date]],
    )


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
    """The gate `write_bars` and `copilot-backfill check` share."""

    SPLIT = (SplitEvent(ex_date=date(2026, 7, 10), factor=2.0),)

    def test_a_quiet_series_has_no_signature(self) -> None:
        closes = pd.Series([100.0, 101.0, 99.5, 100.2, 103.0])

        assert has_mixed_basis_signature(closes, self.SPLIT) is False

    def test_a_single_real_shock_is_not_a_signature(self) -> None:
        """MRNA's 2026-08-19 +77% day: one jump, never mirrored back."""
        closes = pd.Series([25.0, 25.4, 45.0, 44.1, 46.0, 45.2])

        assert has_mixed_basis_signature(closes, self.SPLIT) is False

    def test_a_real_split_in_a_raw_series_is_not_a_signature(self) -> None:
        """A raw series steps down once on the ex-date and stays there."""
        closes = pd.Series([97.2, 97.6, 96.4, 93.5, 45.5, 45.9, 46.2])

        assert has_mixed_basis_signature(closes, self.SPLIT) is False

    def test_alternating_bases_are_a_signature(self) -> None:
        closes = pd.Series([97.2, 48.6, 97.6, 96.4])

        assert has_mixed_basis_signature(closes, self.SPLIT) is True

    def test_ordinary_sessions_between_the_two_jumps_still_count(self) -> None:
        """The reversal is looked for in the *jump* sequence, not day to day."""
        closes = pd.Series([97.2, 48.6, 48.7, 48.5, 48.8, 97.6])

        assert has_mixed_basis_signature(closes, self.SPLIT) is True

    def test_a_single_jump_is_never_a_signature(self) -> None:
        closes = pd.Series([97.2, 48.6, 48.7])

        assert has_mixed_basis_signature(closes, self.SPLIT) is False

    def test_non_positive_and_non_finite_values_contribute_no_ratio(self) -> None:
        closes = pd.Series([100.0, 0.0, float("nan"), 100.5, 101.0])

        assert has_mixed_basis_signature(closes, self.SPLIT) is False

    def test_an_empty_series_is_not_a_signature(self) -> None:
        assert (
            has_mixed_basis_signature(pd.Series([], dtype=float), self.SPLIT) is False
        )

    def test_a_symbol_with_no_splits_can_never_carry_a_signature(self) -> None:
        """`^VIX` doubling and halving back is volatility, not a basis.

        Issue #421: scanned without splits this pattern flagged 153 of 510
        stored symbols. With no corporate action there is no second basis for
        the series to be quoted on, so the question is not merely hard — it
        is meaningless.
        """
        closes = pd.Series([97.2, 48.6, 97.6, 96.4])

        assert has_mixed_basis_signature(closes, ()) is False

    def test_a_reversing_pair_that_is_not_split_sized_is_not_a_signature(self) -> None:
        """A 45% swing back and forth under a 2:1 split explains nothing."""
        closes = pd.Series([100.0, 68.0, 99.0, 98.0])

        assert has_mixed_basis_signature(closes, self.SPLIT) is False

    def test_the_flip_is_matched_against_every_supplied_factor(self) -> None:
        """A 3:1 flip is a signature once the symbol's 3:1 split is known."""
        closes = pd.Series([99.0, 33.0, 99.5, 99.2])
        splits = (SplitEvent(ex_date=date(2026, 7, 10), factor=3.0),)

        assert has_mixed_basis_signature(closes, self.SPLIT) is False
        assert has_mixed_basis_signature(closes, splits) is True


class TestFirstMixedBasisJump:
    """The reporting counterpart: which session `check` tells an operator about."""

    SPLIT = (SplitEvent(ex_date=date(2026, 7, 10), factor=2.0),)

    def test_a_clean_series_names_no_session(self) -> None:
        assert (
            first_mixed_basis_jump(pd.Series([100.0, 101.0, 99.5]), self.SPLIT) is None
        )

    def test_a_single_real_split_names_no_session(self) -> None:
        closes = pd.Series([97.2, 97.6, 96.4, 45.5, 45.9])

        assert first_mixed_basis_jump(closes, self.SPLIT) is None

    def test_it_names_the_first_row_quoted_on_the_other_basis(self) -> None:
        # Index 1 is the halved row: the jump down lands *on* it, and the
        # jump back up at index 2 is what proves the pair reverses.
        closes = pd.Series([97.2, 48.6, 97.6, 96.4])

        assert first_mixed_basis_jump(closes, self.SPLIT) == 1

    def test_a_non_reversing_jump_pair_is_walked_past(self) -> None:
        # The first pair of jumps (x2 then x2) compounds rather than
        # reversing, so it is skipped; the x2/x0.5 pair that follows is the
        # flip, and index 2 is where it starts.
        closes = pd.Series([25.0, 50.0, 100.0, 50.0, 100.0])

        assert first_mixed_basis_jump(closes, self.SPLIT) == 2

    def test_an_empty_series_names_no_session(self) -> None:
        assert first_mixed_basis_jump(pd.Series([], dtype=float), self.SPLIT) is None

    def test_a_symbol_with_no_splits_names_no_session(self) -> None:
        closes = pd.Series([97.2, 48.6, 97.6, 96.4])

        assert first_mixed_basis_jump(closes, ()) is None


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

    def test_a_split_too_small_to_read_is_taken_at_the_provider_s_word(self) -> None:
        """A 1.1:1 split's boundary reads the same either way, so trust Yahoo.

        Its two readings — "propagated" (no step) and "not propagated" (a
        step of 1.1) — overlap inside one ordinary session's move, so there
        is no evidence to act on. Guessing wrong would corrupt the history
        silently, while trusting the documented contract is what every
        release before Issue #413 did unconditionally.
        """
        response = _flat("TINY", [100.0, 55.0, 100.5, 100.2])
        splits = [SplitEvent(ex_date=date(2026, 7, 4), factor=1.1)]

        raw = unadjust_yahoo_bars("TINY", response, splits)

        assert isinstance(raw, pd.DataFrame)
        assert list(raw["close"]) == pytest.approx([110.0, 60.5, 110.55, 100.2])

    def test_a_symbol_with_no_splits_is_returned_untouched(self) -> None:
        """Alternating rows without a split are volatility, not two bases.

        Issue #421: this response used to be rejected, and with it `^VIX`,
        `^TNX` and every other split-free symbol whose history contains a
        doubling and a halving. There is no adjustment to undo here, so
        `close` *is* the as-traded price.
        """
        response = _flat("VOLATILE", [100.0, 50.0, 100.5, 50.2, 100.8])

        raw = unadjust_yahoo_bars("VOLATILE", response, [])

        assert isinstance(raw, pd.DataFrame)
        assert list(raw["close"]) == pytest.approx([100.0, 50.0, 100.5, 50.2, 100.8])

    def test_rows_no_hypothesis_explains_are_rejected(self) -> None:
        """Two un-propagated splits, and a row corrected for only one of them.

        Rows before 07-06 are missing both the 2:1 and the 3:1 (a factor of
        6), so the walk offers each of them exactly two readings — corrected
        or not. 2026-07-03 sits at a third, `close / 3`, which neither
        explains, and the flip survives into the reconstruction.
        """
        response = _bars(
            "ODD",
            [
                ("2026-07-01", 600.0, 1000),
                ("2026-07-02", 600.0, 1000),
                ("2026-07-03", 200.0, 1000),
                ("2026-07-06", 300.0, 1000),
                ("2026-07-07", 300.0, 1000),
                ("2026-07-08", 300.0, 1000),
                ("2026-07-09", 300.0, 1000),
                ("2026-07-10", 300.0, 1000),
                ("2026-07-13", 100.0, 1000),
                ("2026-07-14", 100.0, 1000),
                ("2026-07-15", 100.0, 1000),
                ("2026-07-16", 100.0, 1000),
                ("2026-07-17", 100.0, 1000),
            ],
        )
        splits = [
            SplitEvent(ex_date=date(2026, 7, 6), factor=2.0),
            SplitEvent(ex_date=date(2026, 7, 13), factor=3.0),
        ]

        rejection = unadjust_yahoo_bars("ODD", response, splits)

        assert isinstance(rejection, NormalizationRejection)
        assert rejection.symbol == "ODD"
        assert "未伝播の分割" in rejection.reason
        assert "ex_date=2026-07-06" in rejection.reason
        assert "ex_date=2026-07-13" in rejection.reason


class TestMnstGolden:
    """Issues #413 and #421's real response (see the module docstring)."""

    #: Every as-traded close the response resolves to, to the cent. The five
    #: sessions in `MNST_CORRECTED_SESSIONS` are the ones that move.
    EXPECTED: ClassVar[dict[date, float]] = {
        date(2026, 7, 15): 97.57,
        date(2026, 7, 16): 99.94,
        date(2026, 7, 17): 97.50,
        date(2026, 7, 20): 95.45,
        date(2026, 7, 21): 94.46,
        date(2026, 7, 22): 95.67,
        date(2026, 7, 23): 93.56,
        date(2026, 7, 24): 93.49,
        date(2026, 7, 27): 95.33,
        date(2026, 7, 28): 97.74,
        date(2026, 7, 29): 97.23,
        date(2026, 7, 30): 97.65,
        date(2026, 7, 31): 96.38,
        date(2026, 8, 3): 93.55,
        date(2026, 8, 4): 94.18,
        date(2026, 8, 5): 94.46,
        date(2026, 8, 6): 94.16,
        date(2026, 8, 7): 90.36,
        date(2026, 8, 11): 45.53,
        date(2026, 8, 12): 45.98,
    }

    def test_the_mixed_response_normalizes_to_the_as_traded_series(self) -> None:
        raw = unadjust_yahoo_bars("MNST", _mnst_response(), [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        closes = dict(zip(raw["date"], raw["close"], strict=True))
        for day, expected in self.EXPECTED.items():
            assert closes[day] == pytest.approx(expected, abs=5e-3), day

    def test_the_two_entry_prices_the_ledger_recorded_are_reproduced(self) -> None:
        """The ledger's MNST fills, which is what Issue #413 corrupted.

        These two rows Yahoo happened to return un-halved, so they survived
        the bad ingest and are the fixed points the repair has to land on.
        """
        raw = unadjust_yahoo_bars("MNST", _mnst_response(), [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        closes = dict(zip(raw["date"], raw["close"], strict=True))
        assert closes[date(2026, 7, 29)] == pytest.approx(97.23, abs=5e-3)
        assert closes[date(2026, 8, 3)] == pytest.approx(93.55, abs=5e-3)

    def test_exactly_the_halved_sessions_are_doubled_back(self) -> None:
        """Every other row was already as-traded and must be handed back as is."""
        response = _mnst_response()

        raw = unadjust_yahoo_bars("MNST", response, [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        before = dict(zip(response["date"], response["close"], strict=True))
        after = dict(zip(raw["date"], raw["close"], strict=True))
        moved = {day for day in before if after[day] != pytest.approx(before[day])}
        assert moved == set(MNST_CORRECTED_SESSIONS)

    def test_the_reclassified_rows_volume_is_un_adjusted_too(self) -> None:
        raw = unadjust_yahoo_bars("MNST", _mnst_response(), [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        volumes = dict(zip(raw["date"], raw["volume"], strict=True))
        assert volumes[date(2026, 8, 6)] == 6_829_400
        assert volumes[date(2026, 7, 20)] == 5_056_600
        assert volumes[date(2026, 8, 3)] == 7_080_600

    def test_the_ohlc_of_a_reclassified_row_moves_with_its_close(self) -> None:
        raw = unadjust_yahoo_bars("MNST", _mnst_response(), [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        row = raw[raw["date"] == date(2026, 8, 6)].iloc[0]
        assert row["open"] == pytest.approx(94.0, abs=5e-3)
        assert row["high"] == pytest.approx(95.2, abs=5e-3)
        assert row["low"] == pytest.approx(92.81, abs=5e-3)

    def test_the_normalized_series_no_longer_carries_the_signature(self) -> None:
        raw = unadjust_yahoo_bars("MNST", _mnst_response(), [MNST_SPLIT])

        assert isinstance(raw, pd.DataFrame)
        assert has_mixed_basis_signature(raw["close"], [MNST_SPLIT]) is False

    def test_the_untouched_response_does_carry_the_signature(self) -> None:
        """The write gate still refuses the raw response, which is its job."""
        assert (
            has_mixed_basis_signature(_mnst_response()["close"], [MNST_SPLIT]) is True
        )

    def test_reading_it_as_of_the_split_halves_every_earlier_row(self) -> None:
        raw = unadjust_yahoo_bars("MNST", _mnst_response(), [MNST_SPLIT])
        assert isinstance(raw, pd.DataFrame)

        adjusted = adjust_bars(raw, {"MNST": [MNST_SPLIT]}, as_of=date(2026, 8, 12))

        closes = dict(zip(adjusted["date"], adjusted["close"], strict=True))
        assert closes[date(2026, 7, 29)] == pytest.approx(48.615, abs=5e-3)
        assert closes[date(2026, 8, 7)] == pytest.approx(45.18, abs=5e-3)
        assert closes[date(2026, 8, 11)] == pytest.approx(45.53, abs=5e-3)


class TestMnstSplitPropagation:
    """Which of MNST's six splits Yahoo pushed back through 36 years.

    Read from the real sessions around each ex-date. Only the newest is
    missing — the finding Issue #421's per-row search was built on the
    opposite of, and the reason the whole history needs one factor undone
    rather than a different one per era.
    """

    PROPAGATED = (
        date(2005, 8, 9),
        date(2006, 7, 10),
        date(2012, 2, 16),
        date(2016, 11, 10),
        date(2023, 3, 28),
    )

    @pytest.mark.parametrize("ex_date", PROPAGATED)
    def test_an_older_split_leaves_no_step_and_is_left_alone(
        self, ex_date: date
    ) -> None:
        """A propagated split is invisible in the adjusted series it produced."""
        split = next(event for event in MNST_SPLITS if event.ex_date == ex_date)
        bars = _boundary_bars(ex_date)

        raw = unadjust_yahoo_bars("MNST", bars, [split])

        assert isinstance(raw, pd.DataFrame)
        # Straight through the fast path: every row multiplied by its own
        # cumulative factor, nothing reclassified.
        expected = [
            close * (split.factor if date.fromisoformat(day) < ex_date else 1.0)
            for day, close in MNST_SPLIT_BOUNDARIES[ex_date]
        ]
        assert list(raw["close"]) == pytest.approx(expected)

    def test_the_2026_split_is_seen_as_un_propagated(self) -> None:
        """Its boundary steps by the factor, so the history is missing it.

        2026-08-06 is one of the five sessions Yahoo *did* correct, which is
        why several pre-ex sessions vote rather than one: on its own it would
        say the split had been propagated.
        """
        ex_date = date(2026, 8, 11)
        split = next(event for event in MNST_SPLITS if event.ex_date == ex_date)

        raw = unadjust_yahoo_bars("MNST", _boundary_bars(ex_date), [split])

        assert isinstance(raw, pd.DataFrame)
        closes = dict(zip(raw["date"], raw["close"], strict=True))
        # Un-propagated, so the baseline rows are already as-traded and only
        # the corrected one is doubled back.
        assert closes[date(2026, 8, 3)] == pytest.approx(93.55, abs=5e-3)
        assert closes[date(2026, 8, 6)] == pytest.approx(94.16, abs=5e-3)
        assert closes[date(2026, 8, 11)] == pytest.approx(45.53, abs=5e-3)


class TestUnadjustEdgeCases:
    def test_a_split_postdating_every_bar_is_taken_at_the_provider_s_word(
        self,
    ) -> None:
        """No boundary in the window, so no evidence: trust the contract.

        A response window ends at "today", so this is only reachable when the
        newest bar is older than the newest split — a halted or delisted
        symbol. Yahoo will have adjusted every one of those rows, which is
        what `close x cum` assumes; and if it has not, the mixed series it
        produces is what `write_bars`' gate refuses.
        """
        response = _flat("LATE", [100.0, 101.0, 100.5, 100.4])
        splits = [SplitEvent(ex_date=date(2026, 7, 10), factor=2.0)]

        raw = unadjust_yahoo_bars("LATE", response, splits)

        assert isinstance(raw, pd.DataFrame)
        assert list(raw["close"]) == pytest.approx([200.0, 202.0, 201.0, 200.8])

    def test_a_zero_close_never_raises_and_is_left_as_it_is(self) -> None:
        """A zero price takes part in no ratio, so it is carried through.

        The split is un-propagated here, so the row-by-row walk does run and
        has to step over the unusable row rather than divide by it.
        """
        response = _flat(
            "ZERO", [100.0, 100.0, 0.0, 100.0, 100.0, 100.0, 50.0, 50.5], start_day=3
        )
        splits = [SplitEvent(ex_date=date(2026, 7, 9), factor=2.0)]

        raw = unadjust_yahoo_bars("ZERO", response, splits)

        assert isinstance(raw, pd.DataFrame)
        assert raw.iloc[2]["close"] == pytest.approx(0.0)
        assert raw.iloc[0]["close"] == pytest.approx(100.0)
        assert raw.iloc[6]["close"] == pytest.approx(50.0)

    def test_a_leading_run_of_corrected_rows_is_read_as_a_price_move(self) -> None:
        """MNST's 1996-05-06 in miniature: a real doubling, not a basis flip.

        Walking backwards, the +100% session at the front of the series looks
        exactly like Yahoo having corrected everything before it. Nothing
        flips back, though, so the "corrected" run runs off the start — and a
        split Yahoo never propagated cannot have reached the oldest rows
        alone. The run is read back as baseline, which leaves the doubling
        where it belongs, in the price.
        """
        response = _flat(
            "EARLY", [50.0, 100.0, 100.0, 100.0, 100.0, 100.0, 50.0, 50.5], start_day=3
        )
        splits = [SplitEvent(ex_date=date(2026, 7, 9), factor=2.0)]

        raw = unadjust_yahoo_bars("EARLY", response, splits)

        assert isinstance(raw, pd.DataFrame)
        # 50 -> 100 as-traded, not 100 -> 100 with the first row doubled.
        assert list(raw["close"]) == pytest.approx(
            [50.0, 100.0, 100.0, 100.0, 100.0, 100.0, 50.0, 50.5]
        )

    def test_an_unusable_split_factor_is_ignored_everywhere(self) -> None:
        """A zero or negative factor is not arithmetic anyone can undo."""
        response = _flat("ODDF", [100.0, 50.0, 100.5, 50.2, 100.8])
        splits = [SplitEvent(ex_date=date(2026, 7, 3), factor=0.0)]

        raw = unadjust_yahoo_bars("ODDF", response, splits)

        assert isinstance(raw, pd.DataFrame)
        assert list(raw["close"]) == pytest.approx([100.0, 50.0, 100.5, 50.2, 100.8])
        assert has_mixed_basis_signature(response["close"], splits) is False

    def test_an_unusable_close_casts_no_vote_on_a_split(self) -> None:
        """A zero in the sessions before an ex-date simply does not vote."""
        response = _flat(
            "GAP", [100.0, 0.0, 100.0, 100.0, 100.0, 100.0, 50.0, 50.5], start_day=3
        )
        splits = [SplitEvent(ex_date=date(2026, 7, 9), factor=2.0)]

        raw = unadjust_yahoo_bars("GAP", response, splits)

        assert isinstance(raw, pd.DataFrame)
        # The four usable pre-ex sessions still carry the vote.
        assert raw.iloc[0]["close"] == pytest.approx(100.0)
        assert raw.iloc[6]["close"] == pytest.approx(50.0)

    def test_a_frame_without_a_volume_column_is_still_normalized(self) -> None:
        """`_apply_basis` touches only the columns the frame actually has."""
        response = _flat("NOVOL", [100.0, 100.0, 100.0, 100.0, 50.0, 50.5]).drop(
            columns=["volume", "open", "high", "low"]
        )
        splits = [SplitEvent(ex_date=date(2026, 7, 5), factor=2.0)]

        raw = unadjust_yahoo_bars("NOVOL", response, splits)

        assert isinstance(raw, pd.DataFrame)
        assert "volume" not in raw.columns
        assert list(raw["close"]) == pytest.approx(
            [100.0, 100.0, 100.0, 100.0, 50.0, 50.5]
        )


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
