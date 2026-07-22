"""Acceptance tests for `report/chart_data.py` (`docs/05_ui_design.md` 8.4/10.1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from swing_copilot.report.chart_data import build_chart_data
from swing_copilot.screening.indicators import sma
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore

_AS_OF = date(2026, 7, 20)
_DISPLAY_START = _AS_OF - timedelta(days=180)


@pytest.fixture
def market_store(tmp_path):
    database = Database(tmp_path / "copilot.duckdb")
    return MarketStore(database, parquet_root=tmp_path / "bars")


def _write_daily_bars(
    market_store: MarketStore, symbol: str, start: date, end: date
) -> None:
    days = pd.date_range(start, end, freq="D")
    rows = [
        {
            "symbol": symbol,
            "date": day.date(),
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 1_000_000 + i,
            "provider": "yfinance",
            "fetched_at": datetime(2026, 7, 20, tzinfo=UTC),
        }
        for i, day in enumerate(days)
    ]
    market_store.write_bars(pd.DataFrame(rows))


class TestBuildChartDataWithFullHistory:
    def test_slices_ohlcv_to_display_window_only(self, market_store):
        fetch_start = _DISPLAY_START - timedelta(days=320)
        _write_daily_bars(market_store, "AAPL", fetch_start, _AS_OF)

        result = build_chart_data("AAPL", market_store, _AS_OF)

        assert result.symbol == "AAPL"
        earliest = date.fromisoformat(result.ohlcv[0].time)
        latest = date.fromisoformat(result.ohlcv[-1].time)
        assert earliest >= _DISPLAY_START
        assert latest == _AS_OF
        assert len(result.ohlcv) == (_AS_OF - _DISPLAY_START).days + 1

    def test_time_fields_are_iso_date_strings(self, market_store):
        fetch_start = _DISPLAY_START - timedelta(days=320)
        _write_daily_bars(market_store, "AAPL", fetch_start, _AS_OF)

        result = build_chart_data("AAPL", market_store, _AS_OF)

        assert result.ohlcv[0].time == _DISPLAY_START.isoformat()
        assert result.ohlcv[-1].time == _AS_OF.isoformat()

    def test_sma_values_match_shared_indicator_function(self, market_store):
        fetch_start = _DISPLAY_START - timedelta(days=320)
        _write_daily_bars(market_store, "AAPL", fetch_start, _AS_OF)

        result = build_chart_data("AAPL", market_store, _AS_OF)

        bars = market_store.read_bars(
            ["AAPL"], fetch_start, _AS_OF, _AS_OF
        ).sort_values("date")
        expected_sma200 = sma(bars["close"], 200)
        expected_last = expected_sma200.iloc[-1]
        assert result.sma200[-1].value == pytest.approx(expected_last)

    def test_sma200_fully_covers_display_window_given_enough_buffer(self, market_store):
        fetch_start = _DISPLAY_START - timedelta(days=320)
        _write_daily_bars(market_store, "AAPL", fetch_start, _AS_OF)

        result = build_chart_data("AAPL", market_store, _AS_OF)

        assert len(result.sma200) == len(result.ohlcv)
        assert len(result.sma50) == len(result.ohlcv)


class TestBuildChartDataWithInsufficientHistory:
    def test_sma200_omits_days_without_enough_lookback_instead_of_zero_filling(
        self, market_store
    ):
        # Recently listed symbol: only 60 calendar days of history exist at all,
        # nowhere near SMA200's required window.
        start = _AS_OF - timedelta(days=59)
        _write_daily_bars(market_store, "NEWCO", start, _AS_OF)

        result = build_chart_data("NEWCO", market_store, _AS_OF)

        assert result.sma200 == []
        assert len(result.sma50) == 60 - 50 + 1
        assert len(result.ohlcv) == 60

    def test_no_bars_returns_empty_chart_data(self, market_store):
        result = build_chart_data("GHOST", market_store, _AS_OF)

        assert result.symbol == "GHOST"
        assert result.ohlcv == []
        assert result.sma50 == []
        assert result.sma200 == []
