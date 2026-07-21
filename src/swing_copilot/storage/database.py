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

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        """Create the wrapper.

        Args:
            db_path: Path to the DuckDB file. Its parent directory is
                created on first `connect()`.
        """
        self.db_path = Path(db_path)

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Open a connection, usable as a context manager per call site.

        Returns:
            A new DuckDB connection to `db_path`.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(self.db_path))
