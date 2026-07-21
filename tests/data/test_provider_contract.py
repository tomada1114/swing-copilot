"""Shared DataProvider contract tests (FR-02, NFR-07).

Every DataProvider implementation must satisfy this contract. Adding a new
provider (e.g. EODHD at P4) means adding one entry to `PROVIDER_FACTORIES`
below — these tests themselves never change, which is what lets a new
provider be added without touching existing code.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from swing_copilot.data.base import BARS_COLUMNS
from swing_copilot.data.yfinance_provider import YFinanceProvider


def _multi_index_frame(
    data: dict[tuple[str, str], list[float]], dates: list[str]
) -> pd.DataFrame:
    columns = pd.MultiIndex.from_tuples(list(data.keys()), names=["Price", "Ticker"])
    frame = pd.DataFrame(dict(data.items()), index=pd.to_datetime(dates))
    frame.columns = columns
    return frame


def _yfinance_two_symbol_fixture() -> pd.DataFrame:
    dates = ["2026-07-15", "2026-07-16", "2026-07-17"]
    return _multi_index_frame(
        {
            ("Open", "AAPL"): [10.0, 11.0, 12.0],
            ("High", "AAPL"): [10.5, 11.5, 12.5],
            ("Low", "AAPL"): [9.5, 10.5, 11.5],
            ("Close", "AAPL"): [10.2, 11.2, 12.2],
            ("Volume", "AAPL"): [1000, 1100, 1200],
            ("Open", "MSFT"): [20.0, 21.0, 22.0],
            ("High", "MSFT"): [20.5, 21.5, 22.5],
            ("Low", "MSFT"): [19.5, 20.5, 21.5],
            ("Close", "MSFT"): [20.2, 21.2, 22.2],
            ("Volume", "MSFT"): [2000, 2100, 2200],
        },
        dates,
    )


def _make_yfinance_provider():
    def fake_download(symbols, start, end, **kwargs):
        return _yfinance_two_symbol_fixture()

    return YFinanceProvider(download_fn=fake_download)


PROVIDER_FACTORIES = (_make_yfinance_provider,)
PROVIDER_IDS = ("yfinance",)


@pytest.mark.parametrize("factory", PROVIDER_FACTORIES, ids=PROVIDER_IDS)
class TestDataProviderContract:
    def test_returns_expected_bars_schema(self, factory):
        provider = factory()
        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )
        assert tuple(result.bars.columns) == BARS_COLUMNS
        assert not result.failures

    def test_adjusted_ohlc_values_pass_through(self, factory):
        provider = factory()
        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )
        aapl_first = (
            result.bars[result.bars["symbol"] == "AAPL"].sort_values("date").iloc[0]
        )
        assert aapl_first["open"] == pytest.approx(10.0)
        assert aapl_first["close"] == pytest.approx(10.2)

    def test_multi_symbol_multiindex_is_normalized_to_tidy_rows(self, factory):
        provider = factory()
        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 18)
        )
        assert set(result.bars["symbol"]) == {"AAPL", "MSFT"}
        assert len(result.bars) == 6  # 2 symbols x 3 trading days

    def test_end_date_is_exclusive(self, factory):
        provider = factory()
        result = provider.get_daily_bars(
            ["AAPL", "MSFT"], date(2026, 7, 15), date(2026, 7, 17)
        )
        assert date(2026, 7, 17) not in set(result.bars["date"])
        assert date(2026, 7, 16) in set(result.bars["date"])

    def test_unknown_symbol_reported_as_failure_not_exception(self, factory):
        provider = factory()
        result = provider.get_daily_bars(
            ["AAPL", "DOESNOTEXIST"], date(2026, 7, 15), date(2026, 7, 18)
        )
        assert "DOESNOTEXIST" in {failure.symbol for failure in result.failures}
        assert "AAPL" in set(result.bars["symbol"])
