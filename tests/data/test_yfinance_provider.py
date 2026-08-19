"""Tests specific to YFinanceProvider (FR-02, CON-02)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

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
