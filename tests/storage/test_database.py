"""Tests for the single DuckDB connection wrapper."""

from __future__ import annotations

import duckdb
import pytest

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

    def test_session_timezone_is_utc_regardless_of_host_locale(self, tmp_path):
        # TIMESTAMPTZ -> DATE casts (as_of point-in-time boundaries) must be
        # deterministic no matter which timezone the host machine runs in.
        database = Database(tmp_path / "copilot.duckdb")

        with database.connect() as conn:
            timezone = conn.execute("SELECT current_setting('TimeZone')").fetchone()

        assert timezone == ("UTC",)


class TestReadOnly:
    def test_read_only_connection_rejects_writes(self, tmp_path):
        db_path = tmp_path / "copilot.duckdb"
        with Database(db_path).connect() as conn:
            conn.execute("CREATE TABLE t (a INTEGER)")

        readonly = Database(db_path, read_only=True)
        with readonly.connect() as conn:
            assert conn.execute("SELECT count(*) FROM t").fetchone() == (0,)
            with pytest.raises(duckdb.Error):
                conn.execute("INSERT INTO t VALUES (1)")
            with pytest.raises(duckdb.Error):
                conn.execute("CREATE TABLE t2 (a INTEGER)")

    def test_read_only_keeps_utc_timezone(self, tmp_path):
        db_path = tmp_path / "copilot.duckdb"
        with Database(db_path).connect() as conn:
            conn.execute("SELECT 1")

        with Database(db_path, read_only=True).connect() as conn:
            timezone = conn.execute("SELECT current_setting('TimeZone')").fetchone()

        assert timezone == ("UTC",)

    def test_read_only_never_creates_the_file_or_its_parent(self, tmp_path):
        # A read-only connection must not manufacture an empty database
        # where none exists — that would mask a wrong path with an
        # empty-but-valid file.
        db_path = tmp_path / "nested" / "missing.duckdb"

        with pytest.raises(duckdb.Error):
            Database(db_path, read_only=True).connect()

        assert not db_path.parent.exists()
