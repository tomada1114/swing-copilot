"""Tests for MarketStore: Parquet bars + DuckDB fundamentals (FR-02, FR-03)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pandas as pd
import pytest
from duckdb import ConstraintException

from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    DEFAULT_PARQUET_ROOT,
    FundamentalsRecord,
    MarketStore,
    ParquetRootNotFoundError,
    resolve_parquet_root,
)


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

    def test_replace_failure_preserves_partition_and_cleans_unique_temp(
        self, market_store: MarketStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        initial = _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000)])
        corrected = _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 11.5, 1000)])
        market_store.write_bars(initial)
        partition_dir = market_store.parquet_root / "year=2026"
        partition_file = partition_dir / "data.parquet"
        previous_bytes = partition_file.read_bytes()

        def _boom(_source, _destination):
            msg = "replace failed"
            raise OSError(msg)

        monkeypatch.setattr(Path, "replace", _boom)

        with pytest.raises(OSError, match="replace failed"):
            market_store.write_bars(corrected)

        assert partition_file.read_bytes() == previous_bytes
        assert list(partition_dir.glob(".data.parquet.*.tmp")) == []

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


def _fetched_record(
    symbol: str, accession_no: str, fetched_at: datetime
) -> FundamentalsRecord:
    return FundamentalsRecord(
        accession_no=accession_no,
        symbol=symbol,
        form="10-Q",
        fiscal_period_end=date(2026, 6, 30),
        filed_at=datetime(2026, 7, 10, tzinfo=UTC),
        revenue=1.0,
        net_income=1.0,
        fcf=1.0,
        equity=1.0,
        assets=2.0,
        shares=1.0,
        source_url="https://www.sec.gov/example",
        fetched_at=fetched_at,
    )


class TestHasFundamentalsFetchedOn:
    def test_returns_true_when_fetched_exactly_on_day(self, market_store):
        market_store.upsert_fundamentals(
            [_fetched_record("AAPL", "acc-1", datetime(2026, 7, 20, 9, tzinfo=UTC))]
        )

        assert market_store.has_fundamentals_fetched_on("AAPL", date(2026, 7, 20))

    def test_returns_false_for_a_symbol_with_no_fundamentals(self, market_store):
        assert not market_store.has_fundamentals_fetched_on("AAPL", date(2026, 7, 20))

    def test_returns_false_when_fetched_one_day_before(self, market_store):
        market_store.upsert_fundamentals(
            [
                _fetched_record(
                    "AAPL", "acc-1", datetime(2026, 7, 19, 23, 59, tzinfo=UTC)
                )
            ]
        )

        assert not market_store.has_fundamentals_fetched_on("AAPL", date(2026, 7, 20))

    def test_returns_false_when_fetched_one_day_after(self, market_store):
        market_store.upsert_fundamentals(
            [_fetched_record("AAPL", "acc-1", datetime(2026, 7, 21, 0, 0, tzinfo=UTC))]
        )

        assert not market_store.has_fundamentals_fetched_on("AAPL", date(2026, 7, 20))

    def test_only_matches_the_requested_symbol(self, market_store):
        market_store.upsert_fundamentals(
            [_fetched_record("MSFT", "acc-1", datetime(2026, 7, 20, 9, tzinfo=UTC))]
        )

        assert not market_store.has_fundamentals_fetched_on("AAPL", date(2026, 7, 20))


class TestEarliestBarDates:
    def test_returns_oldest_stored_date_per_symbol(self, market_store):
        market_store.write_bars(
            _bars(
                [
                    ("AAPL", "2019-01-02", 1.0, 1.0, 1.0, 1.0, 1),
                    ("AAPL", "2020-01-02", 1.0, 1.0, 1.0, 1.0, 1),
                    ("MSFT", "2021-06-01", 1.0, 1.0, 1.0, 1.0, 1),
                ]
            )
        )

        assert market_store.earliest_bar_dates(["AAPL", "MSFT"]) == {
            "AAPL": date(2019, 1, 2),
            "MSFT": date(2021, 6, 1),
        }

    def test_omits_symbols_with_no_stored_bars(self, market_store):
        market_store.write_bars(_bars([("AAPL", "2019-01-02", 1.0, 1.0, 1.0, 1.0, 1)]))

        assert market_store.earliest_bar_dates(["AAPL", "NVDA"]) == {
            "AAPL": date(2019, 1, 2)
        }

    def test_returns_empty_mapping_before_any_partition_exists(self, market_store):
        assert market_store.earliest_bar_dates(["AAPL"]) == {}

    def test_returns_empty_mapping_for_empty_symbol_list(self, market_store):
        market_store.write_bars(_bars([("AAPL", "2019-01-02", 1.0, 1.0, 1.0, 1.0, 1)]))

        assert market_store.earliest_bar_dates([]) == {}


def _filing(
    symbol: str,
    form: str,
    period_end: date,
    filed_on: date,
    *,
    accession_no: str | None = None,
) -> FundamentalsRecord:
    return FundamentalsRecord(
        accession_no=accession_no or f"acc-{symbol}-{form}-{filed_on.isoformat()}",
        symbol=symbol,
        form=form,
        fiscal_period_end=period_end,
        filed_at=datetime.combine(filed_on, datetime.min.time(), tzinfo=UTC),
        revenue=1.0,
        net_income=1.0,
        fcf=1.0,
        equity=1.0,
        assets=2.0,
        shares=1.0,
        source_url="https://www.sec.gov/example",
        fetched_at=datetime(2026, 7, 30, tzinfo=UTC),
    )


class TestReadFilingDates:
    """The point-in-time filing history the backtest earnings gate reads (#201)."""

    _FORMS = ("10-K", "10-Q")

    def test_returns_distinct_ascending_dates_per_symbol(self, market_store):
        market_store.upsert_fundamentals(
            [
                _filing("AAPL", "10-Q", date(2026, 3, 31), date(2026, 5, 1)),
                _filing("AAPL", "10-Q", date(2025, 12, 31), date(2026, 2, 2)),
                _filing("MSFT", "10-K", date(2026, 6, 30), date(2026, 8, 3)),
            ]
        )

        assert market_store.read_filing_dates(
            ["AAPL", "MSFT"], self._FORMS, date(2026, 12, 31)
        ) == {
            "AAPL": (date(2026, 2, 2), date(2026, 5, 1)),
            "MSFT": (date(2026, 8, 3),),
        }

    def test_filing_accepted_the_day_before_the_cutoff_is_visible(self, market_store):
        market_store.upsert_fundamentals(
            [_filing("AAPL", "10-Q", date(2026, 3, 31), date(2026, 5, 1))]
        )

        assert market_store.read_filing_dates(
            ["AAPL"], self._FORMS, date(2026, 5, 2)
        ) == {"AAPL": (date(2026, 5, 1),)}

    def test_filing_accepted_exactly_on_the_cutoff_is_visible(self, market_store):
        market_store.upsert_fundamentals(
            [_filing("AAPL", "10-Q", date(2026, 3, 31), date(2026, 5, 1))]
        )

        assert market_store.read_filing_dates(
            ["AAPL"], self._FORMS, date(2026, 5, 1)
        ) == {"AAPL": (date(2026, 5, 1),)}

    def test_filing_accepted_the_day_after_the_cutoff_is_invisible(self, market_store):
        market_store.upsert_fundamentals(
            [_filing("AAPL", "10-Q", date(2026, 3, 31), date(2026, 5, 1))]
        )

        assert (
            market_store.read_filing_dates(["AAPL"], self._FORMS, date(2026, 4, 30))
            == {}
        )

    def test_one_period_filed_twice_counts_as_its_earliest_filing_only(
        self, market_store
    ):
        # A corrected re-filing of the same quarter is one reporting event,
        # not two -- counting it twice would halve the estimated cadence.
        market_store.upsert_fundamentals(
            [
                _filing(
                    "AAPL",
                    "10-Q",
                    date(2026, 3, 31),
                    date(2026, 5, 1),
                    accession_no="acc-original",
                ),
                _filing(
                    "AAPL",
                    "10-Q",
                    date(2026, 3, 31),
                    date(2026, 5, 20),
                    accession_no="acc-corrected",
                ),
            ]
        )

        assert market_store.read_filing_dates(
            ["AAPL"], self._FORMS, date(2026, 12, 31)
        ) == {"AAPL": (date(2026, 5, 1),)}

    def test_two_periods_filed_the_same_day_are_one_date(self, market_store):
        market_store.upsert_fundamentals(
            [
                _filing("AAPL", "10-K", date(2025, 12, 31), date(2026, 2, 2)),
                _filing("AAPL", "10-Q", date(2026, 3, 31), date(2026, 2, 2)),
            ]
        )

        assert market_store.read_filing_dates(
            ["AAPL"], self._FORMS, date(2026, 12, 31)
        ) == {"AAPL": (date(2026, 2, 2),)}

    def test_forms_outside_the_requested_set_are_ignored(self, market_store):
        market_store.upsert_fundamentals(
            [_filing("AAPL", "10-Q/A", date(2026, 3, 31), date(2026, 5, 1))]
        )

        assert (
            market_store.read_filing_dates(["AAPL"], self._FORMS, date(2026, 12, 31))
            == {}
        )

    def test_unrequested_symbols_are_never_returned(self, market_store):
        market_store.upsert_fundamentals(
            [_filing("MSFT", "10-Q", date(2026, 3, 31), date(2026, 5, 1))]
        )

        assert (
            market_store.read_filing_dates(["AAPL"], self._FORMS, date(2026, 12, 31))
            == {}
        )

    def test_empty_inputs_short_circuit_without_a_query(self, market_store):
        market_store.upsert_fundamentals(
            [_filing("AAPL", "10-Q", date(2026, 3, 31), date(2026, 5, 1))]
        )

        assert market_store.read_filing_dates([], self._FORMS, date(2026, 12, 31)) == {}
        assert market_store.read_filing_dates(["AAPL"], (), date(2026, 12, 31)) == {}


class TestResolveParquetRoot:
    """`<db>/../bars` resolution and its fail-fast guard (Issue #217 / #221).

    The shared implementation the `--db`-taking CLIs call: `copilot-backtest`,
    `copilot-track`, `copilot-retro`, `copilot-dd-forward`, and
    `copilot-filter-matrix`.
    """

    _CONSEQUENCE = "このまま実行すると空振りする。"

    def test_the_default_db_path_pairs_with_the_default_parquet_root(self) -> None:
        # `--db` 未指定の既定経路が指す先が、この対応規約そのものである。
        assert DEFAULT_DB_PATH.parent / "bars" == DEFAULT_PARQUET_ROOT

    def test_an_existing_sibling_directory_is_returned(self, tmp_path: Path) -> None:
        (tmp_path / "bars").mkdir()

        assert (
            resolve_parquet_root(
                tmp_path / "copilot.duckdb", consequence=self._CONSEQUENCE
            )
            == tmp_path / "bars"
        )

    def test_an_empty_existing_root_is_accepted(self, tmp_path: Path) -> None:
        """A present root with no partition yet stays fail-soft; only absence is fatal."""
        root = tmp_path / "bars"
        root.mkdir()

        assert (
            resolve_parquet_root(
                tmp_path / "copilot.duckdb", consequence=self._CONSEQUENCE
            )
            == root
        )
        assert not any(root.iterdir())

    def test_a_string_db_path_resolves_the_same_way(self, tmp_path: Path) -> None:
        (tmp_path / "bars").mkdir()

        assert (
            resolve_parquet_root(
                str(tmp_path / "copilot.duckdb"), consequence=self._CONSEQUENCE
            )
            == tmp_path / "bars"
        )

    def test_a_missing_root_raises_naming_the_resolved_path(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ParquetRootNotFoundError) as exc_info:
            resolve_parquet_root(
                tmp_path / "copilot.duckdb", consequence=self._CONSEQUENCE
            )

        message = str(exc_info.value)
        assert "Parquetディレクトリが見つかりません" in message
        assert str(tmp_path / "bars") in message

    def test_the_callers_consequence_is_appended_to_the_shared_explanation(
        self, tmp_path: Path
    ) -> None:
        """One implementation, one layout explanation, per-command damage."""
        with pytest.raises(ParquetRootNotFoundError) as exc_info:
            resolve_parquet_root(
                tmp_path / "copilot.duckdb", consequence="固有の被害を述べる。"
            )

        message = str(exc_info.value)
        assert "同ディレクトリの bars/ として解決する" in message
        assert message.endswith("固有の被害を述べる。")

    def test_a_sibling_bars_file_is_not_accepted_as_a_root(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "bars").write_text("not a directory", encoding="utf-8")

        with pytest.raises(
            ParquetRootNotFoundError, match="Parquetディレクトリが見つかりません"
        ):
            resolve_parquet_root(
                tmp_path / "copilot.duckdb", consequence=self._CONSEQUENCE
            )

    def test_market_store_itself_never_validates_the_root(self, tmp_path: Path) -> None:
        """The daily/backfill path creates the root lazily on first write.

        `MarketStore.__init__` must therefore stay silent about an absent
        root -- the guard belongs to the CLIs that take `--db` (Issue #221).
        """
        absent = tmp_path / "never-created"
        store = MarketStore(Database(tmp_path / "copilot.duckdb"), parquet_root=absent)

        assert store.read_bars(
            ["AAPL"], date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 7, 20)
        ).empty
        assert not absent.exists()
