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

import math
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from swing_copilot.exceptions import SwingCopilotError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime

    import duckdb

    from swing_copilot.storage.database import Database

DEFAULT_PARQUET_ROOT = Path("data/bars")
#: The bars root's directory name, relative to the DuckDB file's directory.
_BARS_DIRNAME = "bars"
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
#: The bar columns whose values must be finite numbers to be storable.
_NUMERIC_BAR_COLUMNS = ("open", "high", "low", "close", "volume")
#: How many offending bars a rejection message names before summarizing.
_MAX_REPORTED_NON_FINITE_BARS = 5


class ParquetRootNotFoundError(SwingCopilotError):
    """Raised when the bars root resolved from a `--db` value does not exist.

    A *fail-fast* signal for the CLIs that take `--db`, never raised by
    `MarketStore` itself: the daily/backfill write path legitimately creates
    the root lazily (`_write_partition`'s `mkdir(parents=True,
    exist_ok=True)`), so validating in `__init__` would break the first run
    on a fresh checkout.
    """


def resolve_parquet_root(db_path: Path | str, *, consequence: str) -> Path:
    """Resolve `--db`'s sibling bars root, failing fast when it is absent.

    Parquet bars live alongside the DuckDB file, mirroring the
    `DEFAULT_DB_PATH`/`DEFAULT_PARQUET_ROOT` pairing ("data/copilot.duckdb" +
    "data/bars") -- `--db` overrides both together, never just the DB. A
    command pointed at a DuckDB copy whose `bars/` was left behind therefore
    reads zero bars for *every* symbol, and every one of the affected
    commands turns that into a plausible-looking zero result at exit 0
    (Issue #217, generalized in Issue #221).

    Only the root being absent altogether is fatal. "A few symbols have no
    bars" (new listings, say) and "the root exists but holds no partition
    yet" stay fail-soft -- what this closes is that a whole-root mistake was
    indistinguishable from those.

    Args:
        db_path: The `--db` value.
        consequence: One sentence naming what this particular command would
            silently produce instead of failing, appended to the shared
            explanation. The layout mistake is common; the damage is not.

    Returns:
        The `bars/` directory next to `db_path`.

    Raises:
        ParquetRootNotFoundError: The resolved `bars/` is not an existing
            directory. Each CLI converts this to its own exit convention.
    """
    parquet_root = Path(db_path).parent / _BARS_DIRNAME
    if not parquet_root.is_dir():
        msg = (
            f"価格バーのParquetディレクトリが見つかりません: {parquet_root}\n"
            f"--db {db_path} は価格バーの根を同ディレクトリの bars/ として解決する"
            "（data/copilot.duckdb + data/bars と同じ対応規約）。"
            "DuckDBファイルだけをコピーして bars/ を並置し忘れていないか確認すること。"
            f"{consequence}"
        )
        raise ParquetRootNotFoundError(msg)
    return parquet_root


class NonFiniteBarsError(SwingCopilotError):
    """Raised when `write_bars` is handed a NaN/±inf OHLCV value (Issue #227).

    *Fail-fast on the whole batch*, deliberately, and not a per-row drop:
    silently persisting a non-finite price is the failure mode this closes,
    and silently discarding a row would only move the silence one layer over.
    It matches how this package's other write boundary treats the same value
    -- `storage/json_guard.dumps_safe` raises before serializing rather than
    coercing -- whereas the fail-soft, "record it and carry on" treatment
    (`risk/checks.check_correlation`'s `data_quality` warning,
    `risk/earnings`' demotion to `unknown`, `pipeline/forward_returns.
    compute_forward_return`'s `None`) belongs to *readers* deciding what to do
    about data that is already stored.

    Rejecting the batch as a whole is also what keeps a multi-year write
    atomic: validation runs before the first partition is touched, so a bad
    row in 2025 cannot leave a half-written 2024.
    """


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

