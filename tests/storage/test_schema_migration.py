"""Issue #192's migration against a database that already holds history.

`init_schema()` on a *fresh* file is covered everywhere else in the storage
suite (every `state_store` fixture starts empty). These tests cover the other
half, which the production file is: tables created before the promoted columns
existed, holding rows that must survive the upgrade and stay readable.

The legacy DDL is spelled out here on purpose rather than imported from an
older revision — it is the shape `data/copilot.duckdb` actually has, and a
test that re-derived it from today's `INIT_SCHEMA_STATEMENTS` would prove
nothing (`CREATE TABLE IF NOT EXISTS` is a no-op against an existing table,
which is exactly the trap `ALTER_SCHEMA_STATEMENTS` exists to cover).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import duckdb
import pytest

from swing_copilot.storage.database import Database
from swing_copilot.storage.retro_records import (
    RetroNarrationRecord,
    RetroSessionRecord,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.storage.verdict_records import backfill_verdict_reasons

if TYPE_CHECKING:
    from pathlib import Path

_LEGACY_DDL = (
    """
    CREATE TABLE runs (
        run_id          UUID PRIMARY KEY,
        run_date        DATE NOT NULL,
        mode            VARCHAR NOT NULL,
        config_hash     VARCHAR NOT NULL,
        status          VARCHAR NOT NULL,
        started_at      TIMESTAMPTZ NOT NULL,
        completed_at    TIMESTAMPTZ,
        report_path     VARCHAR,
        error_summary   VARCHAR
    )
    """,
    """
    CREATE TABLE candidates (
        run_id         UUID NOT NULL,
        symbol         VARCHAR NOT NULL,
        strategy_key   VARCHAR NOT NULL,
        rank           INTEGER NOT NULL,
        signal_names   VARCHAR[] NOT NULL,
        metrics_json   JSON NOT NULL,
        PRIMARY KEY (run_id, symbol, strategy_key)
    )
    """,
    # Issue #251's "before" shape for the truncated tail: created by Issue
    # #188 with the first four score components, without the three
    # strategy-specific ones. `CREATE TABLE IF NOT EXISTS` cannot widen it.
    """
    CREATE TABLE screening_truncations (
        run_id              UUID NOT NULL,
        symbol              VARCHAR NOT NULL,
        strategy_key        VARCHAR NOT NULL,
        rank                INTEGER NOT NULL,
        score               DOUBLE NOT NULL,
        score_rsi_pullback  DOUBLE,
        score_trend_quality DOUBLE,
        score_liquidity     DOUBLE,
        score_atr_pct       DOUBLE,
        execution_state     VARCHAR NOT NULL,
        execution_distance  DOUBLE,
        as_of               DATE NOT NULL,
        PRIMARY KEY (run_id, symbol, strategy_key)
    )
    """,
    """
    CREATE TABLE regime_snapshots (
        run_id          UUID PRIMARY KEY,
        as_of           DATE NOT NULL,
        gate_verdict    VARCHAR NOT NULL,
        dd_count_spy    DOUBLE NOT NULL,
        dd_count_qqq    DOUBLE NOT NULL,
        dd_level        VARCHAR NOT NULL,
        data_quality    VARCHAR NOT NULL,
        detail_json     JSON NOT NULL
    )
    """,
    """
    CREATE TABLE exposure_decisions (
        run_id       UUID PRIMARY KEY,
        verdict      VARCHAR NOT NULL,
        data_quality VARCHAR NOT NULL,
        detail_json  JSON NOT NULL
    )
    """,
    """
    CREATE TABLE verdicts (
        run_id         UUID NOT NULL,
        symbol         VARCHAR NOT NULL,
        as_of          DATE NOT NULL,
        strategy_key   VARCHAR NOT NULL,
        recommendation VARCHAR NOT NULL,
        reasons_json   JSON NOT NULL,
        no_trade       BOOLEAN NOT NULL,
        PRIMARY KEY (run_id, symbol)
    )
    """,
)

_RUN_ID = uuid4()

_METRICS = {
    "score": 0.62,
    "score_rsi_pullback": 0.30,
    "score_trend_quality": 0.20,
    "score_liquidity": 0.10,
    "score_atr_pct": 0.02,
    "rsi14": 41.0,
    "close": 100.0,
}

_REGIME_DETAIL = {
    "spy": {"d25": 3.0, "d15": 2.0, "d5": 1.0, "level": "NORMAL"},
    "qqq": {"d25": 4.0, "d15": 3.0, "d5": 2.0, "level": "CAUTION"},
    "gate_inputs": {"spy_close": 520.0, "spy_ema": 500.0, "vix_close": 15.0},
}

_EXPOSURE_DETAIL = {
    "gate": "BULL",
    "dd_level": "NORMAL",
    "data_quality": "OK",
    "conservatively_downgraded": False,
    "reduce_only_risk_multiplier": 0.5,
}

_REASONS = [
    {"text": "earnings beat", "source_ids": ["news-1", "news-2"], "basis": "filing"},
    {"text": "trend intact", "source_ids": [], "basis": None},
]


def _legacy_database(tmp_path: Path) -> Path:
    """Build a pre-Issue-#192 database holding one run's worth of rows."""
    path = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(path)) as conn:
        for statement in _LEGACY_DDL:
            conn.execute(statement)
        # Out of scope for Issue #398's `StateStore.insert_run()` migration
        # on purpose: this row is written against `_LEGACY_DDL`'s
        # pre-Issue-#192 `runs` shape (no `metadata_json` column, positional
        # values only), over a raw `duckdb.connect` -- not a `Database`/
        # `StateStore` at all -- so there is no post-migration schema for
        # `insert_run()` to write into yet. That is exactly the shape this
        # test module exists to exercise.
        conn.execute(
            "INSERT INTO runs VALUES (?, DATE '2026-07-21', 'live', 'cfg', "
            "'success', now(), NULL, NULL, NULL)",
            [str(_RUN_ID)],
        )
        conn.execute(
            "INSERT INTO candidates VALUES (?, 'AAPL', 'default', 1, ['rsi'], ?)",
            [str(_RUN_ID), json.dumps(_METRICS)],
        )
        conn.execute(
            "INSERT INTO screening_truncations VALUES (?, 'MSFT', 'default', 6, "
            "0.42, 0.20, 0.12, 0.08, 0.02, 'READY', 0.31, DATE '2026-07-21')",
            [str(_RUN_ID)],
        )
        conn.execute(
            "INSERT INTO regime_snapshots VALUES (?, DATE '2026-07-21', 'BULL', "
            "3.0, 4.0, 'NORMAL', 'OK', ?)",
            [str(_RUN_ID), json.dumps(_REGIME_DETAIL)],
        )
        conn.execute(
            "INSERT INTO exposure_decisions VALUES (?, 'FULL', 'OK', ?)",
            [str(_RUN_ID), json.dumps(_EXPOSURE_DETAIL)],
        )
        conn.execute(
            "INSERT INTO verdicts VALUES (?, 'AAPL', DATE '2026-07-21', 'default', "
            "'proceed', ?, FALSE)",
            [str(_RUN_ID), json.dumps(_REASONS)],
        )
    return path


