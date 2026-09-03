"""Tests specific to YFinanceProvider (FR-02, CON-02)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, ClassVar

import pandas as pd
import pytest
import yfinance as yf

from swing_copilot.data.yfinance_provider import YFinanceProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: A known-good value per field, so a fixture can break exactly one cell.
_GOOD_VALUES: dict[str, float] = {
    "Open": 20.0,
    "High": 20.5,
    "Low": 19.5,
    "Close": 20.2,
    "Volume": 2000.0,
}


def _frame(
    columns: Mapping[tuple[str, str], Sequence[object]], dates: list[str]
) -> pd.DataFrame:
    index = pd.to_datetime(dates)
    frame = pd.DataFrame(dict(columns.items()), index=index)
    frame.columns = pd.MultiIndex.from_tuples(
        list(columns.keys()), names=["Price", "Ticker"]
    )
    return frame


class TestGetDailyBars:
    def test_empty_symbol_list_returns_empty_result_without_calling_download(self):
        def _boom(*_args, **_kwargs):
            msg = "download_fn must not be called for an empty symbol list"
            raise AssertionError(msg)

        provider = YFinanceProvider(download_fn=_boom)
        result = provider.get_daily_bars([], date(2026, 7, 15), date(2026, 7, 18))

        assert result.bars.empty
        assert result.failures == ()

    def test_empty_response_marks_all_symbols_failed(self):
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: pd.DataFrame(), sleep_fn=lambda _delay: None
        )
        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        assert result.bars.empty
        assert {failure.symbol for failure in result.failures} == {"AAPL", "MSFT"}
        assert all(failure.retryable for failure in result.failures)

    def test_all_nan_symbol_reported_as_non_retryable_failure(self):
        dates = ["2026-07-15", "2026-07-16"]
        fixture = _frame(
            {
                ("Open", "AAPL"): [10.0, 11.0],
                ("High", "AAPL"): [10.5, 11.5],
                ("Low", "AAPL"): [9.5, 10.5],
                ("Close", "AAPL"): [10.2, 11.2],
                ("Volume", "AAPL"): [1000, 1100],
                ("Open", "DELISTED"): [float("nan"), float("nan")],
                ("High", "DELISTED"): [float("nan"), float("nan")],
                ("Low", "DELISTED"): [float("nan"), float("nan")],
                ("Close", "DELISTED"): [float("nan"), float("nan")],
                ("Volume", "DELISTED"): [float("nan"), float("nan")],
            },
            dates,
        )
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: fixture)

        result = provider.get_daily_bars(
            ["AAPL", "DELISTED"], date(2026, 7, 15), date(2026, 7, 18)
        )

        failures = {failure.symbol: failure for failure in result.failures}
        assert "DELISTED" in failures
        assert failures["DELISTED"].retryable is False
        assert set(result.bars["symbol"]) == {"AAPL"}

    def test_transport_exception_retries_to_the_attempt_limit(self):
        calls = 0
        sleeps: list[float] = []

        def _boom(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            msg = "network unreachable"
            raise ConnectionError(msg)

        provider = YFinanceProvider(download_fn=_boom, sleep_fn=sleeps.append)
        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        assert result.bars.empty
        assert {failure.symbol for failure in result.failures} == {"AAPL", "MSFT"}
        assert all(failure.retryable for failure in result.failures)
        assert calls == 3
        assert sleeps == [1.0, 2.0]

    def test_partial_batch_retry_keeps_successful_symbols_out_of_second_call(self):
        dates = ["2026-07-15"]
        aapl = _frame(
            {
                ("Open", "AAPL"): [10.0],
                ("High", "AAPL"): [10.5],
                ("Low", "AAPL"): [9.5],
                ("Close", "AAPL"): [10.2],
                ("Volume", "AAPL"): [1000],
            },
            dates,
        )
        msft = _frame(
            {
                ("Open", "MSFT"): [20.0],
                ("High", "MSFT"): [20.5],
                ("Low", "MSFT"): [19.5],
                ("Close", "MSFT"): [20.2],
                ("Volume", "MSFT"): [2000],
            },
            dates,
        )
        requested_symbols: list[list[str]] = []

        def _download(symbols, **_kwargs):
            requested_symbols.append(symbols)
            return aapl if symbols == ["AAPL", "MSFT"] else msft

        provider = YFinanceProvider(download_fn=_download, sleep_fn=lambda _delay: None)
        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        assert requested_symbols == [["AAPL", "MSFT"], ["MSFT"]]
        assert set(result.bars["symbol"]) == {"AAPL", "MSFT"}
        assert result.failures == ()

    def test_validation_exception_is_not_retried(self):
        calls = 0

        def _invalid(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            msg = "invalid ticker argument"
            raise ValueError(msg)

        provider = YFinanceProvider(download_fn=_invalid, sleep_fn=lambda _delay: None)
        result = provider.get_daily_bars(["AAPL"], date(2026, 7, 15), date(2026, 7, 18))

        assert calls == 1
        assert result.failures[0].retryable is False

    def test_passes_explicit_timeout_to_every_download_attempt(self):
        timeouts: list[object] = []

        def _download(*_args, **kwargs):
            timeouts.append(kwargs["timeout"])
            return pd.DataFrame()

        provider = YFinanceProvider(download_fn=_download, sleep_fn=lambda _delay: None)
        provider.get_daily_bars(["AAPL"], date(2026, 7, 15), date(2026, 7, 18))

        assert timeouts == [10, 10, 10]

    def test_real_download_fn_default_is_yfinance_download(self):

        provider = YFinanceProvider()
        assert provider._download_fn is yf.download  # noqa: SLF001 - verifying the real default wiring


class TestNonFiniteValues:
    """Issue #249: a broken cell is a `failures` entry, never an exception.

    `data/base.py` states the contract these pin: "Per-symbol failures are
    returned via `BarFetchResult.failures`, never raised, so a batch fetch
    always completes with whatever symbols succeeded." Before the guard, a
    NaN `Volume` slipped past the `Close` check and `int(nan)` raised
    `ValueError` straight out of `get_daily_bars` — on the unattended 18:30
    run, one thin or halted ticker would have cost the whole day's fetch.
    """

    @staticmethod
    def _fixture(field: str, bad_value: object) -> pd.DataFrame:
        """Two symbols over two days; `MSFT`'s `field` is broken on day two."""
        columns: dict[tuple[str, str], list[object]] = {
            ("Open", "AAPL"): [10.0, 11.0],
            ("High", "AAPL"): [10.5, 11.5],
            ("Low", "AAPL"): [9.5, 10.5],
            ("Close", "AAPL"): [10.2, 11.2],
            ("Volume", "AAPL"): [1000, 1100],
            ("Open", "MSFT"): [20.0, 21.0],
            ("High", "MSFT"): [20.5, 21.5],
            ("Low", "MSFT"): [19.5, 20.5],
            ("Close", "MSFT"): [20.2, 21.2],
            ("Volume", "MSFT"): [2000, 2100],
        }
        columns[field, "MSFT"] = [_GOOD_VALUES[field], bad_value]
        return _frame(columns, ["2026-07-15", "2026-07-16"])

    def test_nan_volume_is_reported_as_a_failure_instead_of_raising(self):
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: self._fixture("Volume", float("nan")),
            sleep_fn=lambda _delay: None,
        )

        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        failures = {failure.symbol: failure for failure in result.failures}
        assert set(failures) == {"MSFT"}
        assert "Volume" in failures["MSFT"].reason
        assert "2026-07-16" in failures["MSFT"].reason
        # The healthy symbol in the same batch still comes back.
        assert set(result.bars["symbol"]) == {"AAPL"}

    def test_a_broken_bar_discards_the_symbols_whole_window(self):
        """Not a per-row drop: a hole in a price window is invisible later."""
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: self._fixture("Volume", float("nan")),
            sleep_fn=lambda _delay: None,
        )

        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        # MSFT's 2026-07-15 bar was fine, and is still not emitted.
        assert result.bars[result.bars["symbol"] == "MSFT"].empty

    def test_a_broken_bar_is_a_validation_error_and_is_not_retried(self):
        calls = 0

        def _download(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return self._fixture("Volume", float("nan"))

        def _no_sleep(_delay):
            pytest.fail("a validation error must not be retried")

        provider = YFinanceProvider(download_fn=_download, sleep_fn=_no_sleep)

        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        assert calls == 1
        assert result.failures[0].retryable is False

    @pytest.mark.parametrize("field", ["Open", "High", "Low", "Close"])
    def test_a_non_finite_ohlc_field_fails_the_symbol_too(self, field: str) -> None:
        """A NaN `Close` skips the row; an infinite one is a broken bar."""
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: self._fixture(field, float("inf")),
            sleep_fn=lambda _delay: None,
        )

        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        assert {failure.symbol for failure in result.failures} == {"MSFT"}
        assert set(result.bars["symbol"]) == {"AAPL"}

    def test_a_non_numeric_cell_fails_the_symbol_instead_of_raising(self):
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: self._fixture("Close", "n/a"),
            sleep_fn=lambda _delay: None,
        )

        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        assert {failure.symbol for failure in result.failures} == {"MSFT"}

    def test_a_numeric_string_volume_does_not_escape_as_a_value_error(self):
        """`float("2100.5")` accepts what `int("2100.5")` rejects.

        The finiteness check parses each cell once and the row is built from
        that number, so a numeric string cannot pass validation and then blow
        up in `int()` — the same escape out of `get_daily_bars` this class
        pins shut for NaN.
        """
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: self._fixture("Volume", "2100.5"),
            sleep_fn=lambda _delay: None,
        )

        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        assert result.failures == ()
        msft = result.bars[result.bars["symbol"] == "MSFT"]
        assert msft["volume"].tolist() == [2000, 2100]

    def test_an_all_nan_row_is_still_skipped_rather_than_failing_the_symbol(self):
        """The deliberate asymmetry: no `Close` means no trading row here."""
        fixture = _frame(
            {
                ("Open", "AAPL"): [10.0, float("nan"), 12.0],
                ("High", "AAPL"): [10.5, float("nan"), 12.5],
                ("Low", "AAPL"): [9.5, float("nan"), 11.5],
                ("Close", "AAPL"): [10.2, float("nan"), 12.2],
                ("Volume", "AAPL"): [1000, float("nan"), 1200],
            },
            ["2026-07-15", "2026-07-16", "2026-07-17"],
        )
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: fixture)

        result = provider.get_daily_bars(["AAPL"], date(2026, 7, 15), date(2026, 7, 18))

        assert result.failures == ()
        assert result.bars["date"].tolist() == [date(2026, 7, 15), date(2026, 7, 17)]

    def test_a_broken_bar_outside_the_window_does_not_fail_the_symbol(self):
        """The `[start, end)` clamp runs first, so an unrequested bar is inert."""
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: self._fixture("Volume", float("nan")),
            sleep_fn=lambda _delay: None,
        )

        # `end` is exclusive, so only 2026-07-15 is in range and the broken
        # 2026-07-16 bar sits outside it.
        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 16)
        )

        assert result.failures == ()
        assert set(result.bars["symbol"]) == {"AAPL", "MSFT"}

    def test_get_latest_bars_also_reports_the_symbol_instead_of_raising(self):
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: self._fixture("Volume", float("nan")),
            sleep_fn=lambda _delay: None,
        )

        result = provider.get_latest_bars(["AAPL", "MSFT"], date(2026, 7, 16))

        assert {failure.symbol for failure in result.failures} == {"MSFT"}
        assert set(result.bars["symbol"]) == {"AAPL"}