#: Per-symbol EDGAR fetch bookkeeping for the weekly/incremental refresh rule
#: (`docs/03_basic_design.md` 8.3, Issue #258). Deliberately its own table
#: rather than a column on `fundamentals`, for two reasons:
#:
#: - It records *asking EDGAR about a symbol*, which happens even when the
#:   answer is "no XBRL facts in the lookback window". Hanging the timestamp
#:   off a `fundamentals` row would leave exactly those symbols with nothing
#:   to remember, so they would be re-fetched on every single run forever.
#: - `fundamentals.fetched_at` is a property of one filing record and is
#:   rewritten by the `accession_no` correction upsert; conflating it with
#:   "when did we last poll this symbol" would make the two impossible to
#:   reason about separately.
#:
#: The row carries two *different* facts, and conflating them is what the
#: second column exists to prevent:
#:
#: - `last_fetched_at`: the real wall-clock instant the network fetch
#:   happened. Answers "did we already ask EDGAR about this symbol today?",
#:   which is the same-day rerun skip P6-25 added.
#: - `fetched_through`: how *current* that fetch left us, i.e. the newest
#:   `filed_at` it was allowed to see (`min(now, as_of)`). Answers "is this
#:   symbol's data stale?", which is the weekly backstop.
#:
#: They diverge exactly when `--as-of` replays a past date: such a run really
#: did poll EDGAR today (so it should not re-poll on a same-day rerun) but
#: only obtained filings up to a past date (so it must not make the symbol
#: look fresh to tomorrow's real run). One column could only ever serve one
#: of those, and whichever it served, the other became a bug.
#:
#: A third column, `consecutive_empty`, counts fetches in a row that returned
#: no record at all. It is what lets the refresh rule retry an empty answer
#: quickly (a universe-wide empty response must not freeze fundamentals for a
#: week) while still *converging*: a symbol that will never have XBRL facts --
#: a delisted shell, a foreign private issuer filing 20-F, a trust -- backs
#: off to the ordinary weekly cadence instead of costing one request a day
#: forever.
#:
#: All three are *metadata*, never point-in-time values: nothing derived from
#: any of them may stand in for `as_of` when reading `fundamentals`.
_CREATE_FUNDAMENTALS_FETCH_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS fundamentals_fetch_log (
    symbol            VARCHAR PRIMARY KEY,
    last_fetched_at   TIMESTAMPTZ NOT NULL,
    fetched_through   TIMESTAMPTZ,
    consecutive_empty INTEGER
)
"""

#: `fetched_through` and `consecutive_empty` were added after the table's
#: first revision, so a database created by an earlier one needs them added in
#: place -- the same additive discipline `storage/schema.py` documents for
#: `StateStore` tables. DuckDB cannot add a `NOT NULL` column, so both are
#: nullable; readers treat a NULL horizon as "unknown" (the symbol is due,
#: never "fresh") and a NULL counter as zero.
_ALTER_FUNDAMENTALS_FETCH_LOG_STATEMENTS = (
    "ALTER TABLE fundamentals_fetch_log "
    "ADD COLUMN IF NOT EXISTS fetched_through TIMESTAMPTZ",
    "ALTER TABLE fundamentals_fetch_log "
    "ADD COLUMN IF NOT EXISTS consecutive_empty INTEGER",
)

_UPSERT_FUNDAMENTALS_FETCH_LOG = """
INSERT INTO fundamentals_fetch_log (
    symbol, last_fetched_at, fetched_through, consecutive_empty
) VALUES (?, ?, ?, ?)
ON CONFLICT (symbol) DO UPDATE SET
    last_fetched_at = EXCLUDED.last_fetched_at,
    -- A NULL horizon means "this fetch produced nothing, so it moved no
    -- data": keep whatever the last productive fetch reached, rather than
    -- letting a fruitless retry restart the staleness clock.
    fetched_through = COALESCE(
        EXCLUDED.fetched_through, fundamentals_fetch_log.fetched_through
    ),
    consecutive_empty = EXCLUDED.consecutive_empty
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


