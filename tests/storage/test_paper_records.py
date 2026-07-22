"""Tests for StateStore's paper-trading-journal write/read methods (FR-11, CON-04)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

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
    position_id, *, status="open", close_date=None, close_price=None
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
