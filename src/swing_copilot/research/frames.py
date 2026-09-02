"""Read-only DataFrame accessors over the accumulated DuckDB history.

Every function opens a `read_only=True` connection for exactly one query and
closes it before returning. Both halves matter: read-only makes mutating
operator-owned state impossible (INSERT/DDL fail loudly), and the
open-query-close discipline keeps the file lock held for milliseconds —
DuckDB's lock is exclusive between a read-write process and everything else,
so a notebook that *held* a connection (read-only or not) across think-time
would block the unattended daily run. Never keep a raw connection open in a
notebook; call these functions instead.

The joined views these functions read (`v_verdict_scorecard`, `v_candidates`,
`v_truncated_candidates`, `v_universe_forward_returns`, `v_signal_hits`,
`v_verdict_reasons`, `v_tracked_positions`, `v_symbol_sector_asof`) are defined in
`storage/schema.py` and created by `StateStore.init_schema()` — i.e. by any
daily run. On a database from before the views existed, call
`ensure_views()` once (it opens read-write briefly).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pandas as pd

from swing_copilot.data.adjustments import adjust_bars
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    BARS_COLUMNS,
    DEFAULT_PARQUET_ROOT,
    MarketStore,
    validate_bars_format,
)
from swing_copilot.storage.state_store import StateStore

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date


class ResearchError(SwingCopilotError):
    """Raised when a research read cannot be served from the given database."""


def ensure_views(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Create or refresh the analysis views (opens read-write briefly).

    A daily run does this on every start; this entry point exists for a
    database that has data but predates the views, so a notebook can
    self-serve instead of waiting for the next run.
    """
    StateStore(Database(db_path)).init_schema()


