"""Tests for the single DuckDB connection wrapper."""

from __future__ import annotations

import duckdb
import pytest

from swing_copilot.storage.database import (
    Database,
    DuplicateColumnsError,
    atomic,
    fetch_records,
)


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


class TestTransaction:
    """`Database.transaction()`.

    The one primitive Issue #395 consolidates ~20 hand-written
    `BEGIN TRANSACTION`/rollback blocks into.
    """

    def test_commits_on_normal_exit(self, tmp_path):
        database = Database(tmp_path / "copilot.duckdb")
        with database.connect() as conn:
            conn.execute("CREATE TABLE t (a INTEGER)")

        with database.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            conn.execute("INSERT INTO t VALUES (2)")

        with database.connect() as conn:
            rows = conn.execute("SELECT a FROM t ORDER BY a").fetchall()
        assert rows == [(1,), (2,)]

    def test_a_failure_after_an_earlier_statement_rolls_it_back_and_reraises(
        self, tmp_path
    ):
        database = Database(tmp_path / "copilot.duckdb")
        with database.connect() as conn:
            conn.execute("CREATE TABLE t (a INTEGER)")

        def _write_then_fail() -> None:
            with database.transaction() as conn:
                conn.execute("INSERT INTO t VALUES (1)")
                msg = "simulated failure"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="simulated failure"):
            _write_then_fail()

        with database.connect() as conn:
            rows = conn.execute("SELECT a FROM t").fetchall()
        assert rows == []

    def test_owns_and_closes_the_connection_it_opens(self, tmp_path):
        database = Database(tmp_path / "copilot.duckdb")

        with database.transaction() as conn:
            conn.execute("SELECT 1")

        with pytest.raises(duckdb.Error):
            conn.execute("SELECT 1")

    def test_wraps_an_already_open_connection_without_closing_it(self, tmp_path):
        # MarketStore.get_connection()-shaped callers must run their own
        # per-connection setup before the transaction starts, then hand that
        # same connection to `transaction()` instead of a fresh one.
        database = Database(tmp_path / "copilot.duckdb")
        conn = database.connect()
        conn.execute("CREATE TABLE t (a INTEGER)")

        with database.transaction(conn) as tx_conn:
            assert tx_conn is conn
            conn.execute("INSERT INTO t VALUES (1)")

        # Still open: the caller, not `transaction()`, owns closing it.
        assert conn.execute("SELECT a FROM t").fetchall() == [(1,)]
        conn.close()

    def test_a_failure_on_a_caller_supplied_connection_rolls_back_without_closing(
        self, tmp_path
    ):
        database = Database(tmp_path / "copilot.duckdb")
        conn = database.connect()
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")

        def _write_then_fail() -> None:
            with database.transaction(conn):
                conn.execute("INSERT INTO t VALUES (2)")
                msg = "simulated failure"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="simulated failure"):
            _write_then_fail()

        # Rolled back to just the pre-existing row, and still usable.
        assert conn.execute("SELECT a FROM t").fetchall() == [(1,)]
        conn.close()


class TestAtomic:
    """`atomic()`: the low-level wrapper `Database.transaction()` composes.

    Exercised directly for callers that already hold an open connection.
    """

    def test_commits_on_normal_exit(self, tmp_path):
        database = Database(tmp_path / "copilot.duckdb")
        conn = database.connect()
        conn.execute("CREATE TABLE t (a INTEGER)")

        with atomic(conn):
            conn.execute("INSERT INTO t VALUES (1)")

        assert conn.execute("SELECT a FROM t").fetchall() == [(1,)]
        conn.close()

    def test_a_failure_rolls_back_and_reraises_without_closing(self, tmp_path):
        database = Database(tmp_path / "copilot.duckdb")
        conn = database.connect()
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")

        def _write_then_fail() -> None:
            with atomic(conn):
                conn.execute("INSERT INTO t VALUES (2)")
                msg = "simulated failure"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="simulated failure"):
            _write_then_fail()

        assert conn.execute("SELECT a FROM t").fetchall() == [(1,)]
        conn.close()


class TestFetchRecords:
    """`fetch_records()`: column-name-keyed rows in place of positional tuples."""

    def test_returns_rows_keyed_by_column_name(self, tmp_path):
        database = Database(tmp_path / "copilot.duckdb")
        with database.connect() as conn:
            conn.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
            conn.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
            records = fetch_records(conn, "SELECT a, b FROM t ORDER BY a")

        assert records == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]

    def test_binds_parameters(self, tmp_path):
        database = Database(tmp_path / "copilot.duckdb")
        with database.connect() as conn:
            conn.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
            conn.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
            records = fetch_records(conn, "SELECT a, b FROM t WHERE a = ?", [2])

        assert records == [{"a": 2, "b": "y"}]

    def test_an_empty_result_returns_an_empty_list(self, tmp_path):
        database = Database(tmp_path / "copilot.duckdb")
        with database.connect() as conn:
            conn.execute("CREATE TABLE t (a INTEGER)")
            records = fetch_records(conn, "SELECT a FROM t")

        assert records == []

    def test_reordering_the_selected_columns_still_reads_correctly_by_name(
        self, tmp_path
    ):
        # The whole point (AGENTS.md): a column reorder must not silently
        # shift a positionally-read value to the wrong field.
        database = Database(tmp_path / "copilot.duckdb")
        with database.connect() as conn:
            conn.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
            conn.execute("INSERT INTO t VALUES (1, 'x')")
            records = fetch_records(conn, "SELECT b, a FROM t")

        assert records == [{"b": "x", "a": 1}]

    def test_a_joined_select_with_duplicate_column_names_raises_instead_of_collapsing(
        self, tmp_path
    ):
        # DuckDB's cursor description reports the bare column name, so
        # `SELECT a.id, a.v, b.id, b.v FROM a JOIN b ...` produces two
        # columns literally named "id" and two named "v". Keying a dict by
        # name would silently keep only the last of each pair -- this must
        # be rejected loudly instead (Issue #398 will point a joined SELECT
        # at this helper).
        database = Database(tmp_path / "copilot.duckdb")
        with database.connect() as conn:
            conn.execute("CREATE TABLE a (id INTEGER, v VARCHAR)")
            conn.execute("CREATE TABLE b (id INTEGER, v VARCHAR)")
            conn.execute("INSERT INTO a VALUES (1, 'a-value')")
            conn.execute("INSERT INTO b VALUES (1, 'b-value')")

            with pytest.raises(DuplicateColumnsError, match=r"id.*v|v.*id"):
                fetch_records(
                    conn, "SELECT a.id, a.v, b.id, b.v FROM a JOIN b ON a.id = b.id"
                )
