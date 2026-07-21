"""Tests for MarketStore: Parquet bars + DuckDB fundamentals (FR-02, FR-03)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import FundamentalsRecord, MarketStore


def _bars(rows: list[tuple[str, str, float, float, float, float, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "date": date.fromisoformat(bar_date),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
                "provider": "yfinance",
                "fetched_at": datetime(2026, 7, 20, tzinfo=UTC),
            }
            for symbol, bar_date, o, h, low, c, v in rows
        ]
    )


@pytest.fixture
def market_store(tmp_path):
    database = Database(tmp_path / "copilot.duckdb")
    return MarketStore(database, parquet_root=tmp_path / "bars")


class TestReadBarsEmptyState:
    def test_returns_empty_frame_when_no_partitions_exist(self, market_store):
        result = market_store.read_bars(
            ["AAPL"], date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 7, 20)
        )
        assert result.empty
        assert list(result.columns) == [
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "provider",
            "fetched_at",
        ]

    def test_returns_empty_frame_for_empty_symbol_list(self, market_store):
        result = market_store.read_bars(
            [], date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 7, 20)
        )
        assert result.empty


class TestWriteAndReadBars:
    def test_write_then_read_round_trip(self, market_store):
        market_store.write_bars(
            _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000)])
        )

        result = market_store.read_bars(
            ["AAPL"], date(2026, 7, 1), date(2026, 7, 31), as_of=date(2026, 7, 20)
        )

        assert len(result) == 1
        assert result.iloc[0]["symbol"] == "AAPL"
        assert result.iloc[0]["close"] == pytest.approx(10.2)

    def test_write_bars_is_idempotent_for_identical_rows(self, market_store):
        bars = _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000)])
        market_store.write_bars(bars)
        market_store.write_bars(bars)

        result = market_store.read_bars(
            ["AAPL"], date(2026, 7, 1), date(2026, 7, 31), as_of=date(2026, 7, 20)
        )
        assert len(result) == 1

    def test_write_bars_correction_replaces_same_natural_key(self, market_store):
        market_store.write_bars(
            _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000)])
        )
        market_store.write_bars(
            _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 11.5, 1000)])
        )

        result = market_store.read_bars(
            ["AAPL"], date(2026, 7, 1), date(2026, 7, 31), as_of=date(2026, 7, 20)
        )
        assert len(result) == 1
        assert result.iloc[0]["close"] == pytest.approx(11.5)

    def test_as_of_excludes_future_dated_bars(self, market_store):
        market_store.write_bars(
            _bars(
                [
                    ("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000),
                    ("AAPL", "2026-07-22", 11, 11.5, 10.5, 11.2, 1100),
                ]
            )
        )

        result = market_store.read_bars(
            ["AAPL"], date(2026, 7, 1), date(2026, 7, 31), as_of=date(2026, 7, 20)
        )

        assert list(result["date"]) == [date(2026, 7, 15)]

    def test_bars_across_multiple_years_partition_correctly(self, market_store):
        market_store.write_bars(
            _bars(
                [
                    ("AAPL", "2025-12-30", 9, 9.5, 8.5, 9.2, 900),
                    ("AAPL", "2026-01-02", 10, 10.5, 9.5, 10.2, 1000),
                ]
            )
        )

        result = market_store.read_bars(
            ["AAPL"], date(2025, 1, 1), date(2026, 12, 31), as_of=date(2026, 7, 20)
        )
        assert len(result) == 2
        assert (market_store.parquet_root / "year=2025").is_dir()
        assert (market_store.parquet_root / "year=2026").is_dir()

    def test_write_bars_with_empty_dataframe_is_a_no_op(self, market_store):
        market_store.write_bars(_bars([]))
        result = market_store.read_bars(
            ["AAPL"], date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 7, 20)
        )
        assert result.empty


class TestUpsertFundamentals:
    def test_insert_then_read_back(self, market_store):
        record = FundamentalsRecord(
            accession_no="0001-26-000001",
            symbol="AAPL",
            form="10-Q",
            fiscal_period_end=date(2026, 6, 30),
            filed_at=datetime(2026, 7, 10, tzinfo=UTC),
            revenue=1_000_000.0,
            net_income=200_000.0,
            fcf=150_000.0,
            equity=5_000_000.0,
            assets=10_000_000.0,
            shares=16_000_000_000.0,
            source_url="https://www.sec.gov/example",
            fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
        )

        market_store.upsert_fundamentals([record])

        with market_store.get_connection() as conn:
            rows = conn.execute(
                "SELECT accession_no, symbol, net_income FROM fundamentals"
            ).fetchall()
        assert rows == [("0001-26-000001", "AAPL", 200_000.0)]

    def test_upsert_replaces_same_accession_no(self, market_store):
        base = FundamentalsRecord(
            accession_no="0001-26-000001",
            symbol="AAPL",
            form="10-Q",
            fiscal_period_end=date(2026, 6, 30),
            filed_at=datetime(2026, 7, 10, tzinfo=UTC),
            revenue=1_000_000.0,
            net_income=200_000.0,
            fcf=150_000.0,
            equity=5_000_000.0,
            assets=10_000_000.0,
            shares=16_000_000_000.0,
            source_url="https://www.sec.gov/example",
            fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        corrected = FundamentalsRecord(
            accession_no="0001-26-000001",
            symbol="AAPL",
            form="10-Q/A",
            fiscal_period_end=date(2026, 6, 30),
            filed_at=datetime(2026, 7, 12, tzinfo=UTC),
            revenue=1_000_000.0,
            net_income=210_000.0,
            fcf=150_000.0,
            equity=5_000_000.0,
            assets=10_000_000.0,
            shares=16_000_000_000.0,
            source_url="https://www.sec.gov/example",
            fetched_at=datetime(2026, 7, 21, tzinfo=UTC),
        )

        market_store.upsert_fundamentals([base])
        market_store.upsert_fundamentals([corrected])

        with market_store.get_connection() as conn:
            rows = conn.execute(
                "SELECT form, net_income FROM fundamentals WHERE accession_no = '0001-26-000001'"
            ).fetchall()
        assert rows == [("10-Q/A", 210_000.0)]

    def test_upsert_empty_list_is_a_no_op(self, market_store):
        market_store.upsert_fundamentals([])
        with market_store.get_connection() as conn:
            count = conn.execute("SELECT count(*) FROM fundamentals").fetchone()
        assert count == (0,)
