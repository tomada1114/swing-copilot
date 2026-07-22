"""Tests for MarketStore: Parquet bars + DuckDB fundamentals (FR-02, FR-03)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast

import pandas as pd
import pytest
from duckdb import ConstraintException

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

    def test_read_fundamentals_excludes_filings_after_as_of(self, market_store):
        records = [
            FundamentalsRecord(
                accession_no=f"acc-{filed_at.date().isoformat()}",
                symbol="AAPL",
                form="10-Q",
                fiscal_period_end=date(2026, 6, 30),
                filed_at=filed_at,
                revenue=1.0,
                net_income=1.0,
                fcf=1.0,
                equity=1.0,
                assets=2.0,
                shares=1.0,
                source_url="https://www.sec.gov/example",
                fetched_at=datetime(2026, 7, 30, tzinfo=UTC),
            )
            for filed_at in (
                datetime(2026, 7, 10, tzinfo=UTC),
                datetime(2026, 7, 25, tzinfo=UTC),
            )
        ]
        market_store.upsert_fundamentals(records)

        result = market_store.read_fundamentals(as_of=date(2026, 7, 20))

        assert result["accession_no"].tolist() == ["acc-2026-07-10"]

    def test_batch_rolls_back_when_a_later_record_is_invalid(self, market_store):
        valid = FundamentalsRecord(
            accession_no="valid",
            symbol="AAPL",
            form="10-Q",
            fiscal_period_end=date(2026, 6, 30),
            filed_at=datetime(2026, 7, 10, tzinfo=UTC),
            revenue=1.0,
            net_income=1.0,
            fcf=1.0,
            equity=1.0,
            assets=2.0,
            shares=1.0,
            source_url="https://www.sec.gov/valid",
            fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        invalid = FundamentalsRecord(
            accession_no="invalid",
            symbol="MSFT",
            form="10-Q",
            fiscal_period_end=date(2026, 6, 30),
            filed_at=datetime(2026, 7, 11, tzinfo=UTC),
            revenue=1.0,
            net_income=1.0,
            fcf=1.0,
            equity=1.0,
            assets=2.0,
            shares=1.0,
            source_url=cast("str", None),
            fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
        )

        with pytest.raises(ConstraintException):
            market_store.upsert_fundamentals([valid, invalid])

        assert market_store.read_fundamentals(date(2026, 7, 20)).empty


class TestGetLatestFundamentals:
    def test_returns_most_recently_filed_record_at_or_before_as_of(self, market_store):
        records = [
            FundamentalsRecord(
                accession_no=f"acc-{filed_at.date().isoformat()}",
                symbol="AAPL",
                form="10-Q",
                fiscal_period_end=date(2026, 6, 30),
                revenue=1.0,
                net_income=1.0,
                fcf=1.0,
                equity=1.0,
                assets=2.0,
                shares=1.0,
                source_url="https://www.sec.gov/example",
                filed_at=filed_at,
                fetched_at=datetime(2026, 7, 30, tzinfo=UTC),
            )
            for filed_at in (
                datetime(2026, 6, 1, tzinfo=UTC),
                datetime(2026, 7, 10, tzinfo=UTC),
                datetime(2026, 7, 25, tzinfo=UTC),
            )
        ]
        market_store.upsert_fundamentals(records)

        result = market_store.get_latest_fundamentals("AAPL", as_of=date(2026, 7, 20))

        assert result is not None
        assert result.accession_no == "acc-2026-07-10"

    def test_boundary_includes_filing_filed_exactly_on_as_of(self, market_store):
        record = FundamentalsRecord(
            accession_no="acc-boundary",
            symbol="AAPL",
            form="10-Q",
            fiscal_period_end=date(2026, 6, 30),
            revenue=1.0,
            net_income=1.0,
            fcf=1.0,
            equity=1.0,
            assets=2.0,
            shares=1.0,
            source_url="https://www.sec.gov/example",
            filed_at=datetime(2026, 7, 20, tzinfo=UTC),
            fetched_at=datetime(2026, 7, 30, tzinfo=UTC),
        )
        market_store.upsert_fundamentals([record])

        result = market_store.get_latest_fundamentals("AAPL", as_of=date(2026, 7, 20))

        assert result is not None
        assert result.accession_no == "acc-boundary"

    def test_excludes_filing_filed_after_as_of(self, market_store):
        record = FundamentalsRecord(
            accession_no="acc-future",
            symbol="AAPL",
            form="10-Q",
            fiscal_period_end=date(2026, 6, 30),
            revenue=1.0,
            net_income=1.0,
            fcf=1.0,
            equity=1.0,
            assets=2.0,
            shares=1.0,
            source_url="https://www.sec.gov/example",
            filed_at=datetime(2026, 7, 21, tzinfo=UTC),
            fetched_at=datetime(2026, 7, 30, tzinfo=UTC),
        )
        market_store.upsert_fundamentals([record])

        result = market_store.get_latest_fundamentals("AAPL", as_of=date(2026, 7, 20))

        assert result is None

    def test_only_considers_the_requested_symbol(self, market_store):
        records = [
            FundamentalsRecord(
                accession_no=f"acc-{symbol}",
                symbol=symbol,
                form="10-Q",
                fiscal_period_end=date(2026, 6, 30),
                revenue=1.0,
                net_income=1.0,
                fcf=1.0,
                equity=1.0,
                assets=2.0,
                shares=1.0,
                source_url="https://www.sec.gov/example",
                filed_at=datetime(2026, 7, 10, tzinfo=UTC),
                fetched_at=datetime(2026, 7, 30, tzinfo=UTC),
            )
            for symbol in ("AAPL", "MSFT")
        ]
        market_store.upsert_fundamentals(records)

        result = market_store.get_latest_fundamentals("MSFT", as_of=date(2026, 7, 20))

        assert result is not None
        assert result.symbol == "MSFT"

    def test_returns_none_when_symbol_has_no_fundamentals(self, market_store):
        result = market_store.get_latest_fundamentals("AAPL", as_of=date(2026, 7, 20))

        assert result is None

    def test_deterministic_tiebreak_when_filed_at_ties(self, market_store):
        tied_filed_at = datetime(2026, 7, 10, tzinfo=UTC)
        records = [
            FundamentalsRecord(
                accession_no="acc-earlier-period",
                symbol="AAPL",
                form="10-Q",
                fiscal_period_end=date(2026, 3, 31),
                revenue=1.0,
                net_income=1.0,
                fcf=1.0,
                equity=1.0,
                assets=2.0,
                shares=1.0,
                source_url="https://www.sec.gov/example",
                filed_at=tied_filed_at,
                fetched_at=datetime(2026, 7, 30, tzinfo=UTC),
            ),
            FundamentalsRecord(
                accession_no="acc-later-period",
                symbol="AAPL",
                form="10-Q",
                fiscal_period_end=date(2026, 6, 30),
                revenue=1.0,
                net_income=1.0,
                fcf=1.0,
                equity=1.0,
                assets=2.0,
                shares=1.0,
                source_url="https://www.sec.gov/example",
                filed_at=tied_filed_at,
                fetched_at=datetime(2026, 7, 30, tzinfo=UTC),
            ),
        ]
        market_store.upsert_fundamentals(records)

        result = market_store.get_latest_fundamentals("AAPL", as_of=date(2026, 7, 20))

        assert result is not None
        assert result.accession_no == "acc-later-period"
