"""Market data repository: Parquet bars + DuckDB fundamentals (FR-02, FR-03).

Bars are the large, append-heavy time series, so they live in Hive-partitioned
Parquet (`year=YYYY/data.parquet`) and DuckDB only provides a `read_parquet`
view over them — never a second copy of the raw rows (`docs/03_basic_design.md`
5). `write_bars` upserts `(symbol, date)` within each affected year partition
via write-to-temp-then-rename, so a crash mid-write never corrupts the
previous partition. Fundamentals are comparatively small structured records,
so they live directly in a DuckDB table, natural-keyed by `accession_no` (the
one truly unique identifier for an SEC filing; `storage/database.py`'s DDL is
authoritative over the docstring prose in `docs/04_detailed_design.md` 3.7,
which loosely paraphrases the key as `(symbol, fiscal_period)`).
"""

from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime

    import duckdb

    from swing_copilot.storage.database import Database

DEFAULT_PARQUET_ROOT = Path("data/bars")
BARS_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "provider",
    "fetched_at",
)

_CREATE_FUNDAMENTALS_TABLE = """
CREATE TABLE IF NOT EXISTS fundamentals (
    accession_no       VARCHAR PRIMARY KEY,
    symbol             VARCHAR NOT NULL,
    form               VARCHAR NOT NULL,
    fiscal_period_end  DATE NOT NULL,
    filed_at           TIMESTAMPTZ NOT NULL,
    revenue            DOUBLE,
    net_income         DOUBLE,
    fcf                DOUBLE,
    equity             DOUBLE,
    assets             DOUBLE,
    shares             DOUBLE,
    source_url         VARCHAR NOT NULL,
    fetched_at         TIMESTAMPTZ NOT NULL
)
"""

_UPSERT_FUNDAMENTALS = """
INSERT INTO fundamentals (
    accession_no, symbol, form, fiscal_period_end, filed_at,
    revenue, net_income, fcf, equity, assets, shares, source_url, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (accession_no) DO UPDATE SET
    symbol = EXCLUDED.symbol,
    form = EXCLUDED.form,
    fiscal_period_end = EXCLUDED.fiscal_period_end,
    filed_at = EXCLUDED.filed_at,
    revenue = EXCLUDED.revenue,
    net_income = EXCLUDED.net_income,
    fcf = EXCLUDED.fcf,
    equity = EXCLUDED.equity,
    assets = EXCLUDED.assets,
    shares = EXCLUDED.shares,
    source_url = EXCLUDED.source_url,
    fetched_at = EXCLUDED.fetched_at
"""


@dataclass(frozen=True, slots=True)
class FundamentalsRecord:
    """One filing's normalized fundamentals (`fundamentals` table schema)."""

    accession_no: str
    symbol: str
    form: str
    fiscal_period_end: date
    filed_at: datetime
    revenue: float | None
    net_income: float | None
    fcf: float | None
    equity: float | None
    assets: float | None
    shares: float | None
    source_url: str
    fetched_at: datetime


def _empty_bars_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(BARS_COLUMNS))


def _as_date(value: object) -> date:
    """Normalize a DuckDB date scalar (date or timestamp) to `datetime.date`."""
    return pd.Timestamp(value).date()  # type: ignore[arg-type] # pandas accepts any date-like scalar


