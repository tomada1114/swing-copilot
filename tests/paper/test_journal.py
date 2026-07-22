"""Acceptance tests for `paper/journal.py` (FR-11, CON-04)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pandas as pd
import pytest

from swing_copilot.models import Position
from swing_copilot.paper.journal import PaperJournal, PositionNotClosableError
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store


@pytest.fixture
def market_store(tmp_path):
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


@pytest.fixture
def journal(state_store):
    return PaperJournal(state_store)


def _open_position(
    position_id: UUID | None = None,
    *,
    entry_price: float = 100.0,
    shares: int = 10,
    entry_date: date = date(2026, 7, 1),
) -> Position:
    return Position(
        position_id=position_id or uuid4(),
        symbol="AAPL",
        is_paper=True,
        entry_date=entry_date,
        entry_price=entry_price,
        shares=shares,
        status="open",
        stop_price=95.0,
    )


def _write_spy_bars(
    market_store: MarketStore, start: date, end: date, prices: list[float]
) -> None:
    days = pd.date_range(start, end, freq="D")
    rows = [
        {
            "symbol": "SPY",
            "date": day.date(),
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price,
            "volume": 1_000_000,
            "provider": "yfinance",
            "fetched_at": datetime(2026, 7, 20, tzinfo=UTC),
        }
        for day, price in zip(days, prices, strict=True)
    ]
    market_store.write_bars(pd.DataFrame(rows))


class TestRecordDecisionIdempotency:
    def test_recording_same_natural_key_twice_updates_not_duplicates(
        self, journal, state_store
    ):
        run_id = uuid4()

        journal.record_decision(run_id, "AAPL", "default", "ignored", "too risky", None)
        journal.record_decision(
            run_id, "AAPL", "default", "followed", "changed my mind", 150.0
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT decision, reason_memo, virtual_fill_price FROM trades_journal "
                "WHERE run_id = ?",
                [str(run_id)],
            ).fetchall()
        assert rows == [("followed", "changed my mind", 150.0)]


class TestClosePositionLifecycle:
    def test_closes_an_open_position(self, journal, state_store):
        position = _open_position()
        state_store.upsert_position(position)

        journal.close_position(position.position_id, date(2026, 7, 15), 110.0)

        result = state_store.get_position(position.position_id)
        assert result.status == "closed"
        assert result.close_date == date(2026, 7, 15)
        assert result.close_price == 110.0

    def test_raises_for_nonexistent_position(self, journal):
        with pytest.raises(PositionNotClosableError, match="no position exists"):
            journal.close_position(uuid4(), date(2026, 7, 15), 110.0)

    def test_raises_for_already_closed_position(self, journal, state_store):
        position = _open_position()
        state_store.upsert_position(position)
        journal.close_position(position.position_id, date(2026, 7, 15), 110.0)

        with pytest.raises(PositionNotClosableError, match="already closed"):
            journal.close_position(position.position_id, date(2026, 7, 16), 111.0)


class TestSummarizePerformance:
    def test_no_closed_positions_returns_zeroed_summary(self, journal, market_store):
        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.closed_trade_count == 0
        assert result.total_pnl_usd == 0.0
        assert result.win_rate == 0.0
        assert result.spy_return_pct is None

    def test_computes_exact_pnl_and_win_rate_over_closed_trades(
        self, journal, state_store, market_store
    ):
        winner = _open_position(
            entry_price=100.0, shares=10, entry_date=date(2026, 7, 1)
        )
        loser = _open_position(entry_price=200.0, shares=5, entry_date=date(2026, 7, 5))
        state_store.upsert_position(winner)
        state_store.upsert_position(loser)
        journal.close_position(winner.position_id, date(2026, 7, 10), 110.0)  # +100
        journal.close_position(loser.position_id, date(2026, 7, 12), 190.0)  # -50
        _write_spy_bars(market_store, date(2026, 7, 1), date(2026, 7, 20), [500.0] * 20)

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.closed_trade_count == 2
        assert result.total_pnl_usd == pytest.approx(50.0)  # (110-100)*10 + (190-200)*5
        assert result.win_rate == pytest.approx(0.5)

    def test_excludes_open_positions_from_the_summary(
        self, journal, state_store, market_store
    ):
        open_position = _open_position()
        state_store.upsert_position(open_position)

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.closed_trade_count == 0

    def test_spy_return_computed_over_earliest_entry_to_as_of_span(
        self, journal, state_store, market_store
    ):
        position = _open_position(entry_date=date(2026, 7, 1))
        state_store.upsert_position(position)
        journal.close_position(position.position_id, date(2026, 7, 10), 110.0)
        _write_spy_bars(
            market_store,
            date(2026, 7, 1),
            date(2026, 7, 20),
            [500.0 + i for i in range(20)],
        )

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        expected = (519.0 - 500.0) / 500.0 * 100
        assert result.spy_return_pct == pytest.approx(expected)

    def test_spy_return_none_when_bars_insufficient(
        self, journal, state_store, market_store
    ):
        position = _open_position(entry_date=date(2026, 7, 1))
        state_store.upsert_position(position)
        journal.close_position(position.position_id, date(2026, 7, 10), 110.0)
        # No SPY bars written at all.

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.spy_return_pct is None
