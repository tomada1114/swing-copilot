"""Market data repository: Parquet bars + DuckDB fundamentals (FR-02, FR-03).

Bars are the large, append-heavy time series, so they live in Hive-partitioned
Parquet (`year=YYYY/data.parquet`) and DuckDB only provides a `read_parquet`
view over them — never a second copy of the raw rows (`docs/03_basic_design.md`
5). Stored bars are **raw (as-traded) and immutable**: a re-fetch that lands
within `_MAX_CORRECTION_RATIO` of a stored close replaces it as a correction,
and anything further apart quarantines that *symbol* for the whole write
rather than overwriting history with a second adjustment basis (Issue #413).
Split adjustment is applied on *read*, as of the caller's `as_of`, from the
`corporate_actions` table — so what `read_bars` returns depends on the
requested point in time and never on when the bar was fetched. Each affected
year partition is still published by write-to-temp-then-rename, so a crash
mid-write never corrupts the previous partition. A store's basis is recorded
in `_format.json` beside the partitions; a partitioned store without that
marker holds pre-Issue-#413 adjusted bars and is refused until
`copilot-backfill rebuild` has rewritten it.

Fundamentals are comparatively small structured records,
so they live directly in a DuckDB table, natural-keyed by `accession_no` (the
one truly unique identifier for an SEC filing; `storage/database.py`'s DDL is
authoritative over the docstring prose in `docs/04_detailed_design.md` 3.7,
which loosely paraphrases the key as `(symbol, fiscal_period)`).
"""

from __future__ import annotations

import io
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pandas as pd

from swing_copilot.data.adjustments import (
    SplitEvent,
    adjust_bars,
    has_mixed_basis_signature,
)
from swing_copilot.data.base import ACTIONS_COLUMNS
from swing_copilot.exceptions import StorageSchemaError, SwingCopilotError
from swing_copilot.io_atomic import write_bytes_atomically

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

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
#: The stored-bar columns `read_corporate_actions` mirrors for its own rows.
CORPORATE_ACTION_COLUMNS = (*ACTIONS_COLUMNS, "provider", "fetched_at")
#: Marker file recording which basis the partitions hold, written beside them.
#: Public because `scripts/data_sync.py` has to mirror it to R2 alongside the
#: partitions: a bars tree that arrives on a fresh runner without its marker
#: reads as an unmigrated store and fails every `read_bars`/`write_bars`.
BARS_FORMAT_MARKER_NAME = "_format.json"
#: Its only accepted content. `basis` is prose for a human reading the file;
#: `version` is what a future migration would bump.
_FORMAT_MARKER_PAYLOAD = {"basis": "raw", "version": 2}
#: How far a re-fetched close may sit from the stored one and still count as
#: a *correction* rather than a change of adjustment basis. Yahoo revises a
#: close by fractions of a cent; a split or a dividend re-basing moves it by
#: percent or more (`design-pit-prices.md` 3).
_MAX_CORRECTION_RATIO = 0.005


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


class BarsFormatError(SwingCopilotError):
    """Raised when the bars root's adjustment basis is unknown or wrong.

    Partitions written before Issue #413 hold whatever adjustment basis the
    provider happened to return that day, and mixing those rows with raw
    (as-traded) ones would produce a series that is wrong in a way no reader
    can detect. So a partitioned root without the `_format.json` marker --
    or with a marker naming a different basis -- is refused outright by every
    read and write, and only `replace_symbol_bars` (the rebuild path, which
    overwrites the offending rows wholesale) may proceed without it.
    """


@dataclass(frozen=True, slots=True)
class BarQuarantine:
    """One symbol whose rows `write_bars` refused, and why."""

    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class BarWriteResult:
    """What `write_bars` did, for a caller that reports data quality.

    Returned rather than raised: a quarantined symbol is a fail-soft,
    per-symbol data-quality event, exactly like a provider's `FetchFailure`,
    and must not cost the run the other 499 symbols. A caller with nothing to
    report may ignore the result.
    """

    quarantined: tuple[BarQuarantine, ...] = ()