def _migrated(tmp_path: Path) -> StateStore:
    store = StateStore(Database(_legacy_database(tmp_path)))
    store.init_schema()
    return store


class TestPromotedColumnBackfill:
    def test_candidate_scores_are_restated_from_metrics_json(
        self, tmp_path: Path
    ) -> None:
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            row = conn.execute(
                "SELECT score, score_rsi_pullback, score_trend_quality, "
                "score_liquidity, score_atr_pct FROM candidates"
            ).fetchone()

        assert row == (0.62, 0.30, 0.20, 0.10, 0.02)

    def test_strategy_specific_score_columns_are_added_but_not_backfilled(
        self, tmp_path: Path
    ) -> None:
        """Issue #251: NULL means "not recorded", never a measured 0.0.

        A row written before the components existed has them in neither a
        column nor `metrics_json`, so there is nothing to restate -- unlike
        the four columns above, whose values were merely in the wrong shape.
        """
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            candidate = conn.execute(
                "SELECT score_pivot_proximity, score_rs_percentile, "
                "score_criteria_met FROM candidates"
            ).fetchone()
            truncation = conn.execute(
                "SELECT score_pivot_proximity, score_rs_percentile, "
                "score_criteria_met FROM screening_truncations"
            ).fetchone()

        assert candidate == (None, None, None)
        assert truncation == (None, None, None)

    def test_the_migrated_truncation_keeps_its_recorded_components(
        self, tmp_path: Path
    ) -> None:
        # Widening the table must not disturb the rows already in it.
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            row = conn.execute(
                "SELECT symbol, rank, score, score_rsi_pullback, score_atr_pct "
                "FROM v_truncated_candidates"
            ).fetchone()

        assert row == ("MSFT", 6, 0.42, 0.20, 0.02)

    def test_candidate_execution_state_stays_null_for_pre_existing_rows(
        self, tmp_path: Path
    ) -> None:
        """The documented one-way cut: it was never persisted, so it is unknowable.

        NULL here must not be read as the `UNKNOWN` execution state, which is
        a measured "distance not computable".
        """
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            row = conn.execute(
                "SELECT execution_state, execution_distance FROM candidates"
            ).fetchone()

        assert row == (None, None)

    def test_regime_sub_windows_and_gate_inputs_are_restated(
        self, tmp_path: Path
    ) -> None:
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            row = conn.execute(
                "SELECT dd15_spy, dd5_spy, dd15_qqq, dd5_qqq, spy_close, spy_ema, "
                "vix_close, spy_sma200, spy_ftd_state FROM regime_snapshots"
            ).fetchone()

        assert row == (2.0, 1.0, 3.0, 2.0, 520.0, 500.0, 15.0, None, None)

    def test_exposure_inputs_are_restated(self, tmp_path: Path) -> None:
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            row = conn.execute(
                "SELECT gate_verdict, dd_level, is_conservatively_downgraded, "
                "reduce_only_risk_multiplier, spy_sma200, spy_ftd_state, ftd_active "
                "FROM exposure_decisions"
            ).fetchone()

        assert row == ("BULL", "NORMAL", False, 0.5, None, None, None)

    def test_verdict_reasons_are_normalized_out_of_reasons_json(
        self, tmp_path: Path
    ) -> None:
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            reasons = conn.execute(
                "SELECT reason_index, text, basis, source_id_count "
                "FROM verdict_reasons ORDER BY reason_index"
            ).fetchall()
            sources = conn.execute(
                "SELECT reason_index, source_id FROM verdict_reason_sources "
                "ORDER BY reason_index, source_id"
            ).fetchall()

        assert reasons == [
            (0, "earnings beat", "filing", 2),
            (1, "trend intact", None, 0),
        ]
        assert sources == [(0, "news-1"), (0, "news-2")]

    def test_rerunning_init_schema_neither_duplicates_nor_rewrites(
        self, tmp_path: Path
    ) -> None:
        """Every migration statement must be a no-op the second time."""
        store = _migrated(tmp_path)
        with store.database.connect() as conn:
            conn.execute("UPDATE candidates SET score = 99.0")

        store.init_schema()

        with store.database.connect() as conn:
            assert conn.execute("SELECT count(*) FROM verdict_reasons").fetchone() == (
                2,
            )
            assert conn.execute(
                "SELECT count(*) FROM verdict_reason_sources"
            ).fetchone() == (2,)
            # Not re-derived from `metrics_json`: the guard is "is it unset",
            # not "does it match the JSON".
            assert conn.execute("SELECT score FROM candidates").fetchone() == (99.0,)


