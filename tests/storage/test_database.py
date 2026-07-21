"""Tests for the single DuckDB connection wrapper."""

from __future__ import annotations

from swing_copilot.storage.database import Database


class TestDatabase:
    def test_connect_creates_parent_directory(self, tmp_path):
        db_path = tmp_path / "nested" / "copilot.duckdb"
        database = Database(db_path)

        with database.connect() as conn:
            conn.execute("CREATE TABLE t (a INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")

        assert db_path.is_file()

    def test_data_persists_across_connections(self, tmp_path):
        db_path = tmp_path / "copilot.duckdb"
        database = Database(db_path)

        with database.connect() as conn:
            conn.execute("CREATE TABLE t (a INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")

        with database.connect() as conn:
            result = conn.execute("SELECT a FROM t").fetchall()

        assert result == [(42,)]
