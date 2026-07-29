"""P8-30: `verdicts` / `verdict_sources` / `verdict_outcomes` write contracts.

Both writers are full replacements in a single transaction (design.md §4):
a natural-key rerun must pick up corrections *and* drop rows that are absent
from the replacement, and a failure after at least one successful statement
must leave the previous state intact.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import duckdb
import pytest

from swing_copilot.storage.verdict_records import (
    VerdictOutcomeRecord,
    VerdictReasonRecord,
    VerdictRecord,
    VerdictSourceRecord,
)

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore

AS_OF = date(2026, 7, 20)


def _verdict(
    run_id: UUID,
    symbol: str,
    recommendation: str = "proceed",
    *,
    as_of: date = AS_OF,
    reasons: tuple[VerdictReasonRecord, ...] = (),
) -> VerdictRecord:
    return VerdictRecord(
        run_id=run_id,
        symbol=symbol,
        as_of=as_of,
        strategy_key="default",
        recommendation=recommendation,
        reasons=reasons,
        no_trade=False,
    )


def _source(
    run_id: UUID, symbol: str, source_id: str, source_type: str = "news"
) -> VerdictSourceRecord:
    return VerdictSourceRecord(
        run_id=run_id, symbol=symbol, source_id=source_id, source_type=source_type
    )


def _outcome(
    run_id: UUID,
    symbol: str,
    horizon_days: int = 5,
    *,
    forward_return_pct: float = 1.5,
    classification: str = "HIT",
) -> VerdictOutcomeRecord:
    return VerdictOutcomeRecord(
        run_id=run_id,
        symbol=symbol,
        horizon_days=horizon_days,
        as_of=AS_OF,
        recommendation="proceed",
        forward_return_pct=forward_return_pct,
        classification=classification,
    )


def _rows(
    state_store: StateStore, sql: str, parameters: list[object] | None = None
) -> list[tuple[object, ...]]:
    with state_store._database.connect() as conn:  # noqa: SLF001
        return conn.execute(sql, parameters or []).fetchall()


class TestReplaceRunVerdicts:
    def test_persists_verdicts_and_their_cited_sources(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        state_store.replace_run_verdicts(
            run_id,
            [
                _verdict(
                    run_id,
                    "AAPL",
                    reasons=(
                        VerdictReasonRecord(text="堅調な受注", source_ids=("news-1",)),
                    ),
                )
            ],
            [_source(run_id, "AAPL", "news-1")],
        )

        rows = _rows(
            state_store,
            "SELECT symbol, strategy_key, as_of, recommendation, no_trade, "
            "reasons_json FROM verdicts",
        )
        assert [row[:5] for row in rows] == [
            ("AAPL", "default", AS_OF, "proceed", False)
        ]
        assert json.loads(str(rows[0][5])) == [
            {"text": "堅調な受注", "source_ids": ["news-1"]}
        ]
        assert _rows(
            state_store,
            "SELECT symbol, source_id, source_type FROM verdict_sources",
        ) == [("AAPL", "news-1", "news")]

    def test_persists_the_run_level_no_trade_flag(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        state_store.replace_run_verdicts(
            run_id,
            [
                VerdictRecord(
                    run_id=run_id,
                    symbol="AAPL",
                    as_of=AS_OF,
                    strategy_key="default",
                    recommendation="skip",
                    reasons=(),
                    no_trade=True,
                )
            ],
            [],
        )

        assert _rows(state_store, "SELECT no_trade FROM verdicts") == [(True,)]

    def test_rerun_replaces_corrected_rows_without_duplicating(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL", "proceed")],
            [_source(run_id, "AAPL", "news-1")],
        )

        # The skill's answer was re-ingested with a corrected recommendation.
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL", "skip")],
            [_source(run_id, "AAPL", "news-1")],
        )

        assert _rows(state_store, "SELECT symbol, recommendation FROM verdicts") == [
            ("AAPL", "skip")
        ]
        assert _rows(state_store, "SELECT count(*) FROM verdict_sources") == [(1,)]

    def test_replacement_drops_rows_absent_from_the_new_set(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL"), _verdict(run_id, "MSFT")],
            [_source(run_id, "AAPL", "news-1"), _source(run_id, "MSFT", "news-2")],
        )

        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL")], [_source(run_id, "AAPL", "news-1")]
        )

        assert _rows(state_store, "SELECT symbol FROM verdicts") == [("AAPL",)]
        assert _rows(state_store, "SELECT symbol FROM verdict_sources") == [("AAPL",)]

    def test_an_empty_replacement_clears_the_run(self, state_store: StateStore) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL")], [_source(run_id, "AAPL", "news-1")]
        )

        state_store.replace_run_verdicts(run_id, [], [])

        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]
        assert _rows(state_store, "SELECT count(*) FROM verdict_sources") == [(0,)]

    def test_other_runs_are_untouched(self, state_store: StateStore) -> None:
        kept, replaced = uuid4(), uuid4()
        state_store.replace_run_verdicts(kept, [_verdict(kept, "AAPL")], [])
        state_store.replace_run_verdicts(replaced, [_verdict(replaced, "MSFT")], [])

        state_store.replace_run_verdicts(replaced, [], [])

        assert _rows(state_store, "SELECT symbol FROM verdicts") == [("AAPL",)]

    def test_rejects_records_belonging_to_another_run(
        self, state_store: StateStore
    ) -> None:
        run_id, other_run_id = uuid4(), uuid4()

        with pytest.raises(ValueError, match="must match the replacement run_id"):
            state_store.replace_run_verdicts(
                run_id, [_verdict(other_run_id, "AAPL")], []
            )

    def test_rejects_sources_belonging_to_another_run(
        self, state_store: StateStore
    ) -> None:
        run_id, other_run_id = uuid4(), uuid4()

        with pytest.raises(ValueError, match="must match the replacement run_id"):
            state_store.replace_run_verdicts(
                run_id, [], [_source(other_run_id, "AAPL", "news-1")]
            )

    def test_rejects_a_recommendation_outside_the_schema_check(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        with pytest.raises(duckdb.ConstraintException):
            state_store.replace_run_verdicts(
                run_id, [_verdict(run_id, "AAPL", "maybe")], []
            )

    def test_rejects_a_source_type_outside_the_schema_check(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        with pytest.raises(duckdb.ConstraintException):
            state_store.replace_run_verdicts(
                run_id, [], [_source(run_id, "AAPL", "x-1", "rumor")]
            )


class _FlakyVerdictConnection:
    """Wraps a real connection; raises on the Nth `INSERT INTO <table>`.

    Lets the rollback tests inject a failure *after* at least one row of the
    same logical write has already been inserted inside the transaction.
    """

    def __init__(
        self, real_conn: duckdb.DuckDBPyConnection, table: str, fail_on_call: int
    ):
        self._real = real_conn
        self._prefix = f"INSERT INTO {table}"
        self._fail_on_call = fail_on_call
        self._insert_calls = 0

    def execute(self, sql, parameters=None):
        if sql.lstrip().startswith(self._prefix):
            self._insert_calls += 1
            if self._insert_calls == self._fail_on_call:
                msg = "simulated failure on a later verdict insert"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


def _inject_failure(
    state_store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    fail_on_call: int,
) -> None:
    real_connect = state_store._database.connect  # noqa: SLF001
    monkeypatch.setattr(
        state_store._database,  # noqa: SLF001
        "connect",
        lambda: _FlakyVerdictConnection(real_connect(), table, fail_on_call),
    )


class TestReplaceRunVerdictsAtomicity:
    def test_a_failure_after_an_earlier_insert_rolls_the_whole_write_back(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        _inject_failure(state_store, monkeypatch, "verdicts", fail_on_call=2)

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_run_verdicts(
                run_id, [_verdict(run_id, "AAPL"), _verdict(run_id, "MSFT")], []
            )

        monkeypatch.undo()
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]

    def test_a_failure_preserves_the_previously_committed_run(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL", "proceed")],
            [_source(run_id, "AAPL", "news-1")],
        )
        _inject_failure(state_store, monkeypatch, "verdicts", fail_on_call=1)

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_run_verdicts(
                run_id, [_verdict(run_id, "AAPL", "skip")], []
            )

        monkeypatch.undo()
        # The DELETE ran before the failing INSERT: rollback must restore it.
        assert _rows(state_store, "SELECT symbol, recommendation FROM verdicts") == [
            ("AAPL", "proceed")
        ]
        assert _rows(state_store, "SELECT count(*) FROM verdict_sources") == [(1,)]

    def test_a_source_insert_failure_also_rolls_the_verdicts_back(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        _inject_failure(state_store, monkeypatch, "verdict_sources", fail_on_call=2)

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_run_verdicts(
                run_id,
                [_verdict(run_id, "AAPL")],
                [_source(run_id, "AAPL", "news-1"), _source(run_id, "AAPL", "news-2")],
            )

        monkeypatch.undo()
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]
        assert _rows(state_store, "SELECT count(*) FROM verdict_sources") == [(0,)]

    def test_a_rerun_after_a_failure_succeeds(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        _inject_failure(state_store, monkeypatch, "verdicts", fail_on_call=1)
        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_run_verdicts(run_id, [_verdict(run_id, "AAPL")], [])
        monkeypatch.undo()

        state_store.replace_run_verdicts(run_id, [_verdict(run_id, "AAPL")], [])

        assert _rows(state_store, "SELECT symbol FROM verdicts") == [("AAPL",)]


class TestReplaceVerdictOutcomes:
    def test_persists_one_row_per_symbol_and_horizon(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL")])

        assert _rows(
            state_store,
            "SELECT symbol, horizon_days, as_of, recommendation, "
            "forward_return_pct, classification FROM verdict_outcomes",
        ) == [("AAPL", 5, AS_OF, "proceed", 1.5, "HIT")]

    def test_rerun_with_corrected_prices_updates_rather_than_duplicates(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL")])

        state_store.replace_verdict_outcomes(
            run_id,
            5,
            [
                _outcome(
                    run_id,
                    "AAPL",
                    forward_return_pct=-5.0,
                    classification="MISS_SEVERE",
                )
            ],
        )

        assert _rows(
            state_store,
            "SELECT forward_return_pct, classification FROM verdict_outcomes",
        ) == [(-5.0, "MISS_SEVERE")]

    def test_replacement_is_scoped_to_one_horizon(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL", 5)])
        state_store.replace_verdict_outcomes(run_id, 20, [_outcome(run_id, "AAPL", 20)])

        state_store.replace_verdict_outcomes(run_id, 5, [])

        assert _rows(state_store, "SELECT horizon_days FROM verdict_outcomes") == [
            (20,)
        ]

    def test_rejects_records_from_another_run_or_horizon(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        with pytest.raises(ValueError, match="must match the replacement"):
            state_store.replace_verdict_outcomes(
                run_id, 5, [_outcome(run_id, "AAPL", 20)]
            )

    def test_rejects_a_classification_outside_the_schema_check(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        with pytest.raises(duckdb.ConstraintException):
            state_store.replace_verdict_outcomes(
                run_id, 5, [_outcome(run_id, "AAPL", classification="MAYBE")]
            )

    def test_a_failure_after_an_earlier_insert_rolls_the_whole_write_back(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "KEPT")])
        _inject_failure(state_store, monkeypatch, "verdict_outcomes", fail_on_call=2)

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_verdict_outcomes(
                run_id, 5, [_outcome(run_id, "AAPL"), _outcome(run_id, "MSFT")]
            )

        monkeypatch.undo()
        assert _rows(state_store, "SELECT symbol FROM verdict_outcomes") == [("KEPT",)]


class TestGetVerdictsInWindow:
    def test_returns_rows_inside_the_inclusive_window_only(
        self, state_store: StateStore
    ) -> None:
        before, start, inside, end, after = (
            date(2026, 7, 9),
            date(2026, 7, 10),
            date(2026, 7, 15),
            date(2026, 7, 20),
            date(2026, 7, 21),
        )
        for index, run_date in enumerate((before, start, inside, end, after)):
            run_id = uuid4()
            state_store.replace_run_verdicts(
                run_id, [_verdict(run_id, f"S{index}", as_of=run_date)], []
            )

        rows = state_store.get_verdicts_in_window(start, end)

        assert [(row.symbol, row.as_of) for row in rows] == [
            ("S1", start),
            ("S2", inside),
            ("S3", end),
        ]

    def test_carries_the_recommendation_needed_for_classification(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(run_id, [_verdict(run_id, "AAPL", "skip")], [])

        rows = state_store.get_verdicts_in_window(AS_OF, AS_OF)

        assert [(row.run_id, row.symbol, row.recommendation) for row in rows] == [
            (run_id, "AAPL", "skip")
        ]

    def test_returns_nothing_for_an_empty_database(
        self, state_store: StateStore
    ) -> None:
        assert state_store.get_verdicts_in_window(AS_OF, AS_OF) == ()