class _FlakyReasonConnection:
    """Wraps a real connection; raises on the Nth `INSERT INTO verdict_reasons`."""

    def __init__(self, real_conn: duckdb.DuckDBPyConnection, fail_on_call: int) -> None:
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._insert_calls = 0

    def execute(
        self, sql: str, parameters: list[object] | None = None
    ) -> duckdb.DuckDBPyConnection:
        if sql.lstrip().startswith("INSERT INTO verdict_reasons"):
            self._insert_calls += 1
            if self._insert_calls == self._fail_on_call:
                msg = "simulated failure on a later reason insert"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)


class TestBackfillAtomicity:
    """A half-projected verdict would be skipped forever by the idempotence guard."""

    def test_a_failure_after_an_earlier_reason_rolls_the_backfill_back(
        self, tmp_path: Path
    ) -> None:
        store = _migrated(tmp_path)
        with store.database.connect() as conn:
            conn.execute("DELETE FROM verdict_reasons")
            conn.execute("DELETE FROM verdict_reason_sources")

            with pytest.raises(RuntimeError, match="simulated failure"):
                backfill_verdict_reasons(
                    cast(
                        "duckdb.DuckDBPyConnection",
                        _FlakyReasonConnection(conn, fail_on_call=2),
                    )
                )

            assert conn.execute("SELECT count(*) FROM verdict_reasons").fetchone() == (
                0,
            )

    def test_the_next_start_completes_the_backfill_it_rolled_back(
        self, tmp_path: Path
    ) -> None:
        store = _migrated(tmp_path)
        with store.database.connect() as conn:
            conn.execute("DELETE FROM verdict_reasons")
            conn.execute("DELETE FROM verdict_reason_sources")
            with pytest.raises(RuntimeError, match="simulated failure"):
                backfill_verdict_reasons(
                    cast(
                        "duckdb.DuckDBPyConnection",
                        _FlakyReasonConnection(conn, fail_on_call=1),
                    )
                )

        store.init_schema()

        with store.database.connect() as conn:
            assert conn.execute("SELECT count(*) FROM verdict_reasons").fetchone() == (
                2,
            )