@dataclass(frozen=True, slots=True)
class FundamentalsFetchState:
    """One symbol's row in `fundamentals_fetch_log`, as calendar days.

    See `_CREATE_FUNDAMENTALS_FETCH_LOG_TABLE` for why the two dates are
    separate facts rather than one, and what the counter buys.
    """

    #: Day the last network fetch actually happened (wall clock).
    last_fetched_on: date
    #: Day up to which that fetch could see filings (`min(now, as_of)`).
    #: `None` on a row written before the column existed -- read as "unknown",
    #: i.e. treat the symbol as due, never as fresh.
    fetched_through_on: date | None
    #: How many fetches in a row have come back with no record. `0` once a
    #: fetch returns anything.
    consecutive_empty: int = 0


@dataclass(frozen=True, slots=True)
class FundamentalsFetchStamp:
    """One symbol's bookkeeping to write after a fetch.

    A batch mixes symbols whose fetch produced records with symbols whose
    fetch came back empty, and the two carry different counters, so the write
    takes whole rows rather than one value applied to a symbol list.
    """

    symbol: str
    #: Wall-clock instant of the fetch. Drives the same-day rerun skip, so it
    #: is the real instant even on a replay -- the replay did poll EDGAR.
    last_fetched_at: datetime
    #: Newest filing instant the fetch was allowed to see
    #: (`min(last_fetched_at, as_of)`). Drives the staleness rule, so a
    #: past-`as_of` replay records the horizon it reached, not the wall clock.
    #: `None` when the fetch produced no record: it moved no data, so it must
    #: leave the stored horizon exactly where it was rather than restarting
    #: the staleness clock. The upsert keeps the previous value in that case.
    fetched_through: datetime | None
    #: `0` when the fetch returned records; otherwise the previous value plus
    #: one, which is what backs the retry off toward the weekly cadence.
    consecutive_empty: int


def _empty_bars_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(BARS_COLUMNS))


