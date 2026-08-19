"""Tests for MarketStore: Parquet bars + DuckDB fundamentals (FR-02, FR-03)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pandas as pd
import pytest
from duckdb import ConstraintException

from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    DEFAULT_PARQUET_ROOT,
    FundamentalsFetchStamp,
    FundamentalsFetchState,
    FundamentalsRecord,
    MarketStore,
    NonFiniteBarsError,
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


class TestWriteBarsRejectsNonFiniteValues:
    """Issue #227: the store's own NaN/±inf defense layer, under the providers.

    Fail-fast on the whole batch, matching `storage/json_guard.dumps_safe`
    (the package's other write boundary for the same value) rather than the
    reader-side fail-soft treatments. Dropping the offending row instead
    would only move the silence from "a NaN was stored" to "a bar vanished".
    """

    @pytest.mark.parametrize(
        "bad_value",
        [
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="inf"),
            pytest.param(float("-inf"), id="-inf"),
        ],
    )
    @pytest.mark.parametrize("column", ["open", "high", "low", "close", "volume"])
    def test_a_non_finite_ohlcv_value_is_rejected(
        self, market_store: MarketStore, column: str, bad_value: float
    ) -> None:
        bars = _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000)])
        bars[column] = bad_value

        with pytest.raises(NonFiniteBarsError, match="非有限"):
            market_store.write_bars(bars)

        assert not market_store.parquet_root.exists()

    def test_one_bad_row_rejects_the_whole_batch_rather_than_dropping_it(
        self, market_store: MarketStore
    ) -> None:
        """Fail-fast, pinned: the good rows of a bad batch are not written."""
        bars = _bars(
            [
                ("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000),
                ("MSFT", "2026-07-15", 20, 20.5, 19.5, 20.2, 2000),
            ]
        )
        bars.loc[1, "close"] = float("nan")

        with pytest.raises(NonFiniteBarsError):
            market_store.write_bars(bars)

        assert not market_store.parquet_root.exists()

    def test_a_bad_row_in_a_later_year_leaves_the_earlier_year_unwritten(
        self, market_store: MarketStore
    ) -> None:
        """Validation precedes the first partition write, so it stays atomic."""
        bars = _bars(
            [
                ("AAPL", "2025-12-30", 9, 9.5, 8.5, 9.2, 900),
                ("AAPL", "2026-01-02", 10, 10.5, 9.5, 10.2, 1000),
            ]
        )
        bars.loc[1, "low"] = float("-inf")

        with pytest.raises(NonFiniteBarsError):
            market_store.write_bars(bars)

        assert not (market_store.parquet_root / "year=2025").exists()
        assert not (market_store.parquet_root / "year=2026").exists()

    def test_a_rejected_write_preserves_the_previous_partition_and_leaves_no_temp(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000)])
        )
        partition_dir = market_store.parquet_root / "year=2026"
        partition_file = partition_dir / "data.parquet"
        previous_bytes = partition_file.read_bytes()
        corrupt = _bars([("AAPL", "2026-07-16", 10, 10.5, 9.5, 10.4, 1100)])
        corrupt["close"] = float("nan")

        with pytest.raises(NonFiniteBarsError):
            market_store.write_bars(corrupt)

        assert partition_file.read_bytes() == previous_bytes
        assert list(partition_dir.iterdir()) == [partition_file]

    def test_the_rejection_names_the_offending_bars_and_their_total(
        self, market_store: MarketStore
    ) -> None:
        bars = _bars(
            [
                ("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000),
                ("MSFT", "2026-07-15", 20, 20.5, 19.5, 20.2, 2000),
            ]
        )
        bars.loc[1, "high"] = float("inf")
        bars.loc[1, "close"] = float("nan")

        with pytest.raises(NonFiniteBarsError) as excinfo:
            market_store.write_bars(bars)

        message = str(excinfo.value)
        assert "2件" in message
        assert "MSFT 2026-07-15" in message
        assert "high=" in message
        assert "close=" in message
        assert "AAPL" not in message

    def test_a_repeated_index_still_yields_the_rejection_not_a_lookup_error(
        self, market_store: MarketStore
    ) -> None:
        """Callers concatenate provider chunks; the index need not be unique."""
        first = _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000)])
        second = _bars([("MSFT", "2026-07-16", 20, 20.5, 19.5, 20.2, 2000)])
        second["close"] = float("nan")
        duplicated = pd.concat([first, second])
        assert not duplicated.index.is_unique

        with pytest.raises(NonFiniteBarsError, match="MSFT 2026-07-16"):
            market_store.write_bars(duplicated)

    def test_a_non_numeric_price_is_rejected_by_the_same_guard(
        self, market_store: MarketStore
    ) -> None:
        """Coercion makes junk indistinguishable from NaN, so it is refused too."""
        bars = _bars([("AAPL", "2026-07-15", 10, 10.5, 9.5, 10.2, 1000)])
        bars["close"] = bars["close"].astype(object)
        bars.loc[0, "close"] = "n/a"

        with pytest.raises(NonFiniteBarsError):
            market_store.write_bars(bars)

    def test_finite_edge_values_still_write_unchanged(
        self, market_store: MarketStore
    ) -> None:
        """The guard targets non-finite values only, not zero or negatives."""
        market_store.write_bars(_bars([("FLAT", "2026-07-15", 0.0, 0.0, -1.5, 0.0, 0)]))

        result = market_store.read_bars(
            ["FLAT"], date(2026, 7, 1), date(2026, 7, 31), as_of=date(2026, 7, 20)
        )

        assert len(result) == 1
        assert result.iloc[0]["low"] == pytest.approx(-1.5)
        assert result.iloc[0]["volume"] == 0


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


class TestFundamentalsFetchLog:
    """Issue #258: per-symbol EDGAR poll bookkeeping, separate from records.

    The log answers "when did we last *ask* EDGAR about this symbol", which
    `fundamentals.fetched_at` structurally cannot: a symbol with no XBRL
    facts in the lookback window persists no row at all. It records two
    distinct facts -- when we polled, and how current that poll left us --
    because a `--as-of` replay makes them diverge.
    """

    def test_records_and_reads_back_both_dates(self, market_store):
        market_store.record_fundamentals_fetches(
            [
                FundamentalsFetchStamp(
                    "AAPL",
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    0,
                )
            ]
        )

        assert market_store.read_fundamentals_fetch_state(["AAPL"]) == {
            "AAPL": FundamentalsFetchState(
                last_fetched_on=date(2026, 7, 20),
                fetched_through_on=date(2026, 7, 20),
            )
        }

    def test_the_two_dates_are_recorded_independently(self, market_store):
        """A replay polls today but only reaches a past horizon."""
        market_store.record_fundamentals_fetches(
            [
                FundamentalsFetchStamp(
                    "AAPL",
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    datetime(2026, 7, 1, 23, 59, tzinfo=UTC),
                    0,
                )
            ]
        )

        assert market_store.read_fundamentals_fetch_state(["AAPL"]) == {
            "AAPL": FundamentalsFetchState(
                last_fetched_on=date(2026, 7, 20),
                fetched_through_on=date(2026, 7, 1),
            )
        }

    def test_omits_a_symbol_that_was_never_fetched(self, market_store):
        market_store.record_fundamentals_fetches(
            [
                FundamentalsFetchStamp(
                    "MSFT",
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    0,
                )
            ]
        )

        assert market_store.read_fundamentals_fetch_state(["AAPL"]) == {}

    def test_a_later_fetch_replaces_both_recorded_times(self, market_store):
        market_store.record_fundamentals_fetches(
            [
                FundamentalsFetchStamp(
                    "AAPL",
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    0,
                )
            ]
        )
        market_store.record_fundamentals_fetches(
            [
                FundamentalsFetchStamp(
                    "AAPL",
                    datetime(2026, 7, 27, 9, tzinfo=UTC),
                    datetime(2026, 7, 27, 9, tzinfo=UTC),
                    0,
                )
            ]
        )

        assert market_store.read_fundamentals_fetch_state(["AAPL"]) == {
            "AAPL": FundamentalsFetchState(
                last_fetched_on=date(2026, 7, 27),
                fetched_through_on=date(2026, 7, 27),
            )
        }

    def test_records_a_symbol_that_yielded_no_fundamentals_row(self, market_store):
        """The gap `fundamentals.fetched_at` cannot cover.

        A successful fetch that found no facts writes no `fundamentals` row,
        so without this table the symbol would look never-fetched and be
        re-polled on every single run forever.
        """
        market_store.record_fundamentals_fetches(
            [
                FundamentalsFetchStamp(
                    "AAPL",
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    0,
                )
            ]
        )

        assert "AAPL" in market_store.read_fundamentals_fetch_state(["AAPL"])
        with market_store.get_connection() as conn:
            assert conn.execute("SELECT count(*) FROM fundamentals").fetchone() == (0,)

    def test_upserting_fundamentals_alone_records_no_fetch(self, market_store):
        """The two are deliberately independent bookkeeping.

        `upsert_fundamentals` is also reached by `copilot-backfill`, which is
        a historical bulk load, not a freshness poll; only the daily step's
        own successful fetch may claim the symbol was polled.
        """
        market_store.upsert_fundamentals(
            [_fetched_record("AAPL", "acc-1", datetime(2026, 7, 20, 9, tzinfo=UTC))]
        )

        assert market_store.read_fundamentals_fetch_state(["AAPL"]) == {}

    def test_a_row_predating_the_horizon_column_reads_as_unknown(self, market_store):
        """The additive-migration path: NULL must never read as "fresh".

        A database created by the first revision of this branch has neither
        `fetched_through` nor `consecutive_empty`. `get_connection()` adds
        them, and the pre-existing row's NULLs have to mean "horizon
        unknown" (which the refresh rule treats as due) and "no empty answers
        recorded" respectively.
        """
        with market_store.get_connection() as conn:
            conn.execute(
                "INSERT INTO fundamentals_fetch_log (symbol, last_fetched_at) "
                "VALUES (?, ?)",
                ["AAPL", datetime(2026, 7, 20, 9, tzinfo=UTC)],
            )

        assert market_store.read_fundamentals_fetch_state(["AAPL"]) == {
            "AAPL": FundamentalsFetchState(
                last_fetched_on=date(2026, 7, 20),
                fetched_through_on=None,
                consecutive_empty=0,
            )
        }

    def test_reading_no_symbols_never_touches_the_database(self, market_store):
        assert market_store.read_fundamentals_fetch_state([]) == {}

    def test_recording_no_stamps_is_a_no_op(self, market_store):
        market_store.record_fundamentals_fetches([])

        assert market_store.read_fundamentals_fetch_state(["AAPL"]) == {}

    def test_an_empty_answer_is_recorded_with_its_counter(self, market_store):
        """The counter is what makes the empty retry converge (#258 review)."""
        stamp = datetime(2026, 7, 20, 9, tzinfo=UTC)
        market_store.record_fundamentals_fetches(
            [FundamentalsFetchStamp("AAPL", stamp, stamp, 3)]
        )

        assert market_store.read_fundamentals_fetch_state(["AAPL"]) == {
            "AAPL": FundamentalsFetchState(
                last_fetched_on=date(2026, 7, 20),
                fetched_through_on=date(2026, 7, 20),
                consecutive_empty=3,
            )
        }

    def test_a_later_non_empty_fetch_resets_the_counter(self, market_store):
        stamp = datetime(2026, 7, 20, 9, tzinfo=UTC)
        later = datetime(2026, 7, 21, 9, tzinfo=UTC)
        market_store.record_fundamentals_fetches(
            [FundamentalsFetchStamp("AAPL", stamp, stamp, 3)]
        )
        market_store.record_fundamentals_fetches(
            [FundamentalsFetchStamp("AAPL", later, later, 0)]
        )

        assert (
            market_store.read_fundamentals_fetch_state(["AAPL"])[
                "AAPL"
            ].consecutive_empty
            == 0
        )

    def test_a_failing_row_rolls_the_whole_batch_back(self, market_store):
        """One logical multi-row write, all-or-nothing (AGENTS.md storage rule).

        `symbol` is the primary key, so a `None` symbol fails on the *second*
        row -- after the first one has already been inserted successfully.
        """
        stamp = datetime(2026, 7, 20, 9, tzinfo=UTC)
        later = datetime(2026, 7, 27, 9, tzinfo=UTC)
        market_store.record_fundamentals_fetches(
            [FundamentalsFetchStamp("AAPL", stamp, stamp, 0)]
        )

        with pytest.raises(ConstraintException):
            market_store.record_fundamentals_fetches(
                [
                    FundamentalsFetchStamp("MSFT", later, later, 0),
                    FundamentalsFetchStamp(cast("str", None), later, later, 0),
                ]
            )

        assert list(market_store.read_fundamentals_fetch_state(["AAPL", "MSFT"])) == [
            "AAPL"
        ]


class TestReadLatestFilingDates:
    """Issue #258 review finding 4: `MAX` in SQL, not in Python.

    Distinct from `read_filing_dates()` in two ways that matter here: it
    returns one row per symbol instead of every quarter, and it does not
    collapse a corrected re-filing to the earliest date of its period -- an
    amendment's later filing date is exactly what proves the period landed.
    """

    def _record(self, symbol, accession_no, filed_at, form="10-Q"):
        return replace(
            _fetched_record(symbol, accession_no, datetime(2026, 7, 20, tzinfo=UTC)),
            filed_at=filed_at,
            form=form,
        )

    def test_returns_the_newest_filing_per_symbol(self, market_store):
        market_store.upsert_fundamentals(
            [
                self._record("AAPL", "acc-1", datetime(2026, 4, 10, tzinfo=UTC)),
                self._record("AAPL", "acc-2", datetime(2026, 7, 10, tzinfo=UTC)),
                self._record("MSFT", "acc-3", datetime(2026, 5, 15, tzinfo=UTC)),
            ]
        )

        assert market_store.read_latest_filing_dates(
            ["AAPL", "MSFT"], ("10-K", "10-Q"), date(2026, 7, 20)
        ) == {"AAPL": date(2026, 7, 10), "MSFT": date(2026, 5, 15)}

    def test_a_filing_accepted_exactly_on_as_of_is_visible(self, market_store):
        market_store.upsert_fundamentals(
            [self._record("AAPL", "acc-1", datetime(2026, 7, 20, tzinfo=UTC))]
        )

        assert market_store.read_latest_filing_dates(
            ["AAPL"], ("10-K", "10-Q"), date(2026, 7, 20)
        ) == {"AAPL": date(2026, 7, 20)}

    def test_a_filing_accepted_after_as_of_is_invisible(self, market_store):
        market_store.upsert_fundamentals(
            [self._record("AAPL", "acc-1", datetime(2026, 7, 21, tzinfo=UTC))]
        )

        assert (
            market_store.read_latest_filing_dates(
                ["AAPL"], ("10-K", "10-Q"), date(2026, 7, 20)
            )
            == {}
        )

    def test_only_the_requested_forms_count(self, market_store):
        market_store.upsert_fundamentals(
            [
                self._record(
                    "AAPL", "acc-1", datetime(2026, 7, 10, tzinfo=UTC), form="10-K"
                )
            ]
        )

        assert market_store.read_latest_filing_dates(
            ["AAPL"], ("10-K",), date(2026, 7, 20)
        ) == {"AAPL": date(2026, 7, 10)}
        assert (
            market_store.read_latest_filing_dates(
                ["AAPL"], ("10-Q",), date(2026, 7, 20)
            )
            == {}
        )

    def test_empty_symbols_or_forms_never_touch_the_database(self, market_store):
        assert (
            market_store.read_latest_filing_dates([], ("10-Q",), date(2026, 7, 20))
            == {}
        )
        assert (
            market_store.read_latest_filing_dates(["AAPL"], (), date(2026, 7, 20)) == {}
        )


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
