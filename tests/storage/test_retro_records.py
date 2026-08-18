"""Issue #189: the retrospective narration ledger (design §8.1's L2 gate).

The contracts under test are the storage invariants this table has to hold to
be worth anything: one retrospective is one transaction, a re-ingest replaces
the date's reading rather than merging into it, and the trailing cross-tab is
point-in-time so an old retrospective re-exported today reproduces the number
it saw then.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from swing_copilot.storage.database import Database
from swing_copilot.storage.retro_records import (
    FailureClassHistory,
    RetroNarrationRecord,
    RetroSessionRecord,
    replace_retro_session,
)

if TYPE_CHECKING:
    import duckdb

    from swing_copilot.storage.state_store import StateStore

_RUN_ID = uuid4()
_GENERATED_AT = datetime(2027, 3, 29, 12, tzinfo=UTC)


def _session(as_of: date, proposal_count: int = 1) -> RetroSessionRecord:
    return RetroSessionRecord(
        retro_as_of=as_of,
        window_start=date(2026, 12, 29),
        input_digest="a" * 64,
        generated_at=_GENERATED_AT,
        outcome_count=12,
        proposal_count=proposal_count,
    )


def _narration(
    as_of: date, surprise_id: str, failure_class: str
) -> RetroNarrationRecord:
    return RetroNarrationRecord(
        retro_as_of=as_of,
        surprise_id=surprise_id,
        run_id=_RUN_ID,
        symbol="AAPL",
        failure_class=failure_class,
        narrative="当時の入力に材料が無かった",
        evidence_refs=(surprise_id, "finnhub:1"),
    )


class TestReplaceRetroSession:
    def test_records_the_session_and_its_narrations(
        self, state_store: StateStore
    ) -> None:
        as_of = date(2027, 3, 29)

        state_store.replace_retro_session(
            _session(as_of), [_narration(as_of, "s-1", "information_absent")]
        )

        rows = state_store.get_retro_narrations(as_of)
        assert [(row.symbol, row.failure_class, row.evidence_refs) for row in rows] == [
            ("AAPL", "information_absent", ("s-1", "finnhub:1"))
        ]

    def test_a_re_ingest_drops_a_symbol_the_correction_no_longer_names(
        self, state_store: StateStore
    ) -> None:
        """Snapshot replacement: a stale reading must not survive a correction."""
        as_of = date(2027, 3, 29)
        state_store.replace_retro_session(
            _session(as_of),
            [
                _narration(as_of, "s-1", "information_absent"),
                _narration(as_of, "s-2", "exogenous"),
            ],
        )

        state_store.replace_retro_session(
            _session(as_of), [_narration(as_of, "s-2", "interpretation_error")]
        )

        rows = state_store.get_retro_narrations(as_of)
        assert [(row.surprise_id, row.failure_class) for row in rows] == [
            ("s-2", "interpretation_error")
        ]

    def test_a_re_ingest_corrects_the_session_row_in_place(
        self, state_store: StateStore
    ) -> None:
        as_of = date(2027, 3, 29)
        state_store.replace_retro_session(_session(as_of), [])

        state_store.replace_retro_session(_session(as_of, proposal_count=4), [])

        with state_store.database.connect() as conn:
            assert conn.execute(
                "SELECT count(*), max(proposal_count) FROM retro_sessions"
            ).fetchone() == (1, 4)

    def test_an_empty_narration_set_still_clears_the_previous_reading(
        self, state_store: StateStore
    ) -> None:
        """A retrospective that verified nothing is a fact, not a no-op."""
        as_of = date(2027, 3, 29)
        state_store.replace_retro_session(
            _session(as_of), [_narration(as_of, "s-1", "exogenous")]
        )

        state_store.replace_retro_session(_session(as_of), [])

        assert state_store.get_retro_narrations(as_of) == ()

    def test_a_narration_from_another_session_is_rejected_before_any_write(
        self, state_store: StateStore
    ) -> None:
        as_of = date(2027, 3, 29)

        with pytest.raises(ValueError, match="retro_as_of"):
            state_store.replace_retro_session(
                _session(as_of), [_narration(date(2027, 2, 26), "s-1", "exogenous")]
            )

        with state_store.database.connect() as conn:
            assert conn.execute("SELECT count(*) FROM retro_sessions").fetchone() == (
                0,
            )


class _FlakyConnection:
    """Wraps a real connection; raises on the Nth narration insert."""

    def __init__(self, real_conn: duckdb.DuckDBPyConnection, fail_on_call: int) -> None:
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._insert_calls = 0

    def execute(
        self, sql: str, parameters: list[object] | None = None
    ) -> duckdb.DuckDBPyConnection:
        if sql.lstrip().startswith("INSERT INTO retro_narrations"):
            self._insert_calls += 1
            if self._insert_calls == self._fail_on_call:
                msg = "simulated failure on a later narration insert"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __enter__(self) -> _FlakyConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._real.close()


class _FlakyDatabase(Database):
    """A `Database` whose connections fail partway through the narration loop."""

    def __init__(self, real: Database, fail_on_call: int) -> None:
        super().__init__(real.db_path)
        self._fail_on_call = fail_on_call

    def connect(self) -> duckdb.DuckDBPyConnection:
        return cast(
            "duckdb.DuckDBPyConnection",
            _FlakyConnection(super().connect(), self._fail_on_call),
        )


class TestAtomicity:
    def test_a_failure_after_an_earlier_narration_rolls_the_whole_session_back(
        self, state_store: StateStore
    ) -> None:
        as_of = date(2027, 3, 29)

        with pytest.raises(RuntimeError, match="simulated failure"):
            replace_retro_session(
                _FlakyDatabase(state_store.database, fail_on_call=2),
                _session(as_of),
                [
                    _narration(as_of, "s-1", "information_absent"),
                    _narration(as_of, "s-2", "exogenous"),
                ],
            )

        with state_store.database.connect() as conn:
            assert conn.execute("SELECT count(*) FROM retro_sessions").fetchone() == (
                0,
            )
            assert conn.execute("SELECT count(*) FROM retro_narrations").fetchone() == (
                0,
            )

    def test_the_next_ingest_completes_what_the_rollback_undid(
        self, state_store: StateStore
    ) -> None:
        as_of = date(2027, 3, 29)
        with pytest.raises(RuntimeError, match="simulated failure"):
            replace_retro_session(
                _FlakyDatabase(state_store.database, fail_on_call=1),
                _session(as_of),
                [_narration(as_of, "s-1", "information_absent")],
            )

        state_store.replace_retro_session(
            _session(as_of), [_narration(as_of, "s-1", "information_absent")]
        )

        assert len(state_store.get_retro_narrations(as_of)) == 1


def _ingest(store: StateStore, as_of: date, classes: tuple[str, ...]) -> None:
    store.replace_retro_session(
        _session(as_of),
        [
            _narration(as_of, f"{as_of.isoformat()}-{index}", failure_class)
            for index, failure_class in enumerate(classes)
        ],
    )


class TestFailureClassHistory:
    def test_counts_across_the_trailing_sessions(self, state_store: StateStore) -> None:
        _ingest(state_store, date(2027, 1, 25), ("information_absent",))
        _ingest(state_store, date(2027, 2, 22), ("information_absent", "exogenous"))

        history = state_store.get_failure_class_history(date(2027, 3, 29), 3)

        assert history.sessions == (date(2027, 2, 22), date(2027, 1, 25))
        assert [
            (row.failure_class, row.count, row.session_count) for row in history.counts
        ] == [("information_absent", 2, 2), ("exogenous", 1, 1)]

    def test_only_the_most_recent_sessions_are_counted(
        self, state_store: StateStore
    ) -> None:
        """The gate reads "the last three retrospectives", not all of history."""
        for month in (1, 2, 3, 4):
            _ingest(state_store, date(2027, month, 25), ("exogenous",))

        history = state_store.get_failure_class_history(date(2027, 4, 30), 3)

        assert len(history.sessions) == 3
        assert [row.count for row in history.counts] == [3]

    @pytest.mark.parametrize(
        ("as_of", "expected_sessions"),
        [
            pytest.param(date(2027, 2, 21), 1, id="before"),
            pytest.param(date(2027, 2, 22), 2, id="exactly-at"),
            pytest.param(date(2027, 2, 23), 2, id="after"),
        ],
    )
    def test_the_as_of_boundary_is_inclusive(
        self, state_store: StateStore, as_of: date, expected_sessions: int
    ) -> None:
        _ingest(state_store, date(2027, 1, 25), ("exogenous",))
        _ingest(state_store, date(2027, 2, 22), ("exogenous",))

        history = state_store.get_failure_class_history(as_of, 3)

        assert len(history.sessions) == expected_sessions

    def test_an_empty_ledger_reports_no_sessions(self, state_store: StateStore) -> None:
        history = state_store.get_failure_class_history(date(2027, 3, 29), 3)

        assert history == FailureClassHistory(sessions=(), counts=())
