"""Tests for StateStore's paper-trading-journal write/read methods (FR-11, CON-04)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID, uuid4

import pytest

from swing_copilot.models import Position
from swing_copilot.storage.database import Database
from swing_copilot.storage.paper_records import TradeDecisionRecord
from swing_copilot.storage.state_store import StateStore


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store


class TestRecordTradeDecision:
    def test_records_a_new_decision(self, state_store):
        run_id = uuid4()
        record = TradeDecisionRecord(
            run_id=run_id,
            symbol="AAPL",
            strategy_key="default",
            position_id=None,
            decision="followed",
            reason_memo="matches my plan",
            virtual_fill_price=150.0,
        )

        state_store.record_trade_decision(record)

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT decision, reason_memo FROM trades_journal WHERE run_id = ?",
                [str(run_id)],
            ).fetchall()
        assert rows == [("followed", "matches my plan")]

    def test_rerecording_same_natural_key_updates_the_row_not_a_duplicate(
        self, state_store
    ):
        run_id = uuid4()
        first = TradeDecisionRecord(
            run_id=run_id,
            symbol="AAPL",
            strategy_key="default",
            position_id=None,
            decision="ignored",
            reason_memo="too risky",
            virtual_fill_price=None,
        )
        second = TradeDecisionRecord(
            run_id=run_id,
            symbol="AAPL",
            strategy_key="default",
            position_id=None,
            decision="followed",
            reason_memo="changed my mind",
            virtual_fill_price=151.0,
        )

        state_store.record_trade_decision(first)
        state_store.record_trade_decision(second)

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute("SELECT count(*) FROM trades_journal").fetchone()
            rows = conn.execute(
                "SELECT decision, reason_memo FROM trades_journal WHERE run_id = ?",
                [str(run_id)],
            ).fetchall()
        assert count == (1,)
        assert rows == [("followed", "changed my mind")]

    def test_rerecording_same_natural_key_preserves_original_created_at(
        self, state_store
    ):
        run_id = uuid4()
        original = TradeDecisionRecord(
            run_id=run_id,
            symbol="AAPL",
            strategy_key="default",
            position_id=None,
            decision="ignored",
            reason_memo="too risky",
            virtual_fill_price=None,
        )
        state_store.record_trade_decision(original)

        # Pin created_at to a sentinel in the past so the assertion below
        # cannot pass by accident of two `now()` calls landing in the same
        # tick — only a correct ON CONFLICT clause preserves it exactly.
        sentinel_created_at = datetime(2020, 1, 1, tzinfo=UTC)
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE trades_journal SET created_at = ? WHERE run_id = ?",
                [sentinel_created_at, str(run_id)],
            )

        correction = TradeDecisionRecord(
            run_id=run_id,
            symbol="AAPL",
            strategy_key="default",
            position_id=None,
            decision="followed",
            reason_memo="changed my mind",
            virtual_fill_price=151.0,
        )
        state_store.record_trade_decision(correction)

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT created_at, decision FROM trades_journal WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()
        assert row[0] == sentinel_created_at
        assert row[1] == "followed"

    def test_different_strategy_keys_are_independent_rows(self, state_store):
        run_id = uuid4()
        for strategy_key in ("default", "aggressive"):
            state_store.record_trade_decision(
                TradeDecisionRecord(
                    run_id=run_id,
                    symbol="AAPL",
                    strategy_key=strategy_key,
                    position_id=None,
                    decision="followed",
                    reason_memo=None,
                    virtual_fill_price=None,
                )
            )

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute("SELECT count(*) FROM trades_journal").fetchone()
        assert count == (2,)


def _position(
    position_id: UUID,
    *,
    status: Literal["open", "closed"] = "open",
    close_date: date | None = None,
    close_price: float | None = None,
) -> Position:
    return Position(
        position_id=position_id,
        symbol="AAPL",
        is_paper=True,
        entry_date=date(2026, 7, 1),
        entry_price=100.0,
        shares=10,
        status=status,
        stop_price=95.0,
        close_date=close_date,
        close_price=close_price,
    )


class TestGetPosition:
    def test_returns_none_when_position_does_not_exist(self, state_store):
        assert state_store.get_position(uuid4()) is None

    def test_returns_the_matching_position_regardless_of_status(self, state_store):
        position_id = uuid4()
        state_store.upsert_position(_position(position_id, status="closed"))

        result = state_store.get_position(position_id)

        assert result is not None
        assert result.position_id == position_id
        assert result.status == "closed"


class TestGetClosedPositions:
    def test_returns_only_closed_positions_matching_is_paper(self, state_store):
        open_id, closed_id = uuid4(), uuid4()
        state_store.upsert_position(_position(open_id, status="open"))
        state_store.upsert_position(
            _position(
                closed_id,
                status="closed",
                close_date=date(2026, 7, 15),
                close_price=110.0,
            )
        )

        result = state_store.get_closed_positions(is_paper=True)

        assert [p.position_id for p in result] == [closed_id]

    def test_returns_empty_list_when_none_closed(self, state_store):
        assert state_store.get_closed_positions(is_paper=True) == []

    def test_as_of_none_returns_every_closed_position_regardless_of_close_date(
        self, state_store
    ):
        closed_id = uuid4()
        state_store.upsert_position(
            _position(
                closed_id,
                status="closed",
                close_date=date(2026, 7, 25),
                close_price=110.0,
            )
        )

        result = state_store.get_closed_positions(is_paper=True, as_of=None)

        assert [p.position_id for p in result] == [closed_id]

    def test_as_of_boundary_includes_position_closed_just_before_and_exactly_at(
        self, state_store
    ):
        as_of = date(2026, 7, 20)
        before_id, at_id, after_id = uuid4(), uuid4(), uuid4()
        state_store.upsert_position(
            _position(
                before_id,
                status="closed",
                close_date=date(2026, 7, 19),
                close_price=110.0,
            )
        )
        state_store.upsert_position(
            _position(at_id, status="closed", close_date=as_of, close_price=110.0)
        )
        state_store.upsert_position(
            _position(
                after_id,
                status="closed",
                close_date=date(2026, 7, 21),
                close_price=110.0,
            )
        )

        result = state_store.get_closed_positions(is_paper=True, as_of=as_of)

        assert {p.position_id for p in result} == {before_id, at_id}
