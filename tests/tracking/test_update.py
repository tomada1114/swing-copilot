"""Verdict tracking: opening, daily advance, exits, and manual overrides.

Every price assertion is hand-calculated from `tests/tracking/conftest.py`'s
flat prelude (ATR(14) == 2.00 on the entry date), never read back from the
implementation.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from swing_copilot.tracking.update import (
    TrackingError,
    close_manually,
    record_note,
    update_tracking,
)
from tests.tracking.conftest import (
    DAY_1,
    DAY_2,
    ENTRY_DATE,
    EXIT_ATR_MULTIPLE,
    FLAT_ATR,
    FLAT_CLOSE,
    RISK_STOP,
    RUN_ID,
    SYMBOL,
    bar,
    flat_prelude,
    seed_risk,
    seed_verdict,
    write_bars,
)

if TYPE_CHECKING:
    from swing_copilot.config import BacktestConfig, Settings
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore


@pytest.fixture
def backtest_config(settings: Settings) -> BacktestConfig:
    return settings.backtest


def _rise(session_date: date, close: float) -> dict[str, Any]:
    """A quiet up day: two-wide range whose low sits one above the prior close."""
    return bar(
        session_date,
        open_price=close - 1.0,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
    )


class TestOpening:
    def test_proceed_verdict_opens_a_position_marked_flat_on_the_entry_day(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )

        assert (result.opened_count, result.advanced_count, result.closed_count) == (
            1,
            0,
            0,
        )
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.entry_date == ENTRY_DATE
        assert position.entry_price == FLAT_CLOSE
        assert position.stop_price == RISK_STOP
        assert position.days_held == 0
        assert position.status == "open"
        assert position.last_marked_date == ENTRY_DATE
        marks = state_store.get_verdict_position_marks(RUN_ID, SYMBOL)
        assert [(mark.as_of_date, mark.unrealized_return_pct) for mark in marks] == [
            (ENTRY_DATE, 0.0)
        ]

    def test_a_skip_verdict_is_never_tracked(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store, symbol="SKP", recommendation="skip")
        write_bars(market_store, flat_prelude(symbol="SKP"))

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )

        assert result.opened_count == 0
        assert state_store.get_verdict_positions() == ()

    def test_a_no_trade_proceed_verdict_is_tracked_with_the_flag_carried_through(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        # CASH_PRIORITY (or any regime that sets the run-level no_trade flag)
        # must not leave the ledger empty: the symbol's own proceed is still
        # useful as a judgement-quality data point, just never an actual buy.
        no_trade_run_id = uuid4()
        seed_verdict(state_store, run_id=no_trade_run_id, symbol="NTR", no_trade=True)
        seed_risk(state_store, run_id=no_trade_run_id, symbol="NTR")
        write_bars(market_store, flat_prelude(symbol="NTR"))

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )

        assert result.opened_count == 1
        position = state_store.get_verdict_position(no_trade_run_id, "NTR")
        assert position is not None
        assert position.no_trade is True
        assert position.status == "open"

    def test_missing_entry_price_skips_the_symbol_and_reopens_on_the_next_update(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        # No risk assessment and no bars at all: nothing to price the entry at.
        seed_verdict(state_store)

        first = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )

        assert first.opened_count == 0
        assert state_store.get_verdict_positions() == ()
        assert any("エントリー価格を解決できない" in note for note in first.notes)

        write_bars(market_store, flat_prelude())
        second = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )

        assert second.opened_count == 1
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.entry_price == FLAT_CLOSE

    def test_an_unusable_entry_day_close_is_treated_as_no_price_at_all(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        # No risk assessment, and the run day's own bar has no close.
        seed_verdict(state_store)
        write_bars(market_store, flat_prelude(sessions=19))
        write_bars(
            market_store,
            [
                bar(
                    ENTRY_DATE,
                    open_price=100.0,
                    high=101.0,
                    low=99.0,
                    close=math.nan,
                )
            ],
        )

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )

        assert result.opened_count == 0
        assert any("エントリー価格を解決できない" in note for note in result.notes)

    def test_history_without_a_bar_on_the_entry_day_cannot_price_the_entry(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        write_bars(
            market_store,
            [row for row in flat_prelude() if row["date"] != ENTRY_DATE],
        )

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )

        assert result.opened_count == 0
        assert any("エントリー価格を解決できない" in note for note in result.notes)

    def test_a_position_with_no_bars_at_all_opens_but_never_advances(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        # The risk assessment carries both prices, so the entry needs no bars;
        # without any, there is simply no session to replay.
        seed_verdict(state_store)
        seed_risk(state_store)

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=DAY_2
        )

        assert (result.opened_count, result.advanced_count) == (1, 0)
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.stop_price == RISK_STOP
        assert position.last_marked_date == ENTRY_DATE

    def test_null_risk_stop_falls_back_to_the_atr_derived_stop(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store, stop_price=None)
        write_bars(market_store, flat_prelude())

        update_tracking(state_store, market_store, backtest_config, as_of=ENTRY_DATE)

        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.stop_price == pytest.approx(
            FLAT_CLOSE - EXIT_ATR_MULTIPLE * FLAT_ATR
        )

    def test_unavailable_atr_leaves_no_stop_and_only_max_hold_can_close(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        # Five sessions is below ATR(14)'s minimum, so no stop exists at all.
        seed_verdict(state_store)
        seed_risk(state_store, stop_price=None)
        write_bars(market_store, flat_prelude(sessions=5))
        # A day that would breach any sane stop, yet must not close anything.
        write_bars(
            market_store,
            [bar(DAY_1, open_price=60.0, high=61.0, low=55.0, close=58.0)],
        )

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=DAY_1
        )

        assert any(f"ATR({14})を算出できず" in note for note in result.notes)
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.stop_price is None
        assert position.status == "open"
        assert position.days_held == 1


class TestDailyAdvance:
    def test_two_quiet_sessions_ratchet_the_stop_to_the_hand_calculated_value(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(market_store, [_rise(DAY_1, 102.0), _rise(DAY_2, 104.0)])

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=DAY_2
        )

        # Both days have true range max(2, |high - prev close| = 3, 1) = 3, so
        # Wilder smoothing runs 2.00 -> (13*2 + 3)/14 -> (13*that + 3)/14.
        atr_day_1 = (13 * FLAT_ATR + 3.0) / 14
        atr_day_2 = (13 * atr_day_1 + 3.0) / 14
        stop_day_1 = 102.0 - EXIT_ATR_MULTIPLE * atr_day_1
        stop_day_2 = 104.0 - EXIT_ATR_MULTIPLE * atr_day_2

        assert (result.opened_count, result.advanced_count, result.closed_count) == (
            1,
            1,
            0,
        )
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.days_held == 2
        assert position.stop_price == pytest.approx(stop_day_2)
        assert position.last_marked_date == DAY_2
        marks = state_store.get_verdict_position_marks(RUN_ID, SYMBOL)
        assert [mark.as_of_date for mark in marks] == [ENTRY_DATE, DAY_1, DAY_2]
        assert [mark.unrealized_return_pct for mark in marks] == pytest.approx(
            [0.0, 2.0, 4.0]
        )
        assert [mark.stop_price for mark in marks] == pytest.approx(
            [RISK_STOP, stop_day_1, stop_day_2]
        )

    def test_the_stop_set_on_a_day_cannot_close_the_position_that_same_day(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        # Day 1 closes at 102 and ratchets the stop to ~96.8; day 2's low of
        # 96.9 is above it, so nothing fires -- but a stop computed from day
        # 2's own close (104 - ~5.3 = ~98.7) would have closed it.
        write_bars(
            market_store,
            [
                _rise(DAY_1, 102.0),
                bar(DAY_2, open_price=102.0, high=105.0, low=96.9, close=104.0),
            ],
        )

        update_tracking(state_store, market_store, backtest_config, as_of=DAY_2)

        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.status == "open"
        assert position.days_held == 2

    def test_a_bar_with_no_usable_prices_is_skipped_and_reported(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(
            market_store,
            [
                bar(
                    DAY_1,
                    open_price=102.0,
                    high=103.0,
                    low=101.0,
                    close=math.nan,
                ),
                _rise(DAY_2, 104.0),
            ],
        )

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=DAY_2
        )

        assert any(
            f"{SYMBOL} {DAY_1.isoformat()}" in note and "バーが欠損" in note
            for note in result.notes
        )
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.days_held == 1
        # The skipped day still contributes to ATR (its high/low are intact),
        # and its unusable close leaves day 2 with only `high - low` as a true
        # range: 2.00 -> (13*2 + 3)/14 -> (13*that + 2)/14.
        atr_day_1 = (13 * FLAT_ATR + 3.0) / 14
        atr_day_2 = (13 * atr_day_1 + 2.0) / 14
        assert position.stop_price == pytest.approx(
            104.0 - EXIT_ATR_MULTIPLE * atr_day_2
        )
        marks = state_store.get_verdict_position_marks(RUN_ID, SYMBOL)
        assert [mark.as_of_date for mark in marks] == [ENTRY_DATE, DAY_2]

    def test_rerunning_the_same_as_of_changes_nothing(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(market_store, [_rise(DAY_1, 102.0)])
        update_tracking(state_store, market_store, backtest_config, as_of=DAY_1)
        first = state_store.get_verdict_position(RUN_ID, SYMBOL)
        first_marks = state_store.get_verdict_position_marks(RUN_ID, SYMBOL)

        second_result = update_tracking(
            state_store, market_store, backtest_config, as_of=DAY_1
        )

        assert (
            second_result.opened_count,
            second_result.advanced_count,
            second_result.closed_count,
        ) == (0, 0, 0)
        assert state_store.get_verdict_position(RUN_ID, SYMBOL) == first
        assert state_store.get_verdict_position_marks(RUN_ID, SYMBOL) == first_marks


class TestExits:
    def test_a_gap_below_the_stop_fills_at_the_open(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(
            market_store,
            [bar(DAY_1, open_price=94.0, high=95.0, low=90.0, close=93.0)],
        )

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=DAY_1
        )

        assert result.closed_count == 1
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.status == "closed"
        assert position.exit_date == DAY_1
        assert position.exit_price == 94.0
        assert position.exit_reason == "stop"
        assert position.realized_return_pct == pytest.approx(-6.0)
        last_mark = state_store.get_verdict_position_marks(RUN_ID, SYMBOL)[-1]
        assert last_mark.close == 93.0
        assert last_mark.unrealized_return_pct == pytest.approx(-7.0)

    def test_an_intraday_touch_fills_at_the_stop_itself(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(
            market_store,
            [bar(DAY_1, open_price=99.0, high=100.0, low=94.0, close=96.0)],
        )

        update_tracking(state_store, market_store, backtest_config, as_of=DAY_1)

        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.exit_price == RISK_STOP
        assert position.exit_reason == "stop"
        assert position.realized_return_pct == pytest.approx(-5.0)

    def test_reaching_max_hold_fills_at_the_close(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        config = backtest_config.model_copy(update={"max_hold_days": 2})
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(market_store, [_rise(DAY_1, 102.0), _rise(DAY_2, 104.0)])

        update_tracking(state_store, market_store, config, as_of=DAY_2)

        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.exit_date == DAY_2
        assert position.exit_price == 104.0
        assert position.exit_reason == "max_hold"
        assert position.realized_return_pct == pytest.approx(4.0)

    def test_the_stop_wins_when_both_trigger_on_the_same_session(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        config = backtest_config.model_copy(update={"max_hold_days": 1})
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(
            market_store,
            [bar(DAY_1, open_price=99.0, high=100.0, low=94.0, close=96.0)],
        )

        update_tracking(state_store, market_store, config, as_of=DAY_1)

        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.exit_reason == "stop"
        assert position.exit_price == RISK_STOP

    def test_a_closed_position_is_never_advanced_again(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(
            market_store,
            [bar(DAY_1, open_price=94.0, high=95.0, low=90.0, close=93.0)],
        )
        update_tracking(state_store, market_store, backtest_config, as_of=DAY_1)
        write_bars(market_store, [_rise(DAY_2, 104.0)])

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=DAY_2
        )

        assert (result.advanced_count, result.closed_count) == (0, 0)
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.exit_date == DAY_1
        assert [
            mark.as_of_date
            for mark in state_store.get_verdict_position_marks(RUN_ID, SYMBOL)
        ] == [ENTRY_DATE, DAY_1]


class TestAsOfBoundary:
    @pytest.mark.parametrize(
        ("as_of", "expected_opened"),
        [
            pytest.param(ENTRY_DATE - timedelta(days=1), 0, id="before"),
            pytest.param(ENTRY_DATE, 1, id="exactly-at"),
            pytest.param(ENTRY_DATE + timedelta(days=1), 1, id="after"),
        ],
    )
    def test_a_verdict_is_opened_only_once_its_run_date_is_reached(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
        as_of: date,
        expected_opened: int,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(market_store, [_rise(DAY_1, 102.0)])

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=as_of
        )

        assert result.opened_count == expected_opened

    def test_no_session_after_as_of_is_ever_marked(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(market_store, [_rise(DAY_1, 102.0), _rise(DAY_2, 104.0)])

        update_tracking(state_store, market_store, backtest_config, as_of=DAY_1)

        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.last_marked_date == DAY_1
        assert position.days_held == 1
        assert [
            mark.as_of_date
            for mark in state_store.get_verdict_position_marks(RUN_ID, SYMBOL)
        ] == [ENTRY_DATE, DAY_1]


class TestManualWrites:
    @pytest.fixture
    def opened(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(market_store, [_rise(DAY_1, 102.0)])
        update_tracking(state_store, market_store, backtest_config, as_of=DAY_1)

    @pytest.mark.usefixtures("opened")
    def test_manual_close_uses_the_session_close_and_records_the_note(
        self, state_store: StateStore, market_store: MarketStore
    ) -> None:
        closed = close_manually(
            state_store,
            market_store,
            run_id=RUN_ID,
            symbol=SYMBOL,
            as_of=DAY_1,
            note="決算前にリスクを落とす",
        )

        assert closed.exit_reason == "manual"
        assert closed.exit_price == 102.0
        assert closed.realized_return_pct == pytest.approx(2.0)
        assert state_store.get_verdict_position(RUN_ID, SYMBOL) == closed
        notes = state_store.get_verdict_position_notes(RUN_ID, SYMBOL)
        assert [(note.note_date, note.note) for note in notes] == [
            (DAY_1, "決算前にリスクを落とす")
        ]

    @pytest.mark.usefixtures("opened")
    def test_manual_close_without_a_bar_falls_back_to_the_last_mark(
        self, state_store: StateStore, market_store: MarketStore
    ) -> None:
        closed = close_manually(
            state_store, market_store, run_id=RUN_ID, symbol=SYMBOL, as_of=DAY_2
        )

        assert closed.exit_date == DAY_2
        assert closed.exit_price == 102.0
        assert state_store.get_verdict_position_notes(RUN_ID, SYMBOL) == ()

    @pytest.mark.usefixtures("opened")
    def test_closing_an_already_closed_position_is_rejected(
        self, state_store: StateStore, market_store: MarketStore
    ) -> None:
        close_manually(
            state_store, market_store, run_id=RUN_ID, symbol=SYMBOL, as_of=DAY_1
        )

        with pytest.raises(TrackingError, match="既に"):
            close_manually(
                state_store, market_store, run_id=RUN_ID, symbol=SYMBOL, as_of=DAY_1
            )

    def test_closing_an_untracked_position_is_rejected(
        self, state_store: StateStore, market_store: MarketStore
    ) -> None:
        with pytest.raises(TrackingError, match="存在しない"):
            close_manually(
                state_store, market_store, run_id=RUN_ID, symbol=SYMBOL, as_of=DAY_1
            )

    @pytest.mark.usefixtures("opened")
    def test_closing_before_the_entry_date_is_rejected(
        self, state_store: StateStore, market_store: MarketStore
    ) -> None:
        with pytest.raises(TrackingError, match="より前にできない"):
            close_manually(
                state_store,
                market_store,
                run_id=RUN_ID,
                symbol=SYMBOL,
                as_of=ENTRY_DATE - timedelta(days=1),
            )

    @pytest.mark.usefixtures("opened")
    def test_a_note_on_the_same_day_corrects_the_previous_one(
        self, state_store: StateStore
    ) -> None:
        record_note(
            state_store, run_id=RUN_ID, symbol=SYMBOL, note_date=DAY_1, note="様子見"
        )
        record_note(
            state_store,
            run_id=RUN_ID,
            symbol=SYMBOL,
            note_date=DAY_1,
            note="やはり利確を検討",
        )

        notes = state_store.get_verdict_position_notes(RUN_ID, SYMBOL)
        assert [note.note for note in notes] == ["やはり利確を検討"]

    @pytest.mark.usefixtures("opened")
    def test_a_blank_note_is_rejected(self, state_store: StateStore) -> None:
        with pytest.raises(TrackingError, match="空である"):
            record_note(
                state_store, run_id=RUN_ID, symbol=SYMBOL, note_date=DAY_1, note="   "
            )

    def test_a_note_on_an_untracked_position_is_rejected(
        self, state_store: StateStore
    ) -> None:
        with pytest.raises(TrackingError, match="存在しない"):
            record_note(
                state_store, run_id=RUN_ID, symbol=SYMBOL, note_date=DAY_1, note="所感"
            )

    @pytest.mark.usefixtures("opened")
    def test_closing_before_the_last_marked_session_is_rejected(
        self, state_store: StateStore, market_store: MarketStore
    ) -> None:
        # DAY_1 is already replayed and marked. Closing at ENTRY_DATE would
        # leave exit_date behind last_marked_date, days_held, and a mark dated
        # after the position's own exit.
        with pytest.raises(TrackingError, match="最終マーク日"):
            close_manually(
                state_store,
                market_store,
                run_id=RUN_ID,
                symbol=SYMBOL,
                as_of=ENTRY_DATE,
            )

        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.status == "open"

    @pytest.mark.usefixtures("opened")
    def test_a_note_with_imperative_trading_language_is_rejected(
        self, state_store: StateStore
    ) -> None:
        # A note is skill-authored text that `show` prints verbatim, so it
        # goes through the same CON-03 guard as every other skill output.
        with pytest.raises(TrackingError, match="CON-03"):
            record_note(
                state_store,
                run_id=RUN_ID,
                symbol=SYMBOL,
                note_date=DAY_1,
                note="AAA は今すぐ買うべきである",
            )

        assert state_store.get_verdict_position_notes(RUN_ID, SYMBOL) == ()

    @pytest.mark.usefixtures("opened")
    def test_a_manual_close_with_a_forbidden_note_writes_nothing(
        self, state_store: StateStore, market_store: MarketStore
    ) -> None:
        with pytest.raises(TrackingError, match="CON-03"):
            close_manually(
                state_store,
                market_store,
                run_id=RUN_ID,
                symbol=SYMBOL,
                as_of=DAY_1,
                note="ここは売るべき",
            )

        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.status == "open"
        assert state_store.get_verdict_position_notes(RUN_ID, SYMBOL) == ()


class TestVerdictReconciliation:
    def test_a_retracted_proceed_verdict_removes_what_it_opened(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        write_bars(market_store, [_rise(DAY_1, 102.0)])
        update_tracking(state_store, market_store, backtest_config, as_of=DAY_1)
        record_note(
            state_store, run_id=RUN_ID, symbol=SYMBOL, note_date=DAY_1, note="様子見"
        )

        # Re-ingesting a corrected analysis_result.json replaces the run's
        # verdicts wholesale; AAA is demoted from proceed to skip.
        seed_verdict(state_store, recommendation="skip")
        result = update_tracking(
            state_store, market_store, backtest_config, as_of=DAY_2
        )

        assert state_store.get_verdict_position(RUN_ID, SYMBOL) is None
        assert state_store.get_verdict_position_marks(RUN_ID, SYMBOL) == ()
        assert state_store.get_verdict_position_notes(RUN_ID, SYMBOL) == ()
        assert any("取り消された" in note for note in result.notes)
        assert result.opened_count == 0

    def test_a_standing_proceed_verdict_keeps_its_position(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        seed_verdict(state_store)
        seed_risk(state_store)
        write_bars(market_store, flat_prelude())
        update_tracking(state_store, market_store, backtest_config, as_of=ENTRY_DATE)

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )

        assert state_store.get_verdict_position(RUN_ID, SYMBOL) is not None
        assert result.notes == ()


class TestDataQuality:
    def test_a_position_without_any_stored_bar_is_reported_every_update(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        # The risk row prices the entry, so the position opens -- but the
        # symbol goes dark straight afterwards (delisting, universe exit), so
        # it can never advance and max-hold can never fire.
        seed_verdict(state_store)
        seed_risk(state_store)

        opening = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )
        later = update_tracking(state_store, market_store, backtest_config, as_of=DAY_2)

        assert opening.opened_count == 1
        assert any("バーが1本も無い" in note for note in opening.notes)
        assert any("バーが1本も無い" in note for note in later.notes)
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.status == "open"

    def test_a_non_finite_stored_close_is_never_used_as_an_entry_price(
        self,
        state_store: StateStore,
        market_store: MarketStore,
        backtest_config: BacktestConfig,
    ) -> None:
        # No risk assessment, so the entry falls back to the run day's close.
        # An infinite close passes `<= 0` but would poison every later return.
        seed_verdict(state_store)
        write_bars(market_store, flat_prelude(sessions=19))
        write_bars(
            market_store,
            [
                bar(
                    ENTRY_DATE,
                    open_price=FLAT_CLOSE,
                    high=FLAT_CLOSE + 1.0,
                    low=FLAT_CLOSE - 1.0,
                    close=math.inf,
                )
            ],
        )

        result = update_tracking(
            state_store, market_store, backtest_config, as_of=ENTRY_DATE
        )

        assert result.opened_count == 0
        assert state_store.get_verdict_positions() == ()
        assert any("エントリー価格を解決できない" in note for note in result.notes)