class TestDuplicateTimestamps:
    """Issue #294: a duplicate response timestamp is a `failures` entry too.

    Before the fix, `.loc[timestamp]` against a duplicated `DatetimeIndex`
    returns a `Series` instead of a scalar; `pd.isna(...)` on that `Series`
    used in an `if` raises `ValueError: The truth value of a Series is
    ambiguous`, escaping `get_daily_bars` as an uncaught exception -- the same
    `data/base.py` contract violation Issue #249 closed for non-finite cells.
    """

    @staticmethod
    def _fixture_with_duplicate(dates: list[str]) -> pd.DataFrame:
        n = len(dates)
        columns: dict[tuple[str, str], list[object]] = {
            ("Open", "AAPL"): [10.0 + i for i in range(n)],
            ("High", "AAPL"): [10.5 + i for i in range(n)],
            ("Low", "AAPL"): [9.5 + i for i in range(n)],
            ("Close", "AAPL"): [10.2 + i for i in range(n)],
            ("Volume", "AAPL"): [1000 + i for i in range(n)],
        }
        return _frame(columns, dates)

    def test_duplicate_timestamp_is_reported_as_a_failure_instead_of_raising(self):
        fixture = self._fixture_with_duplicate(
            ["2026-08-13", "2026-08-14", "2026-08-14"]
        )
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: fixture, sleep_fn=lambda _delay: None
        )

        result = provider.get_daily_bars(["AAPL"], date(2026, 8, 13), date(2026, 8, 15))

        failures = {failure.symbol: failure for failure in result.failures}
        assert set(failures) == {"AAPL"}
        assert "duplicate timestamp" in failures["AAPL"].reason
        assert "2026-08-14" in failures["AAPL"].reason
        assert "2 rows" in failures["AAPL"].reason
        assert result.bars.empty

    def test_a_duplicate_timestamp_is_a_validation_error_and_is_not_retried(self):
        calls = 0

        def _download(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return self._fixture_with_duplicate(
                ["2026-08-13", "2026-08-14", "2026-08-14"]
            )

        def _no_sleep(_delay):
            pytest.fail("a duplicate timestamp must not be retried")

        provider = YFinanceProvider(download_fn=_download, sleep_fn=_no_sleep)

        result = provider.get_daily_bars(["AAPL"], date(2026, 8, 13), date(2026, 8, 15))

        assert calls == 1
        assert result.failures[0].retryable is False

    def test_a_duplicate_timestamp_outside_the_window_does_not_fail_the_symbol(self):
        fixture = self._fixture_with_duplicate(
            ["2026-08-13", "2026-08-14", "2026-08-14"]
        )
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: fixture, sleep_fn=lambda _delay: None
        )

        # `end` is exclusive: only 2026-08-13 is requested, so the duplicated
        # 2026-08-14 rows sit outside `[start, end)` and stay inert.
        result = provider.get_daily_bars(["AAPL"], date(2026, 8, 13), date(2026, 8, 14))

        assert result.failures == ()
        assert result.bars["date"].tolist() == [date(2026, 8, 13)]

    def test_get_latest_bars_also_reports_duplicate_timestamp_instead_of_raising(self):
        fixture = self._fixture_with_duplicate(
            ["2026-08-13", "2026-08-14", "2026-08-14"]
        )
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: fixture, sleep_fn=lambda _delay: None
        )

        result = provider.get_latest_bars(["AAPL"], date(2026, 8, 14))

        assert {failure.symbol for failure in result.failures} == {"AAPL"}
        assert result.bars.empty


