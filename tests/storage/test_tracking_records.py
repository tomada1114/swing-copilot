"""DuckDB contracts for the verdict-tracking ledger.

Correction upsert, whole-advance rollback after an earlier statement already
succeeded, and rerun after that failure -- the three storage guarantees
`tracking/update.py` relies on.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import duckdb
import pytest

from swing_copilot.storage.tracking_records import (
    VerdictPosition,
    VerdictPositionMark,
    VerdictPositionNote,
)
from swing_copilot.storage.verdict_records import VerdictReasonRecord, VerdictRecord

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore

RUN_ID = UUID("44444444-4444-4444-4444-444444444444")
SYMBOL = "AAA"
ENTRY_DATE = date(2027, 3, 20)
DAY_1 = date(2027, 3, 21)


def _position(**overrides: object) -> VerdictPosition:
    base = VerdictPosition(
        run_id=RUN_ID,
        symbol=SYMBOL,
        strategy_key="default",
        recommendation="proceed",
        no_trade=False,
        entry_date=ENTRY_DATE,
        entry_price=100.0,
        stop_price=95.0,
        days_held=0,
        status="open",
        last_marked_date=ENTRY_DATE,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _mark(as_of_date: date, close: float, **overrides: object) -> VerdictPositionMark:
    base = VerdictPositionMark(
        run_id=RUN_ID,
        symbol=SYMBOL,
        as_of_date=as_of_date,
        close=close,
        stop_price=95.0,
        unrealized_return_pct=(close - 100.0),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _seed_verdict(
    state_store: StateStore, *, recommendation: str = "proceed", no_trade: bool = False
) -> None:
    state_store.replace_run_verdicts(
        RUN_ID,
        [
            VerdictRecord(
                run_id=RUN_ID,
                symbol=SYMBOL,
                as_of=ENTRY_DATE,
                strategy_key="default",
                recommendation=recommendation,
                reasons=(VerdictReasonRecord(text="出来高が伴う", source_ids=()),),
                no_trade=no_trade,
            )
        ],
        [],
    )


def _seed_verdicts(state_store: StateStore, recommendations: tuple[str, str]) -> None:
    """Archive one run holding `SYMBOL` and `BBB`, each on the given side."""
    state_store.replace_run_verdicts(
        RUN_ID,
        [
            VerdictRecord(
                run_id=RUN_ID,
                symbol=symbol,
                as_of=ENTRY_DATE,
                strategy_key="default",
                recommendation=recommendation,
                reasons=(),
                no_trade=False,
            )
            for symbol, recommendation in zip(
                (SYMBOL, "BBB"), recommendations, strict=True
            )
        ],
        [],
    )


class _FlakyConnection:
    """Delegates to a real connection, raising on the `fail_on`-th `execute`."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, fail_on: int) -> None:
        self._conn = conn
        self._fail_on = fail_on
        self._calls = 0

    def execute(
        self, query: str, parameters: list[object] | None = None
    ) -> duckdb.DuckDBPyConnection:
        self._calls += 1
        if self._calls == self._fail_on:
            msg = "injected failure after an earlier delete succeeded"
            raise RuntimeError(msg)
        if parameters is None:
            return self._conn.execute(query)
        return self._conn.execute(query, parameters)

    def close(self) -> None:
        self._conn.close()


