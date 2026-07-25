"""Acceptance tests for `paper/journal.py` (FR-11, CON-04)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swing_copilot.models import Position
from swing_copilot.paper.journal import (
    InvalidDecisionError,
    PaperJournal,
    PositionNotClosableError,
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.paper_records import PositionExcursionRecord
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

    def test_raises_for_an_unrecognized_decision_value(self, journal):
        with pytest.raises(InvalidDecisionError, match="decision"):
            journal.record_decision(uuid4(), "AAPL", "default", "maybe", None, None)

    def test_recording_without_position_id_leaves_it_null(self, journal, state_store):
        run_id = uuid4()

        journal.record_decision(run_id, "AAPL", "default", "followed", None, 150.0)

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT position_id FROM trades_journal WHERE run_id = ?",
                [str(run_id)],
            ).fetchall()
        assert rows == [(None,)]

    def test_linking_decision_to_a_paper_position_enables_pnl_traceability(
        self, journal, state_store
    ):
        run_id = uuid4()
        journal.record_decision(
            run_id, "AAPL", "default", "followed", "looks good", 100.0
        )

        position = _open_position(entry_price=100.0, shares=10)
        state_store.upsert_position(position)
        journal.record_decision(
            run_id,
            "AAPL",
            "default",
            "followed",
            "looks good",
            100.0,
            position_id=position.position_id,
        )
        journal.close_position(position.position_id, date(2026, 7, 15), 110.0, "target")

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT p.entry_price, p.close_price, p.shares
                FROM trades_journal t
                JOIN positions p ON p.position_id = t.position_id
                WHERE t.run_id = ?
                """,
                [str(run_id)],
            ).fetchone()
        assert row is not None
        entry_price, close_price, shares = row
        pnl = (close_price - entry_price) * shares
        assert pnl == pytest.approx(100.0)

    def test_raises_when_position_id_given_with_ignored_decision(
        self, journal, state_store
    ):
        position = _open_position()
        state_store.upsert_position(position)

        with pytest.raises(InvalidDecisionError, match="position_id"):
            journal.record_decision(
                uuid4(),
                "AAPL",
                "default",
                "ignored",
                None,
                None,
                position_id=position.position_id,
            )


class TestClosePositionLifecycle:
    def test_closes_an_open_position(self, journal, state_store):
        position = _open_position()
        state_store.upsert_position(position)

        journal.close_position(position.position_id, date(2026, 7, 15), 110.0, "target")

        result = state_store.get_position(position.position_id)
        assert result.status == "closed"
        assert result.close_date == date(2026, 7, 15)
        assert result.close_at == datetime(
            2026, 7, 15, 16, tzinfo=ZoneInfo("America/New_York")
        )
        assert result.close_price == 110.0
        assert result.exit_reason == "target"

    def test_preserves_precise_timezone_aware_close_time(self, journal, state_store):
        position = _open_position()
        state_store.upsert_position(position)
        closed_at = datetime(2026, 7, 15, 19, 30, tzinfo=UTC)

        journal.close_position(
            position.position_id,
            date(2026, 7, 15),
            110.0,
            "target",
            closed_at=closed_at,
        )

        assert state_store.get_position(position.position_id).close_at == closed_at

    def test_rejects_naive_close_time(self, journal, state_store):
        position = _open_position()
        state_store.upsert_position(position)

        with pytest.raises(PositionNotClosableError, match="timezone-aware"):
            journal.close_position(
                position.position_id,
                date(2026, 7, 15),
                110.0,
                "target",
                closed_at=datetime(2026, 7, 15, 16),  # noqa: DTZ001
            )

    def test_raises_for_nonexistent_position(self, journal):
        with pytest.raises(PositionNotClosableError, match="no position exists"):
            journal.close_position(uuid4(), date(2026, 7, 15), 110.0, "target")

    def test_raises_for_already_closed_position(self, journal, state_store):
        position = _open_position()
        state_store.upsert_position(position)
        journal.close_position(position.position_id, date(2026, 7, 15), 110.0, "target")

        with pytest.raises(PositionNotClosableError, match="already closed"):
            journal.close_position(
                position.position_id, date(2026, 7, 16), 111.0, "target"
            )

    def test_raises_when_close_date_precedes_entry_date(self, journal, state_store):
        position = _open_position(entry_date=date(2026, 7, 10))
        state_store.upsert_position(position)

        with pytest.raises(PositionNotClosableError, match="precedes"):
            journal.close_position(
                position.position_id, date(2026, 7, 1), 110.0, "target"
            )

    def test_raises_for_zero_close_price(self, journal, state_store):
        position = _open_position()
        state_store.upsert_position(position)

        with pytest.raises(PositionNotClosableError, match="positive"):
            journal.close_position(
                position.position_id, date(2026, 7, 15), 0.0, "target"
            )

    def test_raises_for_negative_close_price(self, journal, state_store):
        position = _open_position()
        state_store.upsert_position(position)

        with pytest.raises(PositionNotClosableError, match="positive"):
            journal.close_position(
                position.position_id, date(2026, 7, 15), -5.0, "target"
            )

    def test_raises_for_empty_exit_reason(self, journal, state_store):
        # REQ-001/020: exit_reason is a required, non-keyword-default
        # parameter (unspecified is a TypeError at the call site, enforced
        # by Python itself); an empty/unspecified-looking value that is
        # still passed is rejected the same way as any other non-5-value
        # input.
        position = _open_position()
        state_store.upsert_position(position)

        with pytest.raises(PositionNotClosableError, match="exit_reason"):
            journal.close_position(position.position_id, date(2026, 7, 15), 110.0, "")

    def test_raises_for_invalid_exit_reason_and_leaves_position_unchanged(
        self, journal, state_store
    ):
        # Example 4: close(..., exit_reason="partial") is rejected and the
        # position's state does not change (REQ-020).
        position = _open_position()
        state_store.upsert_position(position)

        with pytest.raises(PositionNotClosableError, match="exit_reason"):
            journal.close_position(
                position.position_id, date(2026, 7, 15), 110.0, "partial"
            )

        result = state_store.get_position(position.position_id)
        assert result.status == "open"
        assert result.close_date is None
        assert result.close_price is None
        assert result.exit_reason is None

    def test_unknown_is_rejected_as_a_close_input(self, journal, state_store):
        # "unknown" is a migration-only backfill sentinel, never a valid
        # close_position() argument (REQ-001/002).
        position = _open_position()
        state_store.upsert_position(position)

        with pytest.raises(PositionNotClosableError, match="exit_reason"):
            journal.close_position(
                position.position_id, date(2026, 7, 15), 110.0, "unknown"
            )


