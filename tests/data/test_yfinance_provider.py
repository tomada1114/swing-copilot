"""Tests specific to YFinanceProvider (FR-02, CON-02)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
import yfinance as yf

from swing_copilot.data.yfinance_provider import YFinanceProvider


def _frame(
    columns: dict[tuple[str, str], list[float]], dates: list[str]
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
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: pd.DataFrame())
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

    def test_download_exception_marks_all_symbols_retryable_failure(self):
        def _boom(*_args, **_kwargs):
            msg = "network unreachable"
            raise RuntimeError(msg)

        provider = YFinanceProvider(download_fn=_boom)
        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )

        assert result.bars.empty
        assert {failure.symbol for failure in result.failures} == {"AAPL", "MSFT"}
        assert all(failure.retryable for failure in result.failures)

    def test_real_download_fn_default_is_yfinance_download(self):

        provider = YFinanceProvider()
        assert provider._download_fn is yf.download  # noqa: SLF001 - verifying the real default wiring


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
        provider = YFinanceProvider(download_fn=lambda *_a, **_k: pd.DataFrame())

        result = provider.get_latest_bars(["AAPL"], date(2026, 7, 17))

        assert result.bars.empty
        assert {failure.symbol for failure in result.failures} == {"AAPL"}