#: Corporate actions live beside `fundamentals` in DuckDB rather than in the
#: Parquet bars: they are few, they are keyed by event (not by session), and
#: every read of them is a lookup joined to a symbol list. `value` carries the
#: split factor or the cash dividend per share, per `kind`.
_CREATE_CORPORATE_ACTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol     VARCHAR NOT NULL,
    ex_date    DATE NOT NULL,
    kind       VARCHAR NOT NULL CHECK (kind IN ('split', 'dividend')),
    value      DOUBLE NOT NULL,
    provider   VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, ex_date, kind)
)
"""

#: Correction upsert (AGENTS.md): a provider that revises a split factor or a
#: dividend amount must be able to overwrite what it said before.
_UPSERT_CORPORATE_ACTION = """
INSERT INTO corporate_actions (symbol, ex_date, kind, value, provider, fetched_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (symbol, ex_date, kind) DO UPDATE SET
    value = EXCLUDED.value,
    provider = EXCLUDED.provider,
    fetched_at = EXCLUDED.fetched_at
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


def _format_marker_path(parquet_root: Path) -> Path:
    """Where a bars root records the adjustment basis it holds."""
    return parquet_root / BARS_FORMAT_MARKER_NAME


def write_bars_format_marker(parquet_root: Path) -> None:
    """Stamp `parquet_root` as holding raw (as-traded) bars.

    Written through `io_atomic` like every other replacement in this
    repository, so a crash mid-write cannot leave a truncated marker that
    then reads as "wrong basis" and locks the operator out of their own data.

    Args:
        parquet_root: The bars root; created if it does not exist yet.
    """
    parquet_root.mkdir(parents=True, exist_ok=True)
    body = json.dumps(_FORMAT_MARKER_PAYLOAD, sort_keys=True) + "\n"
    write_bytes_atomically(_format_marker_path(parquet_root), body.encode("utf-8"))


def validate_bars_format(parquet_root: Path) -> None:
    """Refuse a partitioned bars root that does not hold raw bars.

    An empty (or absent) root is fine — there is nothing there to
    misinterpret, and the first `write_bars` stamps it.

    Args:
        parquet_root: The bars root to check.

    Raises:
        BarsFormatError: The root has partitions but no readable marker
            naming this basis. The message names the rebuild command, because
            the only correct repair is re-fetching the affected history.
    """
    if not any(parquet_root.glob("year=*/*.parquet")):
        return
    marker = _format_marker_path(parquet_root)
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        recorded = None
    if recorded == _FORMAT_MARKER_PAYLOAD:
        return
    msg = (
        f"価格バーの形式マーカーが読めない、または一致しない: {marker}"
        f"（期待: {_FORMAT_MARKER_PAYLOAD}、実際: {recorded!r}）。"
        "Issue #413 以前のパーティションは調整基準が混在した価格を保持しており、"
        "生値（as-traded）の行と混ぜると誰にも検出できない系列になる。"
        "`copilot-backfill rebuild` で全履歴を取り直して置き換えること。"
    )
    raise BarsFormatError(msg)


def _quarantine_reasons(
    new_rows: pd.DataFrame,
    existing: pd.DataFrame,
    splits_by_symbol: Mapping[str, Sequence[SplitEvent]],
) -> dict[str, str]:
    """Which symbols in `new_rows` must not be written, and why.

    Two independent quality gates, both per symbol and both fail-closed:

    1. The incoming close series carries a mixed-basis signature — the
       provider handed over adjusted and unadjusted rows in one response
       (Issue #413). Checked *before* merging with stored rows, so a clean
       history cannot mask a broken batch. The symbol's known splits are
       part of the question: a flip is a *split-sized* step, and a symbol
       with no split has no second basis to flip to (Issue #421).
    2. A row overlapping a stored `(symbol, date)` disagrees with it by more
       than `_MAX_CORRECTION_RATIO`. Raw bars are immutable facts; a real
       correction is fractions of a percent, and a larger move means the two
       rows are quoted on different bases. Only `close` is compared: Yahoo
       revises volume days later as a matter of course, and OHL move with
       `close` anyway.

    Args:
        new_rows: The incoming bars, date-normalized.
        existing: Stored rows for the same symbols, from the affected year
            partitions. May be empty.
        splits_by_symbol: Each symbol's known splits. A symbol absent from
            the mapping has none, so gate 1 cannot fire for it.

    Returns:
        `{symbol: reason}` for every symbol that must be skipped.
    """
    reasons: dict[str, str] = {}
    stored_closes = (
        {}
        if existing.empty
        else dict(
            zip(
                zip(existing["symbol"], existing["date"], strict=True),
                existing["close"],
                strict=True,
            )
        )
    )
    for symbol, rows in new_rows.groupby("symbol", sort=True):
        ordered = rows.sort_values("date")
        if has_mixed_basis_signature(
            ordered["close"], splits_by_symbol.get(str(symbol), ())
        ):
            reasons[str(symbol)] = (
                "調整済みと未調整の行が混在した署名を検出した"
                f"（{ordered['date'].iloc[0]}〜{ordered['date'].iloc[-1]}）"
            )
            continue
        conflict = _first_basis_conflict(ordered, stored_closes)
        if conflict is not None:
            reasons[str(symbol)] = conflict
    return reasons


def _first_basis_conflict(
    ordered: pd.DataFrame, stored_closes: dict[tuple[object, object], float]
) -> str | None:
    """The first stored close this batch would overwrite too far, if any."""
    closes = pd.to_numeric(ordered["close"], errors="coerce")
    for symbol, bar_date, close in zip(
        ordered["symbol"], ordered["date"], closes, strict=True
    ):
        stored = stored_closes.get((symbol, bar_date))
        if stored is None or not math.isfinite(stored) or stored == 0.0:
            continue
        deviation = abs(close / stored - 1.0)
        if deviation > _MAX_CORRECTION_RATIO:
            return (
                f"既存の生値と{deviation:.2%}乖離する行がある"
                f"（{bar_date}: 既存 {stored} → 新規 {close}）。"
                "許容訂正幅を超える差は調整基準の変化なので、"
                "`copilot-backfill rebuild` で明示的に置き換えること。"
            )
    return None


def _read_splits_on(
    conn: duckdb.DuckDBPyConnection, symbols: Sequence[str], as_of: date
) -> dict[str, tuple[SplitEvent, ...]]:
    """Read visible splits on an already-open connection.

    A missing `corporate_actions` table reads as "no splits": read-only
    connections never run DDL, so a database written before Issue #413 must
    still open for reading rather than failing every query.
    """
    placeholders = ",".join("?" for _ in symbols)
    query = f"""
        SELECT symbol, ex_date, value
        FROM corporate_actions
        WHERE kind = 'split' AND symbol IN ({placeholders}) AND ex_date <= ?
        ORDER BY symbol, ex_date
    """  # noqa: S608 - placeholders are bound parameters, not interpolated values
    try:
        rows = conn.execute(query, [*symbols, as_of]).fetchall()
    except duckdb.CatalogException:
        return {}
    splits: dict[str, list[SplitEvent]] = {}
    for symbol, ex_date, value in rows:
        splits.setdefault(str(symbol), []).append(
            SplitEvent(ex_date=_as_date(ex_date), factor=float(value))
        )
    return {symbol: tuple(events) for symbol, events in splits.items()}


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

    @property
    def database(self) -> Database:
        """Expose the shared database for read-only cross-module reuse.

        Callers that need to inspect persisted state should use this seam
        rather than reaching into the store's private database attribute.
        """
        return self._database

    def _has_partition_files(self) -> bool:
        return any(self.parquet_root.glob("year=*/*.parquet"))

    def get_connection(self) -> duckdb.DuckDBPyConnection:
        """Return a DuckDB connection ready to query fundamentals and bars.

        Write connections ensure `fundamentals`, `fundamentals_fetch_log`
        and `corporate_actions`; read-only connections require those tables
        to already exist -- except `corporate_actions`, whose absence every
        reader treats as "no corporate action recorded" so a database
        predating Issue #413 still opens. The `bars` view is (re)created only
        when at least one bar partition file exists, and is temporary for
        read-only connections.

        Returns:
            A connection usable as a context manager.
        """
        conn = self._database.connect()
        if not self._database.read_only:
            conn.execute(_CREATE_FUNDAMENTALS_TABLE)
            conn.execute(_CREATE_FUNDAMENTALS_FETCH_LOG_TABLE)
            conn.execute(_CREATE_CORPORATE_ACTIONS_TABLE)
            for statement in _ALTER_FUNDAMENTALS_FETCH_LOG_STATEMENTS:
                conn.execute(statement)
        if self._has_partition_files():
            glob = str(self.parquet_root / "year=*" / "*.parquet")
            view_kind = "TEMP " if self._database.read_only else ""
            conn.execute(
                f"CREATE OR REPLACE {view_kind}VIEW bars AS "  # noqa: S608 - glob is a local path, not user input
                f"SELECT * FROM read_parquet('{glob}', hive_partitioning=true)"
            )
        return conn

    def validate_read_only_schema(self) -> None:
        """Verify the fundamentals table without running migrations.

        Raises:
            StorageSchemaError: The database does not contain fundamentals.
        """
        with self._database.connect() as conn:
            try:
                conn.execute("SELECT 1 FROM fundamentals LIMIT 0")
            except duckdb.CatalogException as exc:
                msg = "required table 'fundamentals' is missing"
                raise StorageSchemaError(msg) from exc

    def write_bars(self, df: pd.DataFrame) -> BarWriteResult:
        """Upsert raw daily OHLCV rows, partitioned by year, `(symbol, date)`-keyed.

        Bars are stored as-traded and treated as immutable facts, so this is
        an upsert only within `_MAX_CORRECTION_RATIO`; a symbol whose incoming
        rows contradict what is stored, or whose own series carries the
        mixed-basis signature, is quarantined and simply not written. That is
        fail-soft per symbol (the return value), not an exception: one
        provider glitch must not cost a run the other 499 symbols.

        Args:
            df: Rows matching `BARS_COLUMNS` (the Parquet schema, including
                `provider` and `fetched_at` — already stamped by the caller).

        Returns:
            What was skipped. A caller with nothing to report may ignore it.

        Raises:
            NonFiniteBarsError: Any OHLCV value is NaN/±inf. The batch is
                rejected whole, before any partition file is touched
                (Issue #227); normalization stays each provider's job
                (`data/base.py`), and this is the layer under it.
            BarsFormatError: The root holds partitions written on an unknown
                adjustment basis (see `validate_bars_format`).
        """
        if df.empty:
            return BarWriteResult()

        working = df.copy()
        working["date"] = pd.to_datetime(working["date"]).dt.date
        _reject_non_finite_bars(working)
        years = working["date"].map(lambda d: d.year)

        if self._has_partition_files():
            validate_bars_format(self.parquet_root)
        else:
            write_bars_format_marker(self.parquet_root)

        symbols = sorted(set(working["symbol"]))
        existing = self._read_partition_rows(
            (int(year) for year in years.unique()), symbols
        )
        # Splits come from the same store, and every caller records the
        # response's corporate actions before its bars, so the split that
        # could have produced a flip in this batch is already visible here.
        with self.get_connection() as conn:
            splits_by_symbol = _read_splits_on(conn, symbols, date.max)
        reasons = _quarantine_reasons(working, existing, splits_by_symbol)
        if reasons:
            keep = ~working["symbol"].isin(reasons)
            working, years = working[keep], years[keep]

        for year in sorted(years.unique()):
            self._write_partition(int(year), working[years == year])
        return BarWriteResult(
            quarantined=tuple(
                BarQuarantine(symbol=symbol, reason=reason)
                for symbol, reason in sorted(reasons.items())
            )
        )

    def replace_symbol_bars(self, symbols: Sequence[str], df: pd.DataFrame) -> None:
        """Replace every stored row of `symbols` with `df`, across all years.

        The rebuild path (`copilot-backfill rebuild`). `write_bars`' immutable
        -raw gate is deliberately bypassed here and only here: rebuilding is
        precisely the operator-driven act of accepting a new basis for a
        symbol's whole history, so "the new rows contradict the stored ones"
        is the expected state, not a defect. A symbol listed in `symbols` but
        absent from `df` is erased rather than half-replaced, so a rejected
        fetch must be left out of `symbols` to preserve its history.

        Args:
            symbols: Tickers whose stored rows are being replaced wholesale.
            df: Their new raw rows, matching `BARS_COLUMNS`.

        Raises:
            NonFiniteBarsError: Any OHLCV value is NaN/±inf. Validated before
                a partition is touched, exactly as in `write_bars`.
        """
        if not symbols:
            return
        replaced = set(symbols)
        working = df.copy()
        if not working.empty:
            working["date"] = pd.to_datetime(working["date"]).dt.date
            _reject_non_finite_bars(working)
            working = working[working["symbol"].isin(replaced)]

        write_bars_format_marker(self.parquet_root)
        years = (
            working["date"].map(lambda d: d.year)
            if not working.empty
            else pd.Series(dtype="int64")
        )
        touched = {int(year) for year in years.unique()} | {
            int(path.parent.name.removeprefix("year="))
            for path in self.parquet_root.glob("year=*/*.parquet")
        }
        for year in sorted(touched):
            new_rows = working[years == year] if not working.empty else working
            self._replace_partition(year, replaced, new_rows)

    def _partition_file(self, year: int) -> Path:
        return self.parquet_root / f"year={year}" / "data.parquet"

    def _read_partition_rows(
        self, years: Iterable[int], symbols: Sequence[str]
    ) -> pd.DataFrame:
        """Stored rows for `symbols` in the given year partitions.

        Read straight from Parquet rather than through `read_bars`: the gate
        compares *raw* stored values, and `read_bars` would hand back
        as-of-adjusted ones.
        """
        wanted = set(symbols)
        frames = [
            rows
            for year in sorted(set(years))
            if (path := self._partition_file(year)).is_file()
            and not (rows := pd.read_parquet(path)).empty
            and not (rows := rows[rows["symbol"].isin(wanted)]).empty
        ]
        if not frames:
            return _empty_bars_frame()
        combined = pd.concat(frames, ignore_index=True)
        combined["date"] = pd.to_datetime(combined["date"]).dt.date
        return combined

    def _write_partition(self, year: int, new_rows: pd.DataFrame) -> None:
        partition_file = self._partition_file(year)
        if partition_file.is_file():
            existing = pd.read_parquet(partition_file)
            combined = pd.concat([existing, new_rows], ignore_index=True)
        else:
            combined = new_rows
        self._publish_partition(partition_file, combined)

    def _replace_partition(
        self, year: int, replaced: set[str], new_rows: pd.DataFrame
    ) -> None:
        """Drop `replaced`'s rows from one partition, then add `new_rows`."""
        partition_file = self._partition_file(year)
        frames = []
        if partition_file.is_file():
            existing = pd.read_parquet(partition_file)
            frames.append(existing[~existing["symbol"].isin(replaced)])
        if not new_rows.empty:
            frames.append(new_rows)
        combined = (
            pd.concat(frames, ignore_index=True) if frames else _empty_bars_frame()
        )
        if combined.empty:
            # Removed rather than written empty: an all-NULL Parquet column
            # has no usable type, and DuckDB's `read_parquet` union over the
            # partitions then fails to cast it against the other years.
            partition_file.unlink(missing_ok=True)
            return
        self._publish_partition(partition_file, combined)

    def _publish_partition(self, partition_file: Path, combined: pd.DataFrame) -> None:
        partition_file.parent.mkdir(parents=True, exist_ok=True)
        combined = combined.drop_duplicates(subset=["symbol", "date"], keep="last")
        combined = combined.sort_values(["symbol", "date"]).reset_index(drop=True)

        buffer = io.BytesIO()
        combined.to_parquet(buffer, index=False)
        # A unique staging path, not `io_atomic`'s own deterministic
        # `.{name}.tmp`: two writers targeting the same year partition at
        # once (e.g. two `write_bars` calls racing) must never stage into
        # the same temporary file, or one could publish the other's
        # partially-written body.
        temporary_path = partition_file.with_name(
            f".{partition_file.name}.{uuid.uuid4().hex}.tmp"
        )
        write_bytes_atomically(
            partition_file, buffer.getvalue(), temporary_path=temporary_path
        )

    def read_bars(
        self, symbols: list[str], start: date, end: date, as_of: date
    ) -> pd.DataFrame:
        """Read bars for `symbols` over `[start, end]`, never past `as_of`.

        Bars are stored raw, so every split with `ex_date <= as_of` is applied
        here, on read: prices divided and volume multiplied for every row
        dated before the ex-date. A split whose ex-date falls *after* `end`
        but on or before `as_of` still applies to the whole window — that is
        what "the prices a reader saw at `as_of`" means, and it is why a
        forward return computed across a split comes out as a real return
        rather than a 50% crash. Dividends are recorded but never applied.

        Args:
            symbols: Ticker symbols to read.
            start: Inclusive range start.
            end: Inclusive range end.
            as_of: Point-in-time guard — no returned bar is dated after this,
                and no split after it is visible, regardless of `end`.

        Returns:
            Tidy bars (`BARS_COLUMNS`), ordered by symbol then date, on the
            adjustment basis visible at `as_of`.

        Raises:
            BarsFormatError: The root holds partitions written on an unknown
                adjustment basis (see `validate_bars_format`).
        """
        if not symbols or not self._has_partition_files():
            return _empty_bars_frame()
        validate_bars_format(self.parquet_root)

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
        # One connection for both reads: the splits are only needed to
        # interpret these very rows, and DuckDB's file lock is exclusive.
        with self.get_connection() as conn:
            result = conn.execute(query, [*symbols, start, effective_end]).df()
            splits = _read_splits_on(conn, symbols, as_of)
        result["date"] = pd.to_datetime(result["date"]).dt.date
        return adjust_bars(result, splits, as_of)

    def stored_symbols(self) -> tuple[str, ...]:
        """Every symbol that has at least one stored bar, ascending.

        Read from the Parquet partitions rather than the DuckDB `bars` view,
        so an audit (`copilot-backfill check`) can enumerate the store without
        taking DuckDB's exclusive file lock while an operator or the scheduled
        run holds it.

        Returns:
            Sorted, de-duplicated tickers; empty when nothing is stored.
        """
        symbols: set[str] = set()
        for path in sorted(self.parquet_root.glob("year=*/*.parquet")):
            symbols.update(str(value) for value in pd.read_parquet(path)["symbol"])
        return tuple(sorted(symbols))

    def read_raw_bars(
        self, symbols: Sequence[str], start: date | None = None, end: date | None = None
    ) -> pd.DataFrame:
        """Read stored bars **without** any split adjustment.

        The audit counterpart of `read_bars`: an adjusted series is by
        construction free of the mixed-basis signature `copilot-backfill
        check` looks for, so the check has to see the bytes as stored. Like
        `stored_symbols`, this goes straight to Parquet and never opens
        DuckDB.

        Args:
            symbols: Tickers to read; an empty sequence returns an empty frame.
            start: Inclusive range start, or `None` for "from the beginning".
            end: Inclusive range end, or `None` for "to the newest stored bar".

        Returns:
            Tidy raw bars (`BARS_COLUMNS`), ordered by symbol then date.

        Raises:
            BarsFormatError: The root holds partitions written on an unknown
                adjustment basis (see `validate_bars_format`).
        """
        if not symbols or not self._has_partition_files():
            return _empty_bars_frame()
        validate_bars_format(self.parquet_root)
        years = [
            int(path.parent.name.removeprefix("year="))
            for path in self.parquet_root.glob("year=*/*.parquet")
        ]
        if start is not None:
            years = [year for year in years if year >= start.year]
        if end is not None:
            years = [year for year in years if year <= end.year]
        rows = self._read_partition_rows(years, symbols)
        if rows.empty:
            return rows
        if start is not None:
            rows = rows[rows["date"] >= start]
        if end is not None:
            rows = rows[rows["date"] <= end]
        return rows.sort_values(["symbol", "date"]).reset_index(drop=True)

    def read_splits(
        self, symbols: Sequence[str], *, as_of: date
    ) -> dict[str, tuple[SplitEvent, ...]]:
        """Read each symbol's splits visible at `as_of`.

        Args:
            symbols: Tickers to look up; an empty sequence returns `{}`
                without touching the database.
            as_of: Point-in-time cutoff. A split whose ex-date *is* `as_of`
                is visible (the session already trades on the new basis); one
                the day after is not.

        Returns:
            `{symbol: splits, ascending by ex-date}`, omitting symbols with
            no visible split. A database with no `corporate_actions` table
            (one predating Issue #413) reads as "no splits", never as an
            error.
        """
        if not symbols:
            return {}
        with self.get_connection() as conn:
            return _read_splits_on(conn, symbols, as_of)

    def read_corporate_actions(
        self, symbols: Sequence[str], start: date, end: date
    ) -> pd.DataFrame:
        """Read splits *and* dividends with `ex_date` in `[start, end]`.

        The event-level view the tracking ledger and research use; `as_of`
        filtering is the caller's, because a ledger asks "what happened
        between these two marks", not "what was visible on one date".

        Args:
            symbols: Tickers to read; an empty sequence returns an empty
                frame without touching the database.
            start: Inclusive ex-date range start.
            end: Inclusive ex-date range end.

        Returns:
            `CORPORATE_ACTION_COLUMNS` rows ordered by symbol, ex-date, kind.
        """
        empty = pd.DataFrame(columns=list(CORPORATE_ACTION_COLUMNS))
        if not symbols:
            return empty
        placeholders = ",".join("?" for _ in symbols)
        query = f"""
            SELECT symbol, ex_date, kind, value, provider, fetched_at
            FROM corporate_actions
            WHERE symbol IN ({placeholders}) AND ex_date >= ? AND ex_date <= ?
            ORDER BY symbol, ex_date, kind
        """  # noqa: S608 - placeholders are bound parameters, not interpolated values
        with self.get_connection() as conn:
            try:
                return conn.execute(query, [*symbols, start, end]).df()
            except duckdb.CatalogException:
                return empty

    def write_corporate_actions(
        self, df: pd.DataFrame, *, provider: str, fetched_at: datetime
    ) -> None:
        """Upsert corporate actions, keyed by `(symbol, ex_date, kind)`.

        One logical write, one transaction: a provider response's actions all
        land or none do, so a failure halfway through cannot leave a symbol
        with the split recorded and the dividend missing.

        Args:
            df: `ACTIONS_COLUMNS` rows (`symbol`, `ex_date`, `kind`,
                `value`); an empty frame is a no-op.
            provider: Which source these came from, stamped on every row.
            fetched_at: When they were fetched, stamped on every row.
        """
        if df.empty:
            return
        rows = df.copy()
        rows["ex_date"] = pd.to_datetime(rows["ex_date"]).dt.date
        with self.get_connection() as conn, self._database.transaction(conn):
            for row in rows.itertuples(index=False):
                conn.execute(
                    _UPSERT_CORPORATE_ACTION,
                    [
                        row.symbol,
                        row.ex_date,
                        row.kind,
                        row.value,
                        provider,
                        fetched_at,
                    ],
                )

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
        validate_bars_format(self.parquet_root)

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
        with self.get_connection() as conn, self._database.transaction(conn):
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
        with self.get_connection() as conn, self._database.transaction(conn):
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