class TestPositionWrites:
    def test_reading_back_a_position_returns_every_stored_field(
        self, state_store: StateStore
    ) -> None:
        position = _position(
            status="closed",
            days_held=3,
            exit_date=DAY_1,
            exit_price=94.0,
            exit_reason="stop",
            realized_return_pct=-6.0,
            last_marked_date=DAY_1,
        )

        state_store.upsert_verdict_position(position)

        assert state_store.get_verdict_position(RUN_ID, SYMBOL) == position

    def test_an_unknown_position_reads_back_as_none(
        self, state_store: StateStore
    ) -> None:
        assert state_store.get_verdict_position(uuid4(), SYMBOL) is None

    def test_advancing_a_position_corrects_the_row_instead_of_duplicating_it(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_verdict_position(_position(), [_mark(ENTRY_DATE, 100.0)])
        advanced = _position(days_held=1, stop_price=96.0, last_marked_date=DAY_1)

        state_store.upsert_verdict_position(advanced, [_mark(DAY_1, 102.0)])

        assert state_store.get_verdict_positions() == (advanced,)
        assert [
            mark.as_of_date
            for mark in state_store.get_verdict_position_marks(RUN_ID, SYMBOL)
        ] == [ENTRY_DATE, DAY_1]

    def test_a_corrected_bar_overwrites_the_stored_mark_for_that_day(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_verdict_position(_position(), [_mark(DAY_1, 102.0)])
        corrected = _mark(DAY_1, 103.5, stop_price=96.0)

        state_store.upsert_verdict_position(_position(), [corrected])

        assert state_store.get_verdict_position_marks(RUN_ID, SYMBOL) == (corrected,)

    def test_a_mark_belonging_to_another_position_is_rejected(
        self, state_store: StateStore
    ) -> None:
        foreign = replace(_mark(DAY_1, 102.0), symbol="ZZZ")

        with pytest.raises(ValueError, match="must belong to the position"):
            state_store.upsert_verdict_position(_position(), [foreign])

    def test_status_filtering_returns_only_the_requested_side(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_verdict_position(_position())
        closed = _position(
            symbol="BBB", status="closed", exit_date=DAY_1, exit_reason="manual"
        )
        state_store.upsert_verdict_position(closed)

        assert state_store.get_verdict_positions("open") == (_position(),)
        assert state_store.get_verdict_positions("closed") == (closed,)
        assert len(state_store.get_verdict_positions()) == 2


class TestAdvanceAtomicity:
    def test_an_advance_rolls_back_after_an_earlier_mark_already_succeeded(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_verdict_position(_position(), [_mark(ENTRY_DATE, 100.0)])
        good = _mark(DAY_1, 102.0)
        broken = replace(
            _mark(date(2027, 3, 22), 104.0),
            unrealized_return_pct=cast("float", None),
        )
        advanced = _position(days_held=2, last_marked_date=date(2027, 3, 22))

        with pytest.raises(duckdb.ConstraintException, match="NOT NULL"):
            state_store.upsert_verdict_position(advanced, [good, broken])

        # Neither the position row nor the first mark of the failed advance
        # survives: the ledger must never claim a day it did not record.
        assert state_store.get_verdict_position(RUN_ID, SYMBOL) == _position()
        assert [
            mark.as_of_date
            for mark in state_store.get_verdict_position_marks(RUN_ID, SYMBOL)
        ] == [ENTRY_DATE]

    def test_the_same_advance_succeeds_once_rerun_with_valid_rows(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_verdict_position(_position(), [_mark(ENTRY_DATE, 100.0)])
        broken = replace(_mark(DAY_1, 102.0), unrealized_return_pct=cast("float", None))
        with pytest.raises(duckdb.ConstraintException):
            state_store.upsert_verdict_position(_position(days_held=1), [broken])

        advanced = _position(days_held=1, last_marked_date=DAY_1)
        state_store.upsert_verdict_position(advanced, [_mark(DAY_1, 102.0)])

        assert state_store.get_verdict_position(RUN_ID, SYMBOL) == advanced
        assert len(state_store.get_verdict_position_marks(RUN_ID, SYMBOL)) == 2


class TestNotes:
    def test_a_second_note_on_the_same_day_corrects_the_first(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_verdict_position_note(
            VerdictPositionNote(RUN_ID, SYMBOL, DAY_1, "様子見")
        )
        corrected = VerdictPositionNote(RUN_ID, SYMBOL, DAY_1, "利確を検討")

        state_store.upsert_verdict_position_note(corrected)

        assert state_store.get_verdict_position_notes(RUN_ID, SYMBOL) == (corrected,)

    def test_notes_come_back_in_date_order(self, state_store: StateStore) -> None:
        later = VerdictPositionNote(RUN_ID, SYMBOL, DAY_1, "後")
        earlier = VerdictPositionNote(RUN_ID, SYMBOL, ENTRY_DATE, "先")
        state_store.upsert_verdict_position_note(later)
        state_store.upsert_verdict_position_note(earlier)

        assert state_store.get_verdict_position_notes(RUN_ID, SYMBOL) == (
            earlier,
            later,
        )


class TestLatestMarks:
    def test_only_the_newest_mark_per_position_is_returned(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_verdict_position(
            _position(), [_mark(ENTRY_DATE, 100.0), _mark(DAY_1, 102.0)]
        )

        latest = state_store.get_latest_verdict_position_marks()

        assert latest[(RUN_ID, SYMBOL)].as_of_date == DAY_1

    def test_an_empty_ledger_yields_no_marks(self, state_store: StateStore) -> None:
        assert state_store.get_latest_verdict_position_marks() == {}


class TestUntrackedVerdicts:
    def test_a_proceed_verdict_is_listed_with_its_risk_prices(
        self, state_store: StateStore
    ) -> None:
        _seed_verdict(state_store)
        with state_store.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO risk_assessments (
                    run_id, symbol, status, max_shares, entry_price, stop_price,
                    reasons_json, warnings_json, sizing_warnings_json
                ) VALUES (?, ?, 'approved', 10, 100.0, 95.0, '[]', '[]', '[]')
                """,
                [str(RUN_ID), SYMBOL],
            )

        rows = state_store.get_untracked_verdicts(ENTRY_DATE)

        assert [(row.symbol, row.entry_price, row.stop_price) for row in rows] == [
            (SYMBOL, 100.0, 95.0)
        ]

    def test_a_verdict_without_a_risk_row_is_still_listed_with_null_prices(
        self, state_store: StateStore
    ) -> None:
        _seed_verdict(state_store)

        rows = state_store.get_untracked_verdicts(ENTRY_DATE)

        assert [(row.entry_price, row.stop_price) for row in rows] == [(None, None)]

    def test_a_no_trade_proceed_verdict_is_listed_with_the_flag_set(
        self, state_store: StateStore
    ) -> None:
        _seed_verdict(state_store, no_trade=True)

        rows = state_store.get_untracked_verdicts(ENTRY_DATE)

        assert [(row.symbol, row.no_trade) for row in rows] == [(SYMBOL, True)]

    def test_an_already_tracked_verdict_disappears_from_the_list(
        self, state_store: StateStore
    ) -> None:
        _seed_verdict(state_store)
        state_store.upsert_verdict_position(_position())

        assert state_store.get_untracked_verdicts(ENTRY_DATE) == ()

    def test_a_skip_verdict_is_listed_and_carries_its_side(
        self, state_store: StateStore
    ) -> None:
        # Issue #190: skip is shadow-tracked, so it must reach the ledger with
        # its own side recorded rather than being filtered out here.
        _seed_verdict(state_store, recommendation="skip")

        rows = state_store.get_untracked_verdicts(ENTRY_DATE)

        assert [(row.symbol, row.recommendation) for row in rows] == [(SYMBOL, "skip")]

    def test_a_side_left_out_of_the_request_is_not_listed(
        self, state_store: StateStore
    ) -> None:
        _seed_verdict(state_store, recommendation="skip")

        assert state_store.get_untracked_verdicts(ENTRY_DATE, ("proceed",)) == ()

    def test_an_empty_recommendation_set_selects_nothing(
        self, state_store: StateStore
    ) -> None:
        _seed_verdict(state_store)

        assert state_store.get_untracked_verdicts(ENTRY_DATE, ()) == ()


class TestVerdictReasons:
    def test_the_stored_reasons_json_is_returned_verbatim(
        self, state_store: StateStore
    ) -> None:
        _seed_verdict(state_store)

        raw = state_store.get_verdict_reasons_json(RUN_ID, SYMBOL)

        assert raw is not None
        assert json.loads(raw) == [
            {"text": "出来高が伴う", "source_ids": [], "basis": None}
        ]

    def test_a_position_whose_verdict_is_gone_reads_back_as_none(
        self, state_store: StateStore
    ) -> None:
        assert state_store.get_verdict_reasons_json(RUN_ID, SYMBOL) is None


class TestOrphanReconciliation:
    def test_a_position_whose_verdict_is_gone_is_deleted_with_its_marks_and_notes(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_verdict_position(_position(), [_mark(ENTRY_DATE, 100.0)])
        state_store.upsert_verdict_position_note(
            VerdictPositionNote(
                run_id=RUN_ID, symbol=SYMBOL, note_date=ENTRY_DATE, note="様子見"
            )
        )
        # Re-ingesting a corrected result replaces the run's verdicts wholesale
        # and this symbol is no longer analyzed at all: nothing explains the
        # position any more, so it goes with its marks and notes.

        deleted = state_store.delete_orphaned_verdict_positions()

        assert deleted == ((RUN_ID, SYMBOL),)
        assert state_store.get_verdict_positions() == ()
        assert state_store.get_verdict_position_marks(RUN_ID, SYMBOL) == ()
        assert state_store.get_verdict_position_notes(RUN_ID, SYMBOL) == ()

    def test_a_demoted_verdict_keeps_its_position_and_is_realigned(
        self, state_store: StateStore
    ) -> None:
        # Issue #190: both sides are tracked under identical exit rules, so a
        # proceed -> skip correction moves the row between strata instead of
        # destroying a replay that is still exactly right.
        _seed_verdict(state_store)
        state_store.upsert_verdict_position(_position(), [_mark(ENTRY_DATE, 100.0)])
        _seed_verdict(state_store, recommendation="skip")

        assert state_store.delete_orphaned_verdict_positions() == ()
        assert state_store.sync_verdict_position_recommendations() == (
            (RUN_ID, SYMBOL, "skip"),
        )

        stored = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert stored is not None
        assert stored.recommendation == "skip"
        assert state_store.get_verdict_position_marks(RUN_ID, SYMBOL) != ()

    def test_a_partial_realign_rolls_back_after_an_earlier_row_already_moved(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two drifted positions; the second UPDATE fails once the first has
        # already been applied inside the transaction. A half-realigned ledger
        # would report one symbol under a side its verdict no longer says.
        _seed_verdicts(state_store, ("proceed", "proceed"))
        state_store.upsert_verdict_position(_position())
        state_store.upsert_verdict_position(_position(symbol="BBB"))
        _seed_verdicts(state_store, ("skip", "skip"))
        real_connect = state_store.database.connect
        monkeypatch.setattr(
            state_store.database,
            "connect",
            lambda: _FlakyConnection(real_connect(), fail_on=4),
        )

        with pytest.raises(RuntimeError, match="injected failure"):
            state_store.sync_verdict_position_recommendations()
        monkeypatch.undo()

        assert {
            position.recommendation for position in state_store.get_verdict_positions()
        } == {"proceed"}

    def test_realigning_an_already_aligned_ledger_changes_nothing(
        self, state_store: StateStore
    ) -> None:
        _seed_verdict(state_store)
        state_store.upsert_verdict_position(_position())

        assert state_store.sync_verdict_position_recommendations() == ()

    def test_a_legacy_null_recommendation_reads_as_proceed(
        self, state_store: StateStore
    ) -> None:
        # A row written before the column existed. NULL must behave exactly
        # like an explicit 'proceed' for both the realign check and the filter.
        _seed_verdict(state_store)
        state_store.upsert_verdict_position(_position())
        with state_store.database.connect() as conn:
            conn.execute("UPDATE verdict_positions SET recommendation = NULL")

        assert state_store.sync_verdict_position_recommendations() == ()

        stored = state_store.get_verdict_positions(None, ("proceed",))
        assert [position.recommendation for position in stored] == ["proceed"]

    def test_a_position_backed_by_a_standing_proceed_verdict_is_kept(
        self, state_store: StateStore
    ) -> None:
        _seed_verdict(state_store)
        position = _position()
        state_store.upsert_verdict_position(position, [_mark(ENTRY_DATE, 100.0)])

        assert state_store.delete_orphaned_verdict_positions() == ()
        assert state_store.get_verdict_positions() == (position,)

    def test_an_empty_ledger_deletes_nothing(self, state_store: StateStore) -> None:
        assert state_store.delete_orphaned_verdict_positions() == ()

    def test_a_partial_delete_rolls_back_after_an_earlier_row_already_went(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No verdicts at all, so both positions are orphans. The second one's
        # first DELETE fails once the first has already been removed inside
        # the transaction: a half-reconciled ledger is worse than none.
        state_store.upsert_verdict_position(_position(), [_mark(ENTRY_DATE, 100.0)])
        state_store.upsert_verdict_position(
            _position(symbol="BBB"),
            [replace(_mark(ENTRY_DATE, 100.0), symbol="BBB")],
        )
        real_connect = state_store.database.connect
        monkeypatch.setattr(
            state_store.database,
            "connect",
            lambda: _FlakyConnection(real_connect(), fail_on=6),
        )

        with pytest.raises(RuntimeError, match="injected failure"):
            state_store.delete_orphaned_verdict_positions()
        monkeypatch.undo()

        assert {
            position.symbol for position in state_store.get_verdict_positions()
        } == {SYMBOL, "BBB"}
        assert len(state_store.get_verdict_position_marks(RUN_ID, SYMBOL)) == 1
