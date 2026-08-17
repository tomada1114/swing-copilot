"""Single DuckDB connection management (NFR-02, NFR-05).

`MarketStore` and `StateStore` are separate logical repositories that both
share this one physical file — no SQLite, no second database.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

DEFAULT_DB_PATH = Path("data/copilot.duckdb")


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