def _close(
    journal: PaperJournal,
    position: Position,
    close_date: date,
    close_price: float,
    exit_reason: str = "manual",
) -> None:
    journal.close_position(position.position_id, close_date, close_price, exit_reason)


class TestSummarizePerformance:
    def test_no_closed_positions_returns_zeroed_summary(self, journal, market_store):
        # Boundary condition: closed_trade_count == 0 -> every rate/ratio is
        # None (undefined), not an exception and not a misleading 0.0.
        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.closed_trade_count == 0
        assert result.total_pnl_usd == 0.0
        assert result.win_rate is None
        assert result.spy_return_pct is None
        assert result.expectancy_usd is None
        assert result.profit_factor is None
        assert result.avg_r_multiple is None
        assert result.r_multiple_omitted_count == 0
        assert result.r_multiple_omitted_warning is None
        assert result.by_exit_reason == ()
        assert result.by_strategy == ()
        assert result.avg_mae_usd is None
        assert result.avg_mfe_usd is None
        assert result.excursion_notes == ()

    def test_averages_closed_trade_excursions_and_adds_possibility_notes(
        self, journal, state_store, market_store
    ):
        position = _open_position(
            entry_price=100.0, shares=10, entry_date=date(2026, 7, 1)
        )
        state_store.upsert_position(position)
        _close(journal, position, date(2026, 7, 10), 110.0, "target")
        state_store.upsert_position_excursions(
            [
                PositionExcursionRecord(
                    position.position_id,
                    date(2026, 7, 10),
                    -20.0,
                    30.0,
                    "OK",
                )
            ]
        )

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.avg_mae_usd == pytest.approx(-200.0)
        assert result.avg_mfe_usd == pytest.approx(300.0)
        assert any("利確が早すぎる可能性" in note for note in result.excursion_notes)
        assert any(
            "ストップが緩い/エントリーが早い可能性" in note
            for note in result.excursion_notes
        )

    def test_computes_exact_pnl_and_win_rate_over_closed_trades(
        self, journal, state_store, market_store
    ):
        winner = _open_position(
            entry_price=100.0, shares=10, entry_date=date(2026, 7, 1)
        )
        loser = _open_position(entry_price=200.0, shares=5, entry_date=date(2026, 7, 5))
        state_store.upsert_position(winner)
        state_store.upsert_position(loser)
        _close(journal, winner, date(2026, 7, 10), 110.0, "target")  # +100
        _close(journal, loser, date(2026, 7, 12), 190.0, "stop_loss")  # -50
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
        _close(journal, position, date(2026, 7, 10), 110.0)
        _write_spy_bars(
            market_store,
            date(2026, 7, 1),
            date(2026, 7, 20),
            [500.0 + i for i in range(20)],
        )

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        # 500 -> 519 over the span is a hand-computed 3.8% return, independent
        # of production's (last - first) / first * 100 formula.
        assert result.spy_return_pct == pytest.approx(3.8)

    def test_spy_return_none_when_bars_insufficient(
        self, journal, state_store, market_store
    ):
        position = _open_position(entry_date=date(2026, 7, 1))
        state_store.upsert_position(position)
        _close(journal, position, date(2026, 7, 10), 110.0)
        # No SPY bars written at all.

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.spy_return_pct is None

    def test_spy_return_is_none_with_exactly_one_bar_in_span(
        self, journal, state_store, market_store
    ):
        position = _open_position(entry_date=date(2026, 7, 1))
        state_store.upsert_position(position)
        _close(journal, position, date(2026, 7, 1), 110.0)
        _write_spy_bars(market_store, date(2026, 7, 1), date(2026, 7, 1), [500.0])

        result = journal.summarize_performance(market_store, date(2026, 7, 1))

        assert result.spy_return_pct is None

    def test_spy_return_is_computed_with_exactly_two_bars_in_span(
        self, journal, state_store, market_store
    ):
        position = _open_position(entry_date=date(2026, 7, 1))
        state_store.upsert_position(position)
        _close(journal, position, date(2026, 7, 2), 110.0)
        _write_spy_bars(
            market_store, date(2026, 7, 1), date(2026, 7, 2), [500.0, 510.0]
        )

        result = journal.summarize_performance(market_store, date(2026, 7, 2))

        assert result.spy_return_pct == pytest.approx(2.0)  # (510-500)/500*100

    def test_open_position_earlier_entry_date_is_ignored_for_spy_span(
        self, journal, state_store, market_store
    ):
        # The open position's entry_date is earlier than the closed
        # position's, but only closed positions may anchor the SPY span.
        open_position = _open_position(entry_date=date(2026, 6, 1))
        state_store.upsert_position(open_position)
        closed_position = _open_position(entry_date=date(2026, 7, 1))
        state_store.upsert_position(closed_position)
        _close(journal, closed_position, date(2026, 7, 10), 110.0)
        # SPY bars only exist from the closed position's entry_date onward;
        # if the open position's earlier entry leaked in, this span would be
        # insufficient and spy_return_pct would be None.
        _write_spy_bars(
            market_store,
            date(2026, 7, 1),
            date(2026, 7, 20),
            [500.0 + i for i in range(20)],
        )

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.spy_return_pct == pytest.approx(3.8)

    def test_as_of_excludes_a_position_closed_after_as_of_from_the_summary(
        self, journal, state_store, market_store
    ):
        as_of = date(2026, 7, 20)
        # Closed within the window: anchors the span and contributes P&L.
        in_window = _open_position(
            entry_price=100.0, shares=10, entry_date=date(2026, 6, 25)
        )
        state_store.upsert_position(in_window)
        _close(journal, in_window, date(2026, 7, 15), 110.0)  # +100
        # Closed exactly at as_of: still included (inclusive boundary).
        at_boundary = _open_position(
            entry_price=200.0, shares=5, entry_date=date(2026, 6, 30)
        )
        state_store.upsert_position(at_boundary)
        _close(journal, at_boundary, as_of, 190.0)  # -50
        # Closed after as_of, with an earlier entry_date than both of the
        # above: must be excluded from count, P&L, win rate, and the SPY
        # span's earliest-entry anchor.
        after_as_of = _open_position(
            entry_price=50.0, shares=100, entry_date=date(2026, 6, 1)
        )
        state_store.upsert_position(after_as_of)
        _close(
            journal, after_as_of, date(2026, 7, 21), 60.0
        )  # would be +1000 if leaked

        _write_spy_bars(
            market_store, date(2026, 6, 25), as_of, [500.0 + i for i in range(26)]
        )

        result = journal.summarize_performance(market_store, as_of)

        assert result.closed_trade_count == 2
        assert result.total_pnl_usd == pytest.approx(50.0)  # 100 + (-50)
        assert result.win_rate == pytest.approx(0.5)
        # Span anchored at 2026-06-25 (at_boundary's entry_date), not
        # 2026-06-01 (the excluded after_as_of position's earlier entry).
        expected_spy_return = (525.0 - 500.0) / 500.0 * 100
        assert result.spy_return_pct == pytest.approx(expected_spy_return)

    def test_example_1_hand_calculated_five_trade_summary(
        self, journal, state_store, market_store
    ):
        # Issue's worked Example 1: pnl=[+100,+50,-30,-20,+70],
        # exit_reason=[target,target,stop_loss,stop_loss,manual].
        # win_rate=3/5=60%, profit_factor=220/50=4.4, expectancy=34.
        specs = [
            (200.0, "target", 100.0),
            (150.0, "target", 50.0),
            (70.0, "stop_loss", -30.0),
            (80.0, "stop_loss", -20.0),
            (170.0, "manual", 70.0),
        ]
        for close_price, exit_reason, _expected_pnl in specs:
            position = _open_position(entry_price=100.0, shares=1)
            state_store.upsert_position(position)
            _close(journal, position, date(2026, 7, 10), close_price, exit_reason)
        _write_spy_bars(market_store, date(2026, 7, 1), date(2026, 7, 20), [500.0] * 20)

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.closed_trade_count == 5
        assert result.total_pnl_usd == pytest.approx(170.0)
        assert result.win_rate == pytest.approx(0.6)
        assert result.profit_factor == pytest.approx(4.4)
        assert result.expectancy_usd == pytest.approx(34.0)

    def test_example_2_all_winning_trades_profit_factor_is_none(
        self, journal, state_store, market_store
    ):
        # Issue's worked Example 2: pnl=[+10,+20,+30] (all winning) ->
        # total loss is 0 -> profit_factor is None, no ZeroDivisionError.
        for close_price in (110.0, 120.0, 130.0):
            position = _open_position(entry_price=100.0, shares=1)
            state_store.upsert_position(position)
            _close(journal, position, date(2026, 7, 10), close_price, "target")
        _write_spy_bars(market_store, date(2026, 7, 1), date(2026, 7, 20), [500.0] * 20)

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.closed_trade_count == 3
        assert result.win_rate == pytest.approx(1.0)
        assert result.profit_factor is None

    def test_example_3_stop_missing_omits_r_multiple_with_count_and_warning(
        self, journal, state_store, market_store
    ):
        # Issue's worked Example 3: 3 trades, 1 missing entry/stop info ->
        # only 2 R-multiples are computed, and the warning mentions "1件".
        with_stop_a = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=date(2026, 7, 1),
            entry_price=100.0,
            shares=10,
            status="open",
            stop_price=90.0,  # risk_per_share=10, pnl=+50 -> r=+0.5
        )
        with_stop_b = Position(
            position_id=uuid4(),
            symbol="MSFT",
            is_paper=True,
            entry_date=date(2026, 7, 1),
            entry_price=200.0,
            shares=5,
            status="open",
            stop_price=180.0,  # risk_per_share=20, pnl=-50 -> r=-0.5
        )
        missing_stop = Position(
            position_id=uuid4(),
            symbol="GOOG",
            is_paper=True,
            entry_date=date(2026, 7, 1),
            entry_price=100.0,
            shares=10,
            status="open",
            stop_price=None,  # stop never recorded -> R-multiple omitted
        )
        for position in (with_stop_a, with_stop_b, missing_stop):
            state_store.upsert_position(position)
        _close(journal, with_stop_a, date(2026, 7, 10), 105.0, "target")  # pnl=+50
        _close(journal, with_stop_b, date(2026, 7, 10), 190.0, "stop_loss")  # pnl=-50
        _close(journal, missing_stop, date(2026, 7, 10), 110.0, "manual")  # pnl=+100
        _write_spy_bars(market_store, date(2026, 7, 1), date(2026, 7, 20), [500.0] * 20)

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.r_multiple_omitted_count == 1
        assert result.r_multiple_omitted_warning is not None
        assert "1件" in result.r_multiple_omitted_warning
        # avg over the 2 computable r-multiples: (0.5 + -0.5) / 2 == 0.0
        assert result.avg_r_multiple == pytest.approx(0.0)

    def test_r_multiple_omitted_when_stop_is_at_or_above_entry(
        self, journal, state_store, market_store
    ):
        # Judgment call (defensive extension beyond the issue's literal
        # text): entry - stop <= 0 is a data anomaly, treated the same as a
        # missing stop rather than dividing by zero/a negative number.
        anomalous = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=date(2026, 7, 1),
            entry_price=100.0,
            shares=10,
            status="open",
            stop_price=100.0,  # entry - stop == 0
        )
        state_store.upsert_position(anomalous)
        _close(journal, anomalous, date(2026, 7, 10), 110.0, "target")
        _write_spy_bars(market_store, date(2026, 7, 1), date(2026, 7, 20), [500.0] * 20)

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.r_multiple_omitted_count == 1
        assert result.avg_r_multiple is None

    def test_pnl_exactly_zero_is_neutral_excluded_from_win_and_profit_factor(
        self, journal, state_store, market_store
    ):
        # Documented convention: pnl == 0 is neither a win nor a loss. It's
        # counted in closed_trade_count/expectancy but excluded from the
        # win-rate numerator and both profit_factor sums.
        winner = _open_position(entry_price=100.0, shares=1)
        loser = _open_position(entry_price=100.0, shares=1)
        flat = _open_position(entry_price=100.0, shares=1)
        for position in (winner, loser, flat):
            state_store.upsert_position(position)
        _close(journal, winner, date(2026, 7, 10), 110.0, "target")  # +10
        _close(journal, loser, date(2026, 7, 10), 90.0, "stop_loss")  # -10
        _close(journal, flat, date(2026, 7, 10), 100.0, "time_stop")  # 0
        _write_spy_bars(market_store, date(2026, 7, 1), date(2026, 7, 20), [500.0] * 20)

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        assert result.closed_trade_count == 3
        # 1 win out of 3 trades (the flat trade is neutral, not a win).
        assert result.win_rate == pytest.approx(1 / 3)
        # gains=10, losses=10 -> profit_factor=1.0 (the flat trade
        # contributes to neither sum).
        assert result.profit_factor == pytest.approx(1.0)
        assert result.expectancy_usd == pytest.approx(0.0)  # (10 - 10 + 0) / 3

    def test_breakdown_by_exit_reason_and_strategy(
        self, journal, state_store, market_store
    ):
        # REQ-007: exit_reason and strategy breakdowns with trade_count,
        # win_rate, avg_pnl_usd per group. One position is deliberately
        # left unlinked to any trades_journal row -> buckets under
        # "unknown" in by_strategy.
        run_id = uuid4()
        target_win = _open_position(entry_price=100.0, shares=1)
        target_loss = _open_position(entry_price=100.0, shares=1)
        stop_loss = _open_position(entry_price=100.0, shares=1)
        unlinked = _open_position(entry_price=100.0, shares=1)
        for position in (target_win, target_loss, stop_loss, unlinked):
            state_store.upsert_position(position)

        journal.record_decision(
            run_id,
            "AAPL",
            "trend_follow",
            "followed",
            None,
            100.0,
            position_id=target_win.position_id,
        )
        journal.record_decision(
            run_id,
            "MSFT",
            "mean_revert",
            "followed",
            None,
            100.0,
            position_id=target_loss.position_id,
        )
        journal.record_decision(
            run_id,
            "GOOG",
            "trend_follow",
            "followed",
            None,
            100.0,
            position_id=stop_loss.position_id,
        )
        # unlinked's position is closed without ever recording a decision.

        _close(journal, target_win, date(2026, 7, 10), 120.0, "target")  # +20
        _close(journal, target_loss, date(2026, 7, 10), 80.0, "target")  # -20
        _close(journal, stop_loss, date(2026, 7, 10), 90.0, "stop_loss")  # -10
        _close(journal, unlinked, date(2026, 7, 10), 110.0, "target")  # +10
        _write_spy_bars(market_store, date(2026, 7, 1), date(2026, 7, 20), [500.0] * 20)

        result = journal.summarize_performance(market_store, date(2026, 7, 20))

        by_reason = {row.key: row for row in result.by_exit_reason}
        assert by_reason["target"].trade_count == 3
        assert by_reason["target"].win_rate == pytest.approx(2 / 3)
        assert by_reason["target"].avg_pnl_usd == pytest.approx((20 - 20 + 10) / 3)
        assert by_reason["stop_loss"].trade_count == 1
        assert by_reason["stop_loss"].win_rate == pytest.approx(0.0)
        assert by_reason["stop_loss"].avg_pnl_usd == pytest.approx(-10.0)

        by_strategy = {row.key: row for row in result.by_strategy}
        assert by_strategy["trend_follow"].trade_count == 2
        assert by_strategy["trend_follow"].avg_pnl_usd == pytest.approx((20 - 10) / 2)
        assert by_strategy["mean_revert"].trade_count == 1
        assert by_strategy["mean_revert"].avg_pnl_usd == pytest.approx(-20.0)
        assert by_strategy["unknown"].trade_count == 1
        assert by_strategy["unknown"].avg_pnl_usd == pytest.approx(10.0)