def query(
    sql: str,
    params: Sequence[object] | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """Run arbitrary read-only SQL against the DuckDB file.

    The escape hatch for questions the canned accessors do not answer. The
    connection is read-only, so a mutating statement fails loudly instead of
    corrupting operator-owned state.

    Args:
        sql: A SELECT (or other read-only) statement.
        params: Bound parameters for `?` placeholders.
        db_path: DuckDB file to read.

    Returns:
        The result set as a pandas DataFrame.

    Raises:
        ResearchError: The database file does not exist, or `sql` references
            a table/view the file does not have (hint: `ensure_views()`).
    """
    path = Path(db_path)
    if not path.exists():
        msg = f"database file not found: {path}"
        raise ResearchError(msg)
    database = Database(path, read_only=True)
    with database.connect() as conn:
        try:
            relation = (
                conn.execute(sql) if params is None else conn.execute(sql, params)
            )
            return relation.df()
        except duckdb.CatalogException as exc:
            msg = (
                f"{exc} — the analysis views are created by StateStore."
                "init_schema(); on a database predating them, run "
                "swing_copilot.research.ensure_views() once."
            )
            raise ResearchError(msg) from exc


def runs(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Every recorded run: date, mode, status, config hash, timing."""
    return query("SELECT * FROM runs ORDER BY run_date, started_at", db_path=db_path)


def candidates(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Ranked candidates with the score breakdown lifted out of `metrics_json`.

    One row per (run, symbol, strategy); reads `v_candidates`, so the score
    components (`score_rsi_pullback`, ...) and raw ranking inputs (`rsi14`,
    `atr14`, `close`, `avg_volume`, ...) arrive as typed columns.
    """
    return query(
        "SELECT * FROM v_candidates ORDER BY run_date, strategy_key, rank",
        db_path=db_path,
    )


def verdicts(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """The qualitative layer's proceed/skip judgements, one row per verdict."""
    return query(
        "SELECT * FROM verdicts ORDER BY as_of, symbol",
        db_path=db_path,
    )


def verdict_outcomes(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Matured 5/20-session forward returns and HIT/MISS classifications."""
    return query(
        "SELECT * FROM verdict_outcomes ORDER BY as_of, symbol, horizon_days",
        db_path=db_path,
    )


def scorecard(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """The one-stop verdict scorecard: `v_verdict_scorecard` as a DataFrame.

    One row per (verdict, matured horizon) — a verdict with no matured
    outcome keeps a single row with NULL horizon columns. Joins the verdict
    to its forward return, score breakdown, risk binding constraint, market
    regime, tracked virtual position, and as-of GICS sector, so questions
    like "how do proceeds do under a caution regime?" are a one-line
    `groupby` instead of a five-table join.
    """
    return query(
        "SELECT * FROM v_verdict_scorecard ORDER BY run_date, symbol, horizon_days",
        db_path=db_path,
    )


def tracked_positions(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Virtual positions the tracking ledger carries for each verdict."""
    return query(
        "SELECT * FROM v_tracked_positions ORDER BY entry_date, symbol",
        db_path=db_path,
    )


def screening_rejections(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Why each symbol was rejected, one row per (run, symbol)."""
    return query(
        "SELECT * FROM screening_rejections ORDER BY as_of, symbol",
        db_path=db_path,
    )


def truncated_candidates(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """The near-misses `candidate_limit` cut, with their score breakdown.

    Reads `v_truncated_candidates`, whose columns line up with
    `candidates()`' so the two populations can be compared (or concatenated)
    directly: "is rank 6-10 really worse than rank 1-5" is the question these
    rows exist for (Issue #188). Only the retained top of the tail is stored
    -- see `audit_records.PERSISTED_TRUNCATION_MULTIPLIER`.
    """
    return query(
        "SELECT * FROM v_truncated_candidates ORDER BY run_date, strategy_key, rank",
        db_path=db_path,
    )


def universe_forward_returns(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Forward returns for every screening decision, not just the candidates.

    Reads `v_universe_forward_returns`: one row per (evaluated run, symbol,
    horizon), tagged `candidate` / `truncated` / `rejected` and carrying the
    rejection's own `reason_code`. This is the frame that makes a filter's
    worth measurable -- ``df[df.outcome_class == "rejected"].groupby(
    "reason_code")["forward_return_pct"].mean()`` says what the symbols each
    filter threw away actually went on to do (Issue #188).
    """
    return query(
        """
        SELECT * FROM v_universe_forward_returns
        ORDER BY run_date, horizon_days, outcome_class, symbol
        """,
        db_path=db_path,
    )


def signal_hits(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Every signal hit a run produced, keyed by `run_id` (Issue #192).

    Reads `v_signal_hits`, i.e. the `run_id`-keyed `signal_hits` table — not
    the legacy `signals` table, whose `run_date` key let a same-day `dry_run`
    and `live` run overwrite each other and which cannot be joined to any
    other table. Includes hits on symbols that fired one signal but not all
    of them, so "which signal was the bottleneck" is answerable.
    """
    return query(
        """
        SELECT * FROM v_signal_hits
        ORDER BY run_date, strategy_key, symbol, signal_name
        """,
        db_path=db_path,
    )


def verdict_reasons(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """One row per individual verdict reason, with its citation count.

    Reads `v_verdict_reasons`, the normalized projection of
    `verdicts.reasons_json` (Issue #192), so questions about the reasoning
    itself are plain filters: `df[df.source_id_count == 0]` is every reason
    that rested on no news or filing at all, and `basis` carries Issue #191's
    evidence-kind tag where the reason was written with one.
    """
    return query(
        """
        SELECT * FROM v_verdict_reasons
        ORDER BY run_date, symbol, reason_index
        """,
        db_path=db_path,
    )


def regime_snapshots(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """The deterministic market-regime state recorded for each run."""
    return query(
        """
        SELECT g.*, r.run_date
        FROM regime_snapshots g
        JOIN runs r ON r.run_id = g.run_id
        ORDER BY g.as_of
        """,
        db_path=db_path,
    )


def bars(
    symbols: Sequence[str] | None = None,
    parquet_root: Path | str = DEFAULT_PARQUET_ROOT,
    *,
    as_of: date | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """Daily OHLCV bars straight from the Parquet partitions.

    Runs on an in-memory DuckDB connection over `read_parquet`, touching the
    shared database file not at all — the safest possible way to pull price
    history while anything else is running.

    Stored bars are **raw** (as-traded), which is the default here: they are
    the prices that actually printed, and they never change. To see instead
    what a reader saw on some past date — the basis a screening run or a
    forward return worked on — pass `as_of`, which applies every split with
    `ex_date <= as_of` exactly as `MarketStore.read_bars` does. That reads
    the splits from `db_path`, briefly and read-only.

    Args:
        symbols: Restrict to these tickers; `None` returns every symbol.
        parquet_root: Root directory of the `year=YYYY` bar partitions.
        as_of: Adjust to this point in time; `None` (the default) returns raw
            bars. Rows are *not* filtered by it — that stays the caller's, so
            "the whole history on August's basis" is expressible.
        db_path: DuckDB file holding `corporate_actions`. Only opened when
            `as_of` is given.

    Returns:
        Tidy bars (`BARS_COLUMNS` plus the `year` hive partition), ordered by
        symbol then date; empty (with the bar columns) when no partition
        files exist yet.

    Raises:
        BarsFormatError: The partitions predate the raw-bar storage model
            (Issue #413) and their adjustment basis is unknown.
        ResearchError: `as_of` was given but `db_path` does not exist.
    """
    root = Path(parquet_root)
    if not any(root.glob("year=*/*.parquet")):
        return pd.DataFrame(columns=list(BARS_COLUMNS))
    validate_bars_format(root)

    glob = str(root / "year=*" / "*.parquet")
    sql = "SELECT * FROM read_parquet(?, hive_partitioning=true) ORDER BY symbol, date"
    params: list[object] = [glob]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        sql = (
            f"SELECT * FROM read_parquet(?, hive_partitioning=true) "  # noqa: S608 - placeholders are bound parameters
            f"WHERE symbol IN ({placeholders}) ORDER BY symbol, date"
        )
        params.extend(symbols)
    with duckdb.connect() as conn:
        raw = conn.execute(sql, params).df()
    if as_of is None or raw.empty:
        return raw

    path = Path(db_path)
    if not path.exists():
        msg = f"database file not found: {path}"
        raise ResearchError(msg)
    raw["date"] = pd.to_datetime(raw["date"]).dt.date
    store = MarketStore(Database(path, read_only=True), root)
    splits = store.read_splits(sorted(set(raw["symbol"])), as_of=as_of)
    return adjust_bars(raw, splits, as_of)