class TestGetLatestBars:
    def test_returns_single_most_recent_bar_per_symbol(self):
        dates = ["2026-07-15", "2026-07-16", "2026-07-17"]
        fixture = _frame(
            {
                ("Open", "AAPL"): [10.0, 11.0, 12.0],
                ("High", "AAPL"): [10.5, 11.5, 12.5],
                ("Low", "AAPL"): [9.5, 10.5, 11.5],
                ("Close", "AAPL"): [10.2, 11.2, 12.2],
                ("Volume", "AAPL"): [1000, 1100, 1200],
            },
            dates,
        )
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: fixture)

        result = provider.get_latest_bars(["AAPL"], date(2026, 7, 17))

        assert len(result.bars) == 1
        assert result.bars.iloc[0]["date"] == date(2026, 7, 17)
        assert result.bars.iloc[0]["close"] == pytest.approx(12.2)

    def test_symbol_with_no_bars_in_window_is_a_failure(self):
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: pd.DataFrame(), sleep_fn=lambda _delay: None
        )

        result = provider.get_latest_bars(["AAPL"], date(2026, 7, 17))

        assert result.bars.empty
        assert {failure.symbol for failure in result.failures} == {"AAPL"}


class TestRawBarsAndCorporateActions:
    """Issue #413: the response is stored as-traded, actions travel with it."""

    #: Sessions an unresolvable response needs: two un-propagated splits, and
    #: room for the five pre-ex sessions that vote on each of them.
    _LONG_DATES: ClassVar[list[str]] = [
        "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07",
        "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13", "2026-07-14",
        "2026-07-15", "2026-07-16", "2026-07-17",
    ]  # fmt: skip
    #: Rows before 07-06 are missing both splits (a factor of 6); 2026-07-03
    #: sits at `close / 3`, which neither of a row's two readings explains.
    _UNRESOLVABLE_CLOSES: ClassVar[list[float]] = [
        600.0, 600.0, 200.0, 300.0, 300.0, 300.0, 300.0,
        300.0, 100.0, 100.0, 100.0, 100.0, 100.0,
    ]  # fmt: skip
    _UNRESOLVABLE_SPLITS: ClassVar[list[float]] = [
        0.0,
        0.0,
        0.0,
        2.0,
        0.0,
        0.0,
        0.0,
        0.0,
        3.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]

    @classmethod
    def _unresolvable_fixture(cls) -> pd.DataFrame:
        """A response no assignment of bases resolves, so the symbol is withheld."""
        return cls._split_fixture(
            cls._UNRESOLVABLE_CLOSES, cls._UNRESOLVABLE_SPLITS, dates=cls._LONG_DATES
        )

    @staticmethod
    def _split_fixture(
        closes: list[float], splits: list[float], *, dates: list[str] | None = None
    ) -> pd.DataFrame:
        """One symbol's response, Yahoo-adjusted, with a `Stock Splits` column."""
        dates = dates or ["2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16"]
        volume = [1000] * len(closes)
        zeros = [0.0] * len(closes)
        return _frame(
            {
                ("Open", "MNST"): closes,
                ("High", "MNST"): closes,
                ("Low", "MNST"): closes,
                ("Close", "MNST"): closes,
                ("Volume", "MNST"): volume,
                ("Dividends", "MNST"): zeros,
                ("Stock Splits", "MNST"): splits,
            },
            dates,
        )

    def test_download_asks_for_unadjusted_prices_and_actions(self):
        captured: list[dict[str, object]] = []

        def _download(*_args, **kwargs):
            captured.append(kwargs)
            return pd.DataFrame()

        provider = YFinanceProvider(download_fn=_download, sleep_fn=lambda _d: None)
        provider.get_daily_bars(["AAPL"], date(2026, 7, 15), date(2026, 7, 18))

        assert all(kwargs["auto_adjust"] is False for kwargs in captured)
        assert all(kwargs["actions"] is True for kwargs in captured)

    def test_bars_come_back_as_traded_not_split_adjusted(self):
        # Yahoo adjusted every row for the 07-16 2:1 split, so the two
        # pre-split rows print at half their as-traded price.
        fixture = self._split_fixture([50.0, 50.5, 50.2, 51.0], [0.0, 0.0, 0.0, 2.0])
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: fixture)

        result = provider.get_daily_bars(["MNST"], date(2026, 7, 13), date(2026, 7, 17))

        assert list(result.bars["close"]) == pytest.approx([100.0, 101.0, 100.4, 51.0])
        assert list(result.bars["volume"]) == [500, 500, 500, 1000]

    def test_a_split_and_a_dividend_become_action_rows(self):
        dates = ["2026-07-15", "2026-07-16"]
        fixture = _frame(
            {
                ("Open", "AAPL"): [20.0, 20.0],
                ("High", "AAPL"): [20.0, 20.0],
                ("Low", "AAPL"): [20.0, 20.0],
                ("Close", "AAPL"): [20.0, 20.0],
                ("Volume", "AAPL"): [1000, 1000],
                ("Dividends", "AAPL"): [0.0, 0.24],
                ("Stock Splits", "AAPL"): [4.0, 0.0],
            },
            dates,
        )
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: fixture)

        result = provider.get_daily_bars(["AAPL"], date(2026, 7, 15), date(2026, 7, 17))

        assert list(result.actions.columns) == ["symbol", "ex_date", "kind", "value"]
        assert [tuple(row) for row in result.actions.to_numpy()] == [
            ("AAPL", date(2026, 7, 15), "split", 4.0),
            ("AAPL", date(2026, 7, 16), "dividend", 0.24),
        ]

    def test_a_zero_valued_action_cell_is_no_action(self):
        fixture = self._split_fixture([50.0, 50.5, 50.2, 51.0], [0.0, 0.0, 0.0, 0.0])
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: fixture)

        result = provider.get_daily_bars(["MNST"], date(2026, 7, 13), date(2026, 7, 17))

        assert result.actions.empty
        assert list(result.bars["close"]) == pytest.approx([50.0, 50.5, 50.2, 51.0])

    def test_an_action_outside_the_window_is_not_reported(self):
        fixture = self._split_fixture([50.0, 50.5, 50.2, 51.0], [0.0, 0.0, 0.0, 2.0])
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: fixture)

        result = provider.get_daily_bars(["MNST"], date(2026, 7, 13), date(2026, 7, 16))

        assert result.actions.empty

    def test_a_response_without_action_columns_still_yields_bars(self):
        """A feed that ignores `actions=True` reads as "no corporate action"."""
        fixture = _frame(
            {
                ("Open", "AAPL"): [20.0],
                ("High", "AAPL"): [20.0],
                ("Low", "AAPL"): [20.0],
                ("Close", "AAPL"): [20.0],
                ("Volume", "AAPL"): [1000],
            },
            ["2026-07-15"],
        )
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: fixture)

        result = provider.get_daily_bars(["AAPL"], date(2026, 7, 15), date(2026, 7, 16))

        assert result.actions.empty
        assert result.bars.iloc[0]["close"] == pytest.approx(20.0)

    def test_an_unresolvable_adjustment_basis_fails_the_symbol(self):
        fixture = self._unresolvable_fixture()
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: fixture, sleep_fn=lambda _d: None
        )

        result = provider.get_daily_bars(["MNST"], date(2026, 7, 1), date(2026, 7, 18))

        assert result.bars.empty
        assert len(result.failures) == 1
        assert result.failures[0].symbol == "MNST"
        assert result.failures[0].retryable is False
        assert "分割調整の混在" in result.failures[0].reason

    def test_a_rejected_symbol_is_never_retried(self):
        calls = 0
        fixture = self._unresolvable_fixture()

        def _download(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return fixture

        provider = YFinanceProvider(download_fn=_download, sleep_fn=lambda _d: None)
        provider.get_daily_bars(["MNST"], date(2026, 7, 1), date(2026, 7, 18))

        assert calls == 1

    def test_a_rejected_symbol_contributes_no_actions_either(self):
        fixture = self._unresolvable_fixture()
        provider = YFinanceProvider(
            download_fn=lambda *_a, **_k: fixture, sleep_fn=lambda _d: None
        )

        result = provider.get_daily_bars(["MNST"], date(2026, 7, 1), date(2026, 7, 18))

        assert result.actions.empty

    def test_get_latest_bars_carries_the_actions_through(self):
        fixture = self._split_fixture([50.0, 50.5, 50.2, 51.0], [0.0, 0.0, 0.0, 2.0])
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: fixture)

        result = provider.get_latest_bars(["MNST"], date(2026, 7, 16))

        assert len(result.bars) == 1
        # The newest bar is always as-traded, so it is comparable with a
        # price quoted today without any adjustment.
        assert result.bars.iloc[0]["close"] == pytest.approx(51.0)
        assert list(result.actions["kind"]) == ["split"]