class MarketStore:
    """Parquet bars + DuckDB fundamentals, backed by one shared `Database`."""

    def __init__(
        self, database: Database, parquet_root: Path | str = DEFAULT_PARQUET_ROOT
    ) -> None:
        """Create the store.

        Args:
            database: Shared DuckDB connection owner.
            parquet_root: Root directory for `year=YYYY` bar partitions.
        """
        self._database = database
        self.parquet_root = Path(parquet_root)

    def _has_partition_files(self) -> bool:
        return any(self.parquet_root.glob("year=*/*.parquet"))

    def get_connection(self) -> duckdb.DuckDBPyConnection:
        """Return a DuckDB connection ready to query fundamentals and bars.

        `fundamentals` is always ensured; the `bars` view is (re)created only
        when at least one bar partition file exists.

        Returns:
            A connection usable as a context manager.
        """
        conn = self._database.connect()
        conn.execute(_CREATE_FUNDAMENTALS_TABLE)
        if self._has_partition_files():
            glob = str(self.parquet_root / "year=*" / "*.parquet")
            conn.execute(
                f"CREATE OR REPLACE VIEW bars AS "  # noqa: S608 - glob is a local path, not user input
                f"SELECT * FROM read_parquet('{glob}', hive_partitioning=true)"
            )
        return conn

    def write_bars(self, df: pd.DataFrame) -> None:
        """Upsert daily OHLCV rows, partitioned by year, `(symbol, date)`-keyed.

        Args:
            df: Rows matching `BARS_COLUMNS` (the Parquet schema, including
                `provider` and `fetched_at` — already stamped by the caller).
        """
        if df.empty:
            return

        working = df.copy()
        working["date"] = pd.to_datetime(working["date"]).dt.date
        years = working["date"].map(lambda d: d.year)
        for year in sorted(years.unique()):
            self._write_partition(int(year), working[years == year])

    def _write_partition(self, year: int, new_rows: pd.DataFrame) -> None:
        partition_dir = self.parquet_root / f"year={year}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        partition_file = partition_dir / "data.parquet"

        if partition_file.is_file():
            existing = pd.read_parquet(partition_file)
            combined = pd.concat([existing, new_rows], ignore_index=True)
        else:
            combined = new_rows

        combined = combined.drop_duplicates(subset=["symbol", "date"], keep="last")
        combined = combined.sort_values(["symbol", "date"]).reset_index(drop=True)

        with tempfile.NamedTemporaryFile(
            dir=partition_dir,
            prefix=".data.parquet.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_file = Path(handle.name)
        try:
            combined.to_parquet(tmp_file, index=False)
            tmp_file.replace(partition_file)
        finally:
            tmp_file.unlink(missing_ok=True)

    def read_bars(
        self, symbols: list[str], start: date, end: date, as_of: date
    ) -> pd.DataFrame:
        """Read bars for `symbols` over `[start, end]`, never past `as_of`.

        Args:
            symbols: Ticker symbols to read.
            start: Inclusive range start.
            end: Inclusive range end.
            as_of: Point-in-time guard — no returned bar is dated after this,
                regardless of `end`.

        Returns:
            Tidy bars (`BARS_COLUMNS`), ordered by symbol then date.
        """
        if not symbols or not self._has_partition_files():
            return _empty_bars_frame()

        effective_end = min(end, as_of)
        placeholders = ",".join("?" for _ in symbols)
        query = f"""
            SELECT symbol, date, open, high, low, close, volume, provider, fetched_at
            FROM bars
            WHERE symbol IN ({placeholders})
              AND date >= ?
              AND date <= ?
            ORDER BY symbol, date
        """  # noqa: S608 - placeholders are bound parameters, not interpolated values
        with self.get_connection() as conn:
            result = conn.execute(query, [*symbols, start, effective_end]).df()
        result["date"] = pd.to_datetime(result["date"]).dt.date
        return result

    def earliest_bar_dates(self, symbols: list[str]) -> dict[str, date]:
        """Return each symbol's oldest stored bar date, for backfill resume.

        Deliberately not `as_of`-filtered: this answers "how far back does the
        local history already reach", a storage-coverage question, not a
        point-in-time visibility one. No caller feeds it into screening.

        Args:
            symbols: Ticker symbols to inspect.

        Returns:
            `{symbol: oldest bar date}`, omitting symbols with no stored bars.
        """
        if not symbols or not self._has_partition_files():
            return {}

        placeholders = ",".join("?" for _ in symbols)
        query = f"""
            SELECT symbol, MIN(date) AS earliest
            FROM bars
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
        """  # noqa: S608 - placeholders are bound parameters, not interpolated values
        with self.get_connection() as conn:
            rows = conn.execute(query, list(symbols)).fetchall()
        return {str(symbol): _as_date(earliest) for symbol, earliest in rows}

    def upsert_fundamentals(self, records: Sequence[FundamentalsRecord]) -> None:
        """Upsert fundamentals records, keyed by `accession_no`.

        Args:
            records: Records to upsert.
        """
        if not records:
            return
        with self.get_connection() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                for record in records:
                    values = asdict(record)
                    conn.execute(
                        _UPSERT_FUNDAMENTALS,
                        [
                            values["accession_no"],
                            values["symbol"],
                            values["form"],
                            values["fiscal_period_end"],
                            values["filed_at"],
                            values["revenue"],
                            values["net_income"],
                            values["fcf"],
                            values["equity"],
                            values["assets"],
                            values["shares"],
                            values["source_url"],
                            values["fetched_at"],
                        ],
                    )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def get_latest_fundamentals(
        self, symbol: str, as_of: date
    ) -> FundamentalsRecord | None:
        """Return `symbol`'s most recently filed fundamentals row, if any.

        Report-oriented single-record lookup (`docs/goal-prompts/
        swing-copilot-p2-report-paper-wrapup/design.md` 2.1), distinct from
        `read_fundamentals()`'s multi-row screening query.

        Args:
            symbol: Ticker to look up.
            as_of: Point-in-time filing cutoff, inclusive for the whole date.

        Returns:
            The most recent matching row, or `None` if no filing for
            `symbol` was filed at or before `as_of`.
        """
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT accession_no, symbol, form, fiscal_period_end, filed_at,
                       revenue, net_income, fcf, equity, assets, shares,
                       source_url, fetched_at
                FROM fundamentals
                WHERE symbol = ? AND CAST(filed_at AS DATE) <= ?
                ORDER BY filed_at DESC, fiscal_period_end DESC
                LIMIT 1
                """,
                [symbol, as_of],
            ).fetchone()
        if row is None:
            return None
        return FundamentalsRecord(*row)

    def has_fundamentals_fetched_on(self, symbol: str, day: date) -> bool:
        """Return whether `symbol` already has a fundamentals row fetched on `day`.

        Used to skip a same-day rerun's redundant EDGAR network fetch
        (`pipeline/daily.py`'s fundamentals step). The correction upsert
        keyed by `accession_no` is unaffected: a later day's run always
        re-fetches and upserts, regardless of what this returns.

        `fetched_at` is a real fetch timestamp, not a point-in-time value --
        callers must pass the injected `Clock`'s wall-clock date here, never
        `as_of` (P6-25: comparing against a possibly-past `as_of` would never
        match `fetched_at` and defeat the same-day skip entirely).

        Args:
            symbol: Ticker to check.
            day: Calendar day to compare against `fetched_at`'s date.

        Returns:
            `True` if `fundamentals` has at least one row for `symbol` whose
            `fetched_at` falls on `day`.
        """
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM fundamentals
                WHERE symbol = ? AND CAST(fetched_at AS DATE) = ?
                LIMIT 1
                """,
                [symbol, day],
            ).fetchone()
        return row is not None

    def read_fundamentals(self, as_of: date) -> pd.DataFrame:
        """Read every fundamentals row filed on or before `as_of`.

        Args:
            as_of: Point-in-time filing cutoff, inclusive for the whole date.

        Returns:
            Fundamentals ordered deterministically by symbol, period, and filing time.
        """
        with self.get_connection() as conn:
            return conn.execute(
                """
                SELECT * FROM fundamentals
                WHERE CAST(filed_at AS DATE) <= ?
                ORDER BY symbol, fiscal_period_end, filed_at
                """,
                [as_of],
            ).df()