def _reject_non_finite_bars(df: pd.DataFrame) -> None:
    """Reject the whole frame if any OHLCV cell is NaN/±inf or non-numeric.

    Runs before the first partition is touched, so a rejected write leaves
    every previous partition byte-identical and creates no temporary file.

    Args:
        df: Bars whose `date` column has already been normalized.

    Raises:
        NonFiniteBarsError: At least one OHLCV value is non-finite. The
            message names the first `_MAX_REPORTED_NON_FINITE_BARS` offending
            `(symbol, date)` pairs with the columns at fault, plus the total.
    """
    # Intersected rather than assumed, so a frame missing a column is left to
    # the Parquet writer's own failure instead of a `KeyError` from here.
    columns = [name for name in _NUMERIC_BAR_COLUMNS if name in df.columns]
    # `to_numeric(errors="coerce")` maps a non-numeric cell to NaN, so a
    # string price is rejected by the same check rather than reaching Parquet
    # as an object column. `abs() < inf` is False for NaN and for either
    # infinity, and stays vectorized over a backfill-sized frame.
    numeric = df[columns].apply(pd.to_numeric, errors="coerce")
    offending = ~numeric.abs().lt(math.inf)
    total = int(offending.to_numpy().sum())
    if total == 0:
        return

    # Positional, not label-based: a caller may hand over a concatenated frame
    # whose index repeats, and the rejection message must not depend on that.
    positions = [
        position for position, is_bad in enumerate(offending.any(axis=1)) if is_bad
    ][:_MAX_REPORTED_NON_FINITE_BARS]
    samples: list[str] = []
    for position in positions:
        row = df.iloc[position]
        flags = offending.iloc[position]
        detail = ", ".join(f"{name}={row[name]}" for name in columns if flags[name])
        samples.append(f"{row['symbol']} {row['date']}: {detail}")
    msg = (
        f"非有限のOHLCV値が{total}件含まれるためバー書き込みを拒否した"
        f"（該当行の例: {' / '.join(samples)}）。"
        "NaN/±infの価格は保存後の集計を黙って歪めるので、バッチ全体を拒否して"
        "呼び出し側（provider の正規化）で落とすこと。"
    )
    raise NonFiniteBarsError(msg)


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

        `fundamentals` and `fundamentals_fetch_log` are always ensured; the
        `bars` view is (re)created only when at least one bar partition file
        exists.

        Returns:
            A connection usable as a context manager.
        """
        conn = self._database.connect()
        conn.execute(_CREATE_FUNDAMENTALS_TABLE)
        conn.execute(_CREATE_FUNDAMENTALS_FETCH_LOG_TABLE)
        for statement in _ALTER_FUNDAMENTALS_FETCH_LOG_STATEMENTS:
            conn.execute(statement)
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

        Raises:
            NonFiniteBarsError: Any OHLCV value is NaN/±inf. The batch is
                rejected whole, before any partition file is touched
                (Issue #227); normalization stays each provider's job
                (`data/base.py`), and this is the layer under it.
        """
        if df.empty:
            return

        working = df.copy()
        working["date"] = pd.to_datetime(working["date"]).dt.date
        _reject_non_finite_bars(working)
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

    def read_fundamentals_fetch_state(
        self, symbols: Sequence[str]
    ) -> dict[str, FundamentalsFetchState]:
        """Read each symbol's EDGAR fetch bookkeeping.

        Feeds the weekly/incremental refresh rule in `pipeline/daily.py`'s
        fundamentals step (`docs/03_basic_design.md` 8.3). Both values are
        bookkeeping *metadata* and neither is a point-in-time cutoff: what a
        caller may read out of `fundamentals` is still governed solely by
        `as_of` against `filed_at`.

        Args:
            symbols: Tickers to look up; an empty sequence returns `{}`
                without touching the database.

        Returns:
            `{symbol: state}`, omitting every symbol that has never been
            fetched (which the caller must read as "fetch it now", not as
            "fetched long ago").
        """
        if not symbols:
            return {}
        placeholders = ",".join("?" for _ in symbols)
        with self.get_connection() as conn:
            rows = conn.execute(
                f"SELECT symbol, CAST(last_fetched_at AS DATE), "  # noqa: S608 - placeholders only, values are bound
                f"CAST(fetched_through AS DATE), consecutive_empty "
                f"FROM fundamentals_fetch_log WHERE symbol IN ({placeholders})",
                list(symbols),
            ).fetchall()
        return {
            symbol: FundamentalsFetchState(
                last_fetched_on=last_fetched_on,
                fetched_through_on=fetched_through_on,
                consecutive_empty=consecutive_empty or 0,
            )
            for symbol, last_fetched_on, fetched_through_on, consecutive_empty in rows
        }

    def record_fundamentals_fetches(
        self, stamps: Sequence[FundamentalsFetchStamp]
    ) -> None:
        """Write one row per polled symbol, atomically.

        Recorded for every symbol whose network fetch *succeeded*, including
        one that yielded no record — that is precisely the case a
        `fundamentals`-row timestamp cannot represent, and leaving it
        unrecorded would re-fetch the symbol on every run forever. An empty
        answer is distinguished by its `consecutive_empty`, not by being
        omitted.

        A symbol whose fetch raised must not be stamped, so the next run
        retries it.

        Args:
            stamps: Rows to write; an empty sequence is a no-op.
        """
        if not stamps:
            return
        with self.get_connection() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                for stamp in stamps:
                    conn.execute(
                        _UPSERT_FUNDAMENTALS_FETCH_LOG,
                        [
                            stamp.symbol,
                            stamp.last_fetched_at,
                            stamp.fetched_through,
                            stamp.consecutive_empty,
                        ],
                    )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def read_latest_filing_dates(
        self, symbols: Sequence[str], forms: Sequence[str], as_of: date
    ) -> dict[str, date]:
        """Read each symbol's newest ingested filing date, visible at `as_of`.

        Answers only "what is the most recent filing we already hold?", which
        is what the fundamentals refresh trigger needs to tell a filing that
        has landed from one still pending. Deliberately *not* built on
        `read_filing_dates()`: that helper exists for the backtest earliest
        calendar, so it materializes every quarter's date and collapses
        corrections to the earliest filing per fiscal period. Both properties
        are wrong here (an amended filing's later date is exactly what proves
        the period landed) and materializing ~500 symbols' full history to
        take a maximum is wasted work. `MAX` in SQL, one row per symbol.

        Args:
            symbols: Tickers to look up; an empty sequence returns `{}`
                without touching the database.
            forms: SEC form types that count, matched exactly.
            as_of: Point-in-time cutoff. A filing accepted *on* `as_of` is
                visible; one accepted the next day is not.

        Returns:
            `{symbol: newest visible filing date}`, omitting symbols with no
            visible filing.
        """
        if not symbols or not forms:
            return {}
        symbol_placeholders = ",".join("?" for _ in symbols)
        form_placeholders = ",".join("?" for _ in forms)
        with self.get_connection() as conn:
            rows = conn.execute(
                f"SELECT symbol, MAX(CAST(filed_at AS DATE)) "  # noqa: S608 - placeholders only, values are bound
                f"FROM fundamentals "
                f"WHERE symbol IN ({symbol_placeholders}) "
                f"AND form IN ({form_placeholders}) "
                f"AND CAST(filed_at AS DATE) <= ? "
                f"GROUP BY symbol",
                [*symbols, *forms, as_of],
            ).fetchall()
        return dict(rows)

    def read_filing_dates(
        self, symbols: Sequence[str], forms: Sequence[str], as_of: date
    ) -> dict[str, tuple[date, ...]]:
        """Read each symbol's periodic-report filing dates, visible at `as_of`.

        The `fundamentals` table is the only point-in-time filing history the
        application keeps (`filed_at` is the SEC acceptance date), so it is
        also the only honest source for "when did this company last report"
        in a historical replay (Issue #201). One row per `accession_no` means
        a corrected re-filing of the same period would otherwise count as a
        second reporting event, so rows are collapsed to the **earliest**
        filing date per `(symbol, fiscal_period_end)`.

        Args:
            symbols: Tickers to read; an empty sequence returns `{}` without
                touching the database.
            forms: SEC form types that count as a reporting event (e.g.
                `("10-K", "10-Q")`). Matched exactly, so amendments are
                included only when named.
            as_of: Point-in-time cutoff. A filing accepted *on* `as_of` is
                visible; one accepted the next day is not.

        Returns:
            `{symbol: distinct filing dates, ascending}`, omitting symbols
            with no visible filing. Two periods filed on the same day are one
            date, so the caller reads reporting *events*, not rows. Never a
            partially-visible symbol: the cutoff is applied in this query,
            not by the caller.
        """
        if not symbols or not forms:
            return {}
        symbol_placeholders = ",".join("?" for _ in symbols)
        form_placeholders = ",".join("?" for _ in forms)
        with self.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT symbol, MIN(CAST(filed_at AS DATE)) AS filed_on
                FROM fundamentals
                WHERE symbol IN ({symbol_placeholders})
                  AND form IN ({form_placeholders})
                  AND CAST(filed_at AS DATE) <= ?
                GROUP BY symbol, fiscal_period_end
                ORDER BY symbol, filed_on
                """,  # noqa: S608 -- placeholders only; every value is bound
                [*symbols, *forms, as_of],
            ).fetchall()
        dates_by_symbol: dict[str, set[date]] = {}
        for symbol, filed_on in rows:
            dates_by_symbol.setdefault(symbol, set()).add(filed_on)
        return {
            symbol: tuple(sorted(dates)) for symbol, dates in dates_by_symbol.items()
        }

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
