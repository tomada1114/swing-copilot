"""Single DuckDB connection management (NFR-02, NFR-05).

`MarketStore` and `StateStore` are separate logical repositories that both
share this one physical file — no SQLite, no second database.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

DEFAULT_DB_PATH = Path("data/copilot.duckdb")


@contextmanager
def atomic(conn: duckdb.DuckDBPyConnection) -> Iterator[duckdb.DuckDBPyConnection]:
    """Run one transaction on an already-open connection (AGENTS.md).

    One logical write = one transaction: this commits when the block exits
    normally, and rolls back (then re-raises) on any exception. It is the one
    place `BEGIN TRANSACTION`/`COMMIT`/`ROLLBACK` are spelled out; every write
    path in `storage/` goes through this (usually via `Database.transaction()`
    below, which also owns opening and closing the connection) instead of
    repeating the try/except boilerplate.

    Call this directly, instead of `Database.transaction()`, only when the
    caller must do something on the connection *before* the transaction
    starts — `MarketStore.get_connection()`'s schema/view setup, or a read
    that decides whether there is anything to write at all — or has no
    `Database` at hand, only a bare connection (e.g. a schema migration step
    that runs inside `StateStore.init_schema()`'s own connection).

    Args:
        conn: An already-open connection. DuckDB has no nested transactions,
            so it must not already be inside one.

    Yields:
        The same connection, ready for statements.
    """
    conn.execute("BEGIN TRANSACTION")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def fetch_records(
    conn: duckdb.DuckDBPyConnection, query: str, params: Sequence[object] = ()
) -> list[dict[str, object]]:
    """Run `query` and return its rows keyed by column name, not position.

    A positional row (`row[7]`) silently reads the wrong value the moment a
    column is added or reordered — no type error, just a value shifted one
    seat over (AGENTS.md; Issue #192's `efba1c2` had to hand-realign a SELECT
    and its unpacking for exactly this reason). `record["stop_price"]` either
    reads the right value or raises `KeyError`, and `strict=True` on the
    `zip` below means a `columns`/`row` length mismatch fails loudly too,
    instead of silently truncating or padding.

    Args:
        conn: An open connection to run `query` on.
        query: The SQL to execute.
        params: Bound parameters for `query`.

    Returns:
        One dict per row, keyed by the query's own column names, in
        `fetchall()`'s row order.
    """
    cursor = conn.execute(query, list(params))
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


class Database:
    """Owns the one DuckDB file all structured state lives in."""

    def __init__(
        self, db_path: Path | str = DEFAULT_DB_PATH, *, read_only: bool = False
    ) -> None:
        """Create the wrapper.

        Args:
            db_path: Path to the DuckDB file. Its parent directory is
                created on first `connect()` (write mode only).
            read_only: Open every connection read-only. This is what
                `swing_copilot.research` uses: it structurally rules out
                mutating operator-owned state (INSERT/DDL fail loudly), and
                any number of read-only *processes* may share the file.
                Note DuckDB's file lock is still exclusive between a
                read-write process and everything else — a held read-only
                connection blocks the daily run's writes just like a held
                read-write one, so analysis connections must stay
                short-lived (open, query, close), which the `research`
                accessors do. A read-only connection cannot run DDL, so the
                file must already contain an initialized schema.
        """
        self.db_path = Path(db_path)
        self.read_only = read_only

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Open a connection, usable as a context manager per call site.

        Returns:
            A new DuckDB connection to `db_path`.

        Raises:
            duckdb.IOException: `read_only` is set and the file does not
                exist (a read-only connection cannot create it).
        """
        if not self.read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(self.db_path), read_only=self.read_only)
        # TIMESTAMPTZ -> DATE casts (as_of point-in-time boundaries) must be
        # deterministic regardless of the host machine's local timezone.
        conn.execute("SET TimeZone='UTC'")
        return conn

    @contextmanager
    def transaction(
        self, conn: duckdb.DuckDBPyConnection | None = None
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        """One logical write = one transaction (AGENTS.md).

        The common case (`conn` omitted) opens a fresh connection via
        `connect()`, runs the block as one transaction on it, and closes it
        on exit — the single primitive every write path in `storage/` uses
        in place of its own hand-written `BEGIN TRANSACTION`/`try`/`except
        Exception: ROLLBACK; raise`/`else: COMMIT` boilerplate.

        Pass an already-open `conn` (see `atomic()`) when a caller obtained
        one itself and must keep using that same connection — this method
        then wraps it in a transaction without closing it early. DuckDB has
        no nested transactions, so never call this inside another
        `transaction()`/`atomic()` block.

        Args:
            conn: An already-open connection to run the transaction on,
                instead of opening (and later closing) a new one.

        Yields:
            The connection statements should execute against.
        """
        if conn is None:
            with self.connect() as owned_conn, atomic(owned_conn) as tx_conn:
                yield tx_conn
            return
        with atomic(conn) as tx_conn:
            yield tx_conn