class TestIssue189LedgersOnAnExistingDatabase:
    """New tables, not new columns: `CREATE TABLE IF NOT EXISTS` is the migration.

    They start empty on the production file because the history they would
    hold (`failure_class`, what a `config_hash` stood for) was never written
    anywhere -- which is precisely why the tables had to exist before more
    days accumulate.
    """

    def test_the_new_tables_exist_and_start_empty(self, tmp_path: Path) -> None:
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM retro_sessions), "
                "(SELECT count(*) FROM retro_narrations), "
                "(SELECT count(*) FROM config_versions)"
            ).fetchone()

        assert counts == (0, 0, 0)

    def test_an_existing_run_reads_as_configuration_not_recorded(
        self, tmp_path: Path
    ) -> None:
        """NULL means "never written down", never "the configuration was empty"."""
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            row = conn.execute(
                "SELECT config_hash, snapshot_hash, sections_json FROM v_run_configs"
            ).fetchone()

        assert row == ("cfg", None, None)

    def test_a_narration_recorded_after_the_migration_joins_its_existing_verdict(
        self, tmp_path: Path
    ) -> None:
        store = _migrated(tmp_path)
        as_of = date(2026, 8, 18)
        store.replace_retro_session(
            RetroSessionRecord(
                retro_as_of=as_of,
                window_start=date(2026, 7, 21),
                input_digest="d" * 64,
                generated_at=datetime(2026, 8, 18, tzinfo=UTC),
                outcome_count=4,
                proposal_count=0,
            ),
            [
                RetroNarrationRecord(
                    retro_as_of=as_of,
                    surprise_id="s-1",
                    run_id=_RUN_ID,
                    symbol="AAPL",
                    failure_class="information_absent",
                    narrative="当時の入力に材料が無かった",
                    evidence_refs=("s-1",),
                )
            ],
        )

        with store.database.connect() as conn:
            row = conn.execute(
                "SELECT symbol, failure_class, run_date, recommendation "
                "FROM v_retro_narrations"
            ).fetchone()

        assert row == ("AAPL", "information_absent", date(2026, 7, 21), "proceed")


class TestMigratedRowsStayReadable:
    def test_v_candidates_falls_back_to_json_when_columns_are_unset(
        self, tmp_path: Path
    ) -> None:
        """The DoD's COALESCE contract, proven with the columns forced back to NULL.

        A `read_only=True` research connection cannot run DDL, so it can meet
        a file whose migration has not been applied yet.
        """
        store = _migrated(tmp_path)
        with store.database.connect() as conn:
            conn.execute("UPDATE candidates SET score = NULL, score_atr_pct = NULL")

            row = conn.execute(
                "SELECT score, score_atr_pct, execution_state FROM v_candidates"
            ).fetchone()

        assert row == (0.62, 0.02, None)

    def test_v_verdict_reasons_joins_the_backfilled_rows_to_their_verdict(
        self, tmp_path: Path
    ) -> None:
        store = _migrated(tmp_path)

        with store.database.connect() as conn:
            rows = conn.execute(
                "SELECT symbol, recommendation, reason_index, source_id_count "
                "FROM v_verdict_reasons ORDER BY reason_index"
            ).fetchall()

        assert rows == [("AAPL", "proceed", 0, 2), ("AAPL", "proceed", 1, 0)]
