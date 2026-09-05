"""Tests for BacktestEngine: no look-ahead, fills, stops, costs, benchmark (FR-10)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from swing_copilot.backtest.engine import BacktestEngine, BacktestResult, Trade
from swing_copilot.backtest.metrics import (
    ENTRY_BLOCK_ALREADY_HELD,
    ENTRY_BLOCK_INSUFFICIENT_CASH,
    ENTRY_BLOCK_INVALID_STOP,
    ENTRY_BLOCK_LIMIT_NOT_REACHED,
    ENTRY_BLOCK_MAX_CONCURRENT,
    ENTRY_BLOCK_MISSING_DATA,
    ENTRY_BLOCK_REASONS,
    ENTRY_BLOCK_REGIME,
)
from swing_copilot.backtest.policy import EntryDecision, EntryPolicyRequest
from swing_copilot.risk.checks import RiskChecker
from swing_copilot.risk.position_sizing import calc_position_size
from swing_copilot.screening.base import Candidate
from tests.backtest.conftest import (
    LONG_TRADING_DAYS,
    TRADING_DAYS,
    bar_row,
    bars_frame,
    flat_bars,
)

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.config import Settings


INITIAL_CASH = 100_000.0
SIM_POSITION_CAP_PCT = 0.10
SIM_TRADE_RISK_PCT = 0.01


def _candidate(
    symbol: str, *, atr14: float = 2.0, rank: int = 1, as_of: date = TRADING_DAYS[0]
) -> Candidate:
    return Candidate(
        symbol=symbol,
        as_of=as_of,
        signal_names=("trend_sma",),
        metrics={"atr14": atr14},
        rank=rank,
    )


def _no_candidates(_day: date) -> list[Candidate]:
    return []


def _spy_bars(days: list[date], price: float = 400.0) -> list[dict[str, object]]:
    return flat_bars("SPY", days, price)


@pytest.fixture
def engine(settings):
    return BacktestEngine(settings)


class TestNoLookahead:
    def test_signal_generated_from_a_day_fills_on_the_next_day_open_not_same_day(
        self, engine
    ):
        days = TRADING_DAYS[:5]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100, 101, 99, 100)),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
            bar_row("AAA", days[2], (105, 106, 104, 105)),  # would-be fill day
            bar_row("AAA", days[3], (105, 106, 104, 105)),
            bar_row("AAA", days[4], (105, 106, 104, 105)),
        ]
        bars = bars_frame(rows)

        candidates_by_day = {days[1]: [_candidate("AAA", as_of=days[1])]}

        def candidates_fn(day):
            return candidates_by_day.get(day, [])

        result = engine.run(days, bars, candidates_fn, INITIAL_CASH)

        # No fill may happen on the signal day itself (days[1]): equity
        # there must equal untouched cash. The fill happens at days[2]'s
        # open, so equity moves away from initial cash starting days[2].
        equity_by_day = dict(result.equity_curve)
        assert equity_by_day[days[0]] == pytest.approx(INITIAL_CASH)
        assert equity_by_day[days[1]] == pytest.approx(INITIAL_CASH)
        assert equity_by_day[days[2]] != pytest.approx(INITIAL_CASH)

    def test_bars_past_the_final_session_do_not_move_a_single_number(self, engine):
        # Issue #224 reads the trailing stop's ATR from a column computed over
        # the whole frame rather than re-smoothing the `as_of` prefix on every
        # simulated day. That is only admissible because the read is causal, so
        # appending sessions *after* the simulated window must leave the trade
        # log, both equity curves, and the final equity bit-identical -- not
        # merely approximate. The appended sessions are deliberately flat and
        # numerous: a leaked ATR would decay from 4.0 to ~0.015, ratchet the
        # 2.5x trailing stop from 10 below the close to a few cents below it,
        # and turn the end-of-backtest liquidation below into a stop exit.
        days = LONG_TRADING_DAYS[:25]
        later = LONG_TRADING_DAYS[25:]
        rows = [
            *_spy_bars(days),
            *(
                bar_row("AAA", day, (100 + index, 102 + index, 98 + index, 101 + index))
                for index, day in enumerate(days)
            ),
        ]
        future_rows = [
            *_spy_bars(later),
            *(bar_row("AAA", day, (125.0, 125.0, 125.0, 125.0)) for day in later),
        ]
        candidates_by_day = {days[1]: [_candidate("AAA", as_of=days[1])]}

        def candidates_fn(day):
            return candidates_by_day.get(day, [])

        contained = engine.run(days, bars_frame(rows), candidates_fn, INITIAL_CASH)
        unsliced = engine.run(
            days, bars_frame([*rows, *future_rows]), candidates_fn, INITIAL_CASH
        )

        assert [trade.exit_reason for trade in contained.trades] == ["end_of_backtest"]
        assert unsliced.trades == contained.trades
        assert unsliced.equity_curve == contained.equity_curve
        assert unsliced.benchmark_curve == contained.benchmark_curve
        assert unsliced.final_equity == contained.final_equity


class TestEntryFill:
    def test_fills_at_next_open_with_slippage_and_commission(self, settings, engine):
        days = TRADING_DAYS[:4]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100, 101, 99, 100)),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
            bar_row("AAA", days[2], (100, 105, 98, 102)),
            bar_row("AAA", days[3], (102, 106, 100, 103)),
        ]
        bars = bars_frame(rows)
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=2.0, as_of=days[1])]}

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        expected_entry_price = 100.0 * (1 + settings.backtest.slippage_pct)
        expected_limit_price = 100.0
        expected_stop = 100.0 - settings.trade_plan.exit_atr_multiple * 2.0
        expected_shares = calc_position_size(
            INITIAL_CASH,
            expected_limit_price,
            expected_stop,
            settings.backtest.sim_position_cap_pct,
            settings.backtest.sim_trade_risk_pct,
        ).shares

        # Verify the open position on its fill day, before the mandatory
        # end-of-window liquidation updates the final curve point.
        equity_by_day = dict(result.equity_curve)
        cost = (
            expected_shares
            * expected_entry_price
            * (1 + settings.backtest.commission_pct)
        )
        expected_cash_after_fill = INITIAL_CASH - cost
        expected_equity_on_fill_day = expected_cash_after_fill + expected_shares * 102.0
        assert equity_by_day[days[2]] == pytest.approx(expected_equity_on_fill_day)


class TestLimitEntryGate:
    def _engine(self, settings):
        return BacktestEngine(
            settings.model_copy(
                update={
                    "trade_plan": settings.trade_plan.model_copy(
                        update={"entry_limit_atr_multiple": 0.5}
                    )
                }
            )
        )

    def _rows(self, days, fill_ohlc):
        return [
            *_spy_bars(days),
            *[
                bar_row("AAA", days[0], (100.0, 101.0, 99.0, 100.0)),
                bar_row("AAA", days[1], (100.0, 101.0, 99.0, 100.0)),
                bar_row("AAA", days[2], fill_ohlc),
                bar_row("AAA", days[3], (101.0, 102.0, 100.0, 101.0)),
            ],
        ]

    def test_gap_up_without_a_limit_touch_is_counted_and_not_filled(self, settings):
        days = TRADING_DAYS[:4]
        result = self._engine(settings).run(
            days,
            bars_frame(self._rows(days, (105.0, 106.0, 102.0, 105.0))),
            lambda day: [_candidate("AAA", as_of=day)] if day == days[1] else [],
            INITIAL_CASH,
        )

        assert result.trades == ()
        assert dict(result.entry_block_counts)[ENTRY_BLOCK_LIMIT_NOT_REACHED] == 1
        assert dict(result.entry_block_days)[ENTRY_BLOCK_LIMIT_NOT_REACHED] == 1

    def test_intraday_touch_fills_at_the_limit_without_adverse_slippage(self, settings):
        days = TRADING_DAYS[:4]
        result = self._engine(settings).run(
            days,
            bars_frame(self._rows(days, (105.0, 106.0, 100.0, 105.0))),
            lambda day: [_candidate("AAA", as_of=day)] if day == days[1] else [],
            INITIAL_CASH,
        )

        assert result.trades[0].entry_price == pytest.approx(101.0)

    def test_next_limit_mode_enables_the_gate_even_at_zero_multiple(self, settings):
        limited_settings = settings.model_copy(
            update={
                "backtest": settings.backtest.model_copy(update={"entry": "next_limit"})
            }
        )
        days = TRADING_DAYS[:4]
        result = BacktestEngine(limited_settings).run(
            days,
            bars_frame(self._rows(days, (105.0, 106.0, 102.0, 105.0))),
            lambda day: [_candidate("AAA", as_of=day)] if day == days[1] else [],
            INITIAL_CASH,
        )

        assert result.trades == ()
        assert dict(result.entry_block_counts)[ENTRY_BLOCK_LIMIT_NOT_REACHED] == 1


class TestDuplicateBars:
    """Issue #244: the two bar lookups tie-break duplicates differently.

    A corrected bar appended after the original leaves two rows on one
    `(symbol, date)`. The fill/exit lookup takes the *first* such row and the
    as-of lookup behind the mark-to-market takes the *last* -- flipping either
    one moves the equity curve silently, so both ends are pinned end to end.
    """

    def test_fill_reads_the_first_row_and_the_close_mark_reads_the_last(
        self, settings, engine
    ):
        days = TRADING_DAYS[:4]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100, 101, 99, 100)),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
            bar_row("AAA", days[2], (100, 105, 98, 102)),  # fill day, first copy
            bar_row("AAA", days[2], (200, 205, 198, 202)),  # same day, appended later
            bar_row("AAA", days[3], (102, 106, 100, 103)),
        ]
        bars = bars_frame(rows)
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=2.0, as_of=days[1])]}

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        expected_entry_price = 100.0 * (1 + settings.backtest.slippage_pct)
        expected_limit_price = 100.0
        expected_stop = 100.0 - settings.trade_plan.exit_atr_multiple * 2.0
        expected_shares = calc_position_size(
            INITIAL_CASH,
            expected_limit_price,
            expected_stop,
            settings.backtest.sim_position_cap_pct,
            settings.backtest.sim_trade_risk_pct,
        ).shares
        cost = (
            expected_shares
            * expected_entry_price
            * (1 + settings.backtest.commission_pct)
        )

        # The first row's open, not the appended copy's 200.
        assert result.trades[0].entry_price == pytest.approx(expected_entry_price)
        # The appended copy's close, not the first row's 102.
        assert dict(result.equity_curve)[days[2]] == pytest.approx(
            INITIAL_CASH - cost + expected_shares * 202.0
        )


class TestStopAnchorParity:
    def test_risk_checker_and_backtest_use_the_same_signal_close_anchor(self, settings):
        days = TRADING_DAYS[:4]
        close = 100.0
        atr14 = 2.0
        candidate = Candidate(
            symbol="AAA",
            as_of=days[1],
            signal_names=("trend_sma",),
            metrics={"close": close, "atr14": atr14},
            rank=1,
        )
        risk_assessment = RiskChecker(settings).check([candidate])[0]
        result = BacktestEngine(settings).run(
            days,
            bars_frame(
                [
                    *_spy_bars(days),
                    bar_row("AAA", days[0], (100.0, 101.0, 99.0, 100.0)),
                    bar_row("AAA", days[1], (100.0, 101.0, 99.0, close)),
                    bar_row("AAA", days[2], (100.0, 101.0, 99.0, 100.0)),
                    bar_row("AAA", days[3], (100.0, 101.0, 99.0, 100.0)),
                ]
            ),
            lambda day: [candidate] if day == days[1] else [],
            INITIAL_CASH,
        )

        assert risk_assessment.stop_price == pytest.approx(
            result.trades[0].initial_stop_price
        )
        assert risk_assessment.stop_price == pytest.approx(
            close - settings.trade_plan.exit_atr_multiple * atr14
        )

    @pytest.mark.parametrize(
        ("entry_mode", "entry_limit_multiple", "fill_ohlc", "expected_entry"),
        [
            pytest.param(
                "next_limit",
                0.5,
                (100.5, 102.0, 99.5, 101.0),
                100.5 * (1 + 0.001),
                id="gap-up-open-fill",
            ),
            pytest.param(
                "next_limit",
                0.5,
                (105.0, 106.0, 100.0, 104.0),
                101.0,
                id="intraday-limit-touch",
            ),
            pytest.param(
                "next_open",
                0.0,
                (105.0, 106.0, 104.0, 105.0),
                105.0 * (1 + 0.001),
                id="zero-k-next-open-compatibility",
            ),
        ],
    )
    def test_fill_paths_keep_the_initial_stop_anchored_to_signal_close(
        self, settings, entry_mode, entry_limit_multiple, fill_ohlc, expected_entry
    ):
        # Signal-day close (100.0) and ATR14 (2.0) are fixed by the bars/candidate
        # below; the worst-case limit for sizing is derived the same way
        # `entry_limit_price` does, without importing it just for this literal.
        expected_limit = 100.0 + entry_limit_multiple * 2.0
        days = TRADING_DAYS[:4]
        custom_settings = settings.model_copy(
            update={
                "backtest": settings.backtest.model_copy(update={"entry": entry_mode}),
                "trade_plan": settings.trade_plan.model_copy(
                    update={"entry_limit_atr_multiple": entry_limit_multiple}
                ),
            }
        )
        result = BacktestEngine(custom_settings).run(
            days,
            bars_frame(
                [
                    *_spy_bars(days),
                    *flat_bars("AAA", days[:2], 100.0),
                    bar_row("AAA", days[2], fill_ohlc),
                    bar_row("AAA", days[3], (100.0, 101.0, 99.0, 100.0)),
                ]
            ),
            lambda day: (
                [_candidate("AAA", atr14=2.0, as_of=days[1])] if day == days[1] else []
            ),
            INITIAL_CASH,
        )

        assert result.trades[0].entry_price == pytest.approx(expected_entry)
        assert result.trades[0].initial_stop_price == pytest.approx(95.0)
        # Sizing must key off the worst-case `limit_price`, not the actual
        # fill: a regression that sized from `entry_price` instead would pass
        # every other assertion here while breaking acceptance criterion 2.
        expected_shares = calc_position_size(
            INITIAL_CASH,
            expected_limit,
            95.0,
            custom_settings.backtest.sim_position_cap_pct,
            custom_settings.backtest.sim_trade_risk_pct,
        ).shares
        assert result.trades[0].shares == expected_shares

    def test_sizing_widens_past_the_limit_when_the_fills_own_slippage_exceeds_it(
        self, settings
    ):
        # The open sits exactly at the limit (101.0), so `evaluate_entry_fill`
        # takes the open-with-slippage arm and the execution price
        # (101.101) ends up fractionally above the limit it was gated by.
        custom_settings = settings.model_copy(
            update={
                "backtest": settings.backtest.model_copy(
                    update={"entry": "next_limit"}
                ),
                "trade_plan": settings.trade_plan.model_copy(
                    update={"entry_limit_atr_multiple": 0.5}
                ),
            }
        )
        days = TRADING_DAYS[:4]
        result = BacktestEngine(custom_settings).run(
            days,
            bars_frame(
                [
                    *_spy_bars(days),
                    *flat_bars("AAA", days[:2], 100.0),
                    bar_row("AAA", days[2], (101.0, 102.0, 99.0, 100.5)),
                    bar_row("AAA", days[3], (100.0, 101.0, 99.0, 100.0)),
                ]
            ),
            lambda day: (
                [_candidate("AAA", atr14=2.0, as_of=days[1])] if day == days[1] else []
            ),
            INITIAL_CASH,
        )

        execution_price = 101.0 * (1 + custom_settings.backtest.slippage_pct)
        assert result.trades[0].entry_price == pytest.approx(execution_price)
        # A regression that sized off the bare `limit_price` (101.0) would
        # produce 99 shares here instead of 98.
        expected_shares = calc_position_size(
            INITIAL_CASH,
            execution_price,
            95.0,
            custom_settings.backtest.sim_position_cap_pct,
            custom_settings.backtest.sim_trade_risk_pct,
        ).shares
        assert result.trades[0].shares == expected_shares

    def test_nonpositive_fill_is_rejected_as_an_invalid_stop(self, engine):
        days = TRADING_DAYS[:3]
        rows = [
            *_spy_bars(days),
            *flat_bars("AAA", days[:1], 100.0),
            bar_row("AAA", days[1], (100.0, 101.0, 99.0, 100.0)),
            bar_row("AAA", days[2], (0.0, 1.0, 0.0, 0.5)),
        ]
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=2.0, as_of=days[1])]}

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        assert result.trades == ()
        assert dict(result.entry_block_counts)[ENTRY_BLOCK_INVALID_STOP] == 1


def _naive_equity(bars, cash, positions, day):
    """Cash plus every position marked at its newest close on or before `day`.

    Deliberately the pre-#244 full-frame scan rather than the engine's own
    helper: the point of the assertion below is that the carried basis still
    equals an *independently* recomputed one.
    """
    total = cash
    for position in positions:
        rows = bars[(bars["symbol"] == position.symbol) & (bars["date"] <= day)]
        if not rows.empty:
            total += position.shares * float(rows.sort_values("date").iloc[-1]["close"])
    return total


class TestEquityBasis:
    def test_carried_sizing_basis_equals_a_fresh_mark_to_market(
        self, engine, monkeypatch
    ):
        # Issue #244 stopped recomputing the signal day's equity for every fill
        # day and carries the equity curve's last point instead. That is an
        # identity, not an approximation -- but only as long as nothing between
        # the curve append and the next day's fill step touches cash or the
        # open positions. Assert it holds on every fill day, exactly (`==`, not
        # `approx`), so a future reordering of the day loop cannot quietly turn
        # the sizing basis into a stale number.
        days = LONG_TRADING_DAYS[:12]
        rows = [
            *_spy_bars(days),
            *flat_bars("AAA", days, 100.0),
            *flat_bars("BBB", days, 50.0),
            # CCC is missing a session, so its mark has to carry an earlier
            # close forward and the two computations can disagree if either
            # one resolves the gap differently.
            *[row for row in flat_bars("CCC", days, 30.0) if row["date"] != days[8]],
        ]
        bars = bars_frame(rows)
        candidates_by_day = {
            days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])],
            days[3]: [_candidate("BBB", atr14=1.0, as_of=days[3])],
            days[5]: [_candidate("CCC", atr14=1.0, as_of=days[5])],
        }
        original = engine._fill_pending_entries  # noqa: SLF001
        observed: list[tuple[float, float, float]] = []

        def spy(day, signal, bars, pending, state):
            if signal is not None:
                observed.append(
                    (
                        signal.equity,
                        _naive_equity(
                            bars, state.cash, state.open_positions.values(), signal.day
                        ),
                        state.cash,
                    )
                )
            original(day, signal, bars, pending, state)

        monkeypatch.setattr(engine, "_fill_pending_entries", spy)

        engine.run(days, bars, lambda d: candidates_by_day.get(d, []), INITIAL_CASH)

        assert [carried for carried, _fresh, _cash in observed] == [
            fresh for _carried, fresh, _cash in observed
        ]
        # The equality would be vacuous if no position were ever open.
        assert any(carried != cash for carried, _fresh, cash in observed)


class TestGapStop:
    def test_gap_below_stop_fills_at_open_not_stop_price(self, engine):
        days = TRADING_DAYS[:5]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100, 101, 99, 100)),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
            bar_row("AAA", days[2], (100, 101, 99, 100)),  # fill day, entry ~100.1
            bar_row("AAA", days[3], (80, 81, 79, 80)),  # gaps well below stop
            bar_row("AAA", days[4], (80, 81, 79, 80)),
        ]
        bars = bars_frame(rows)
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        stop_trades = [t for t in result.trades if t.exit_reason == "stop"]
        assert len(stop_trades) == 1
        assert stop_trades[0].exit_price == pytest.approx(80.0 * (1 - 0.001))
        assert stop_trades[0].exit_date == days[3]

    def test_gap_below_initial_stop_on_fill_day_settles_with_both_side_costs(
        self, settings, engine
    ):
        days = TRADING_DAYS[:4]
        fill_open = 95.0
        signal_close = 100.0
        atr14 = 1.0
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100.0, 101.0, 99.0, 100.0)),
            bar_row("AAA", days[1], (100.0, 101.0, 99.0, signal_close)),
            bar_row("AAA", days[2], (fill_open, 96.0, 94.0, 95.0)),
            bar_row("AAA", days[3], (95.0, 96.0, 94.0, 95.0)),
        ]
        result = engine.run(
            days,
            bars_frame(rows),
            lambda day: (
                [_candidate("AAA", atr14=atr14, as_of=days[1])]
                if day == days[1]
                else []
            ),
            INITIAL_CASH,
        )

        limit_price = signal_close
        stop_price = signal_close - settings.trade_plan.exit_atr_multiple * atr14
        shares = calc_position_size(
            INITIAL_CASH,
            limit_price,
            stop_price,
            settings.backtest.sim_position_cap_pct,
            settings.backtest.sim_trade_risk_pct,
        ).shares
        slippage = settings.backtest.slippage_pct
        commission = settings.backtest.commission_pct
        entry_execution = fill_open * (1 + slippage)
        exit_execution = fill_open * (1 - slippage)
        entry_cost = shares * entry_execution * (1 + commission)
        exit_proceeds = shares * exit_execution * (1 - commission)

        assert result.final_equity == pytest.approx(
            INITIAL_CASH - entry_cost + exit_proceeds
        )
        assert result.trades[0].days_held == 0
        assert result.trades[0].exit_reason == "stop"
        assert result.trades[0].exit_date == days[2]
        assert result.trades[0].risk_basis_price == pytest.approx(limit_price)
        # The planned risk remains limit_price - stop_price even though the
        # fill gapped below the signal-day stop; pnl still reflects both-side
        # slippage and commission, so this hand calculation is -0.152R.
        expected_r_multiple = (exit_proceeds - entry_cost) / (
            (limit_price - stop_price) * shares
        )
        assert expected_r_multiple == pytest.approx(-0.152)
        assert result.avg_r_multiple == pytest.approx(expected_r_multiple)

    def test_intraday_touch_fills_at_stop_price_not_low(self, engine):
        days = TRADING_DAYS[:5]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100, 101, 99, 100)),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
            bar_row(
                "AAA", days[2], (100, 101, 99, 100)
            ),  # entry ~100.1, stop ~97.6 (atr=1.0)
            bar_row(
                "AAA", days[3], (100, 101, 90, 100)
            ),  # opens above stop, dips through it
            bar_row("AAA", days[4], (100, 101, 99, 100)),
        ]
        bars = bars_frame(rows)
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        stop_trades = [t for t in result.trades if t.exit_reason == "stop"]
        assert len(stop_trades) == 1
        raw_stop = 100.0 - 2.5 * 1.0
        assert stop_trades[0].exit_price == pytest.approx(raw_stop * (1 - 0.001))
        assert stop_trades[0].exit_date == days[3]


class TestMaxHold:
    def test_forced_exit_at_max_hold_days(self, settings, engine):
        max_hold = settings.trade_plan.max_hold_days
        days = LONG_TRADING_DAYS[: max_hold + 3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        bars = bars_frame(rows)
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        max_hold_trades = [t for t in result.trades if t.exit_reason == "max_hold"]
        assert len(max_hold_trades) == 1
        # Entry day is holding session 1, so forced exit is session 60.
        assert max_hold_trades[0].exit_date == days[2 + max_hold - 1]

    def test_stop_takes_precedence_if_triggered_on_max_hold_day(self, settings):
        custom_settings = settings.model_copy(
            update={
                "trade_plan": settings.trade_plan.model_copy(
                    update={"max_hold_days": 2}
                )
            }
        )
        engine = BacktestEngine(custom_settings)
        days = TRADING_DAYS[:5]
        rows = [
            *_spy_bars(days),
            *flat_bars("AAA", days[:3], 100.0),
            bar_row("AAA", days[3], (80.0, 81.0, 79.0, 80.0)),
            bar_row("AAA", days[4], (80.0, 81.0, 79.0, 80.0)),
        ]
        candidates = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        result = engine.run(
            days, bars_frame(rows), lambda day: candidates.get(day, []), INITIAL_CASH
        )

        assert result.trades[0].exit_reason == "stop"


class TestCashAndRankConstraints:
    def test_lower_ranked_candidate_skipped_when_cash_exhausted(self, engine):
        days = TRADING_DAYS[:3]
        rows = [
            *_spy_bars(days),
            *flat_bars("AAA", days, 5_000.0),
            *flat_bars("BBB", days, 5_000.0),
        ]
        bars = bars_frame(rows)
        candidates_by_day = {
            days[1]: [
                _candidate("AAA", atr14=50.0, rank=1, as_of=days[1]),
                _candidate("BBB", atr14=50.0, rank=2, as_of=days[1]),
            ]
        }

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), initial_cash=6_000.0
        )

        equity_last = result.equity_curve[-1][1]
        assert equity_last > 0
        # With only $6,000 cash and each ~$5,000 position sized near equity
        # limits, at most one of the two candidates can be filled.
        held_symbols = {
            trade.symbol
            for trade in result.trades
            if trade.exit_reason == "end_of_backtest"
        }
        assert len(held_symbols) <= 1

    def test_concurrent_positions_use_configured_simulator_cap(self, settings, engine):
        days = TRADING_DAYS[:3]
        symbols = [f"SYM{i}" for i in range(20)]
        rows = list(_spy_bars(days))
        for symbol in symbols:
            rows += flat_bars(symbol, days, 10.0)
        bars = bars_frame(rows)
        candidates_by_day = {
            days[1]: [
                _candidate(symbol, atr14=0.5, rank=i + 1, as_of=days[1])
                for i, symbol in enumerate(symbols)
            ]
        }

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), initial_cash=1_000_000.0
        )

        max_concurrent = settings.backtest.max_concurrent_positions
        filled_symbols = {trade.symbol for trade in result.trades}
        assert len(filled_symbols) <= max_concurrent


class TestBenchmarkAndReproducibility:
    def test_benchmark_curve_tracks_spy_buy_and_hold(self, engine):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days, price=400.0), *flat_bars("AAA", days, 100.0)]
        bars = bars_frame(rows)

        result = engine.run(days, bars, _no_candidates, INITIAL_CASH)

        expected_shares = int(INITIAL_CASH / 400.0)
        for _day, value in result.benchmark_curve:
            assert value == pytest.approx(expected_shares * 400.0)

    def test_benchmark_curve_carries_residual_cash_at_every_point(self, engine):
        days = TRADING_DAYS[:4]
        rows = [
            bar_row("SPY", days[0], (333.0, 333.0, 333.0, 333.0)),
            bar_row("SPY", days[1], (340.0, 340.0, 340.0, 340.0)),
            bar_row("SPY", days[2], (340.0, 340.0, 340.0, 340.0)),
            bar_row("SPY", days[3], (340.0, 340.0, 340.0, 340.0)),
            *flat_bars("AAA", days, 100.0),
        ]
        result = engine.run(days, bars_frame(rows), _no_candidates, INITIAL_CASH)

        # 300 shares at $333 leave $100 residual; $300 x $340 + $100 = $102,100.
        assert [value for _day, value in result.benchmark_curve] == [
            100_000.0,
            102_100.0,
            102_100.0,
            102_100.0,
        ]
        assert result.benchmark_final_equity == pytest.approx(102_100.0)

    def test_same_input_produces_identical_result(self, engine):
        days = TRADING_DAYS[:6]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100, 101, 99, 100)),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
            bar_row("AAA", days[2], (100, 105, 98, 102)),
            bar_row("AAA", days[3], (102, 106, 100, 103)),
            bar_row("AAA", days[4], (103, 107, 101, 104)),
            bar_row("AAA", days[5], (104, 108, 102, 105)),
        ]
        bars = bars_frame(rows)
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=2.0, as_of=days[1])]}

        def candidates_fn(day):
            return candidates_by_day.get(day, [])

        first = engine.run(days, bars.copy(), candidates_fn, INITIAL_CASH)
        second = engine.run(days, bars.copy(), candidates_fn, INITIAL_CASH)

        assert first == second

    def test_no_candidates_produces_no_trades_and_flat_equity(self, engine):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        bars = bars_frame(rows)

        result = engine.run(days, bars, _no_candidates, INITIAL_CASH)

        assert result.trades == ()
        assert all(
            value == pytest.approx(INITIAL_CASH) for _day, value in result.equity_curve
        )

    def test_survivorship_bias_note_is_present(self, engine):
        days = TRADING_DAYS[:2]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        bars = bars_frame(rows)

        result = engine.run(days, bars, _no_candidates, INITIAL_CASH)

        assert "survivorship" in result.survivorship_bias_note.lower()

    def test_empty_trading_calendar_returns_unchanged_cash(self, engine):
        result = engine.run([], bars_frame([]), _no_candidates, INITIAL_CASH)

        assert result.trades == ()
        assert result.final_equity == pytest.approx(INITIAL_CASH)
        assert result.benchmark_final_equity == pytest.approx(INITIAL_CASH)

    def test_final_equity_includes_exit_slippage_and_commission(self, settings, engine):
        days = TRADING_DAYS[:3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        result = engine.run(
            days,
            bars_frame(rows),
            lambda day: candidates.get(day, []),
            INITIAL_CASH,
        )

        limit = 100.0
        stop = 100.0 - settings.trade_plan.exit_atr_multiple
        shares = calc_position_size(
            INITIAL_CASH,
            limit,
            stop,
            settings.backtest.sim_position_cap_pct,
            settings.backtest.sim_trade_risk_pct,
        ).shares
        entry = 100.0 * (1 + settings.backtest.slippage_pct)
        entry_cost = shares * entry * (1 + settings.backtest.commission_pct)
        exit_price = 100.0 * (1 - settings.backtest.slippage_pct)
        exit_proceeds = shares * exit_price * (1 - settings.backtest.commission_pct)
        assert result.final_equity == pytest.approx(
            INITIAL_CASH - entry_cost + exit_proceeds
        )
        assert result.equity_curve[-1][1] == pytest.approx(result.final_equity)
        assert sum(trade.pnl for trade in result.trades) == pytest.approx(
            result.final_equity - INITIAL_CASH
        )
        assert result.expectancy_per_trade == pytest.approx(result.trades[0].pnl)


class TestTradePnl:
    def test_pnl_is_exit_minus_entry_times_shares(self):

        trade = Trade(
            symbol="AAA",
            entry_date=TRADING_DAYS[0],
            entry_price=100.0,
            exit_date=TRADING_DAYS[1],
            exit_price=110.0,
            shares=10,
            exit_reason="stop",
        )
        assert trade.pnl == pytest.approx(100.0)

    def test_pnl_subtracts_round_trip_commission(self):
        trade = Trade(
            symbol="AAA",
            entry_date=TRADING_DAYS[0],
            entry_price=100.0,
            exit_date=TRADING_DAYS[1],
            exit_price=110.0,
            shares=10,
            exit_reason="stop",
            commission_usd=2.1,
        )

        assert trade.pnl == pytest.approx(97.9)


class TestPessimisticSlippageMultiplier:
    """P2-09: slippage_multiplier scales the same slippage_pct on both sides."""

    def _engine_with_multiplier(
        self, settings: Settings, multiplier: float
    ) -> BacktestEngine:
        custom_settings = settings.model_copy(
            update={
                "backtest": settings.backtest.model_copy(
                    update={"slippage_multiplier": multiplier}
                )
            }
        )
        return BacktestEngine(custom_settings)

    def test_multiplier_one_matches_default_entry_and_exit_prices(self, settings):
        days = TRADING_DAYS[:3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        default_engine = BacktestEngine(settings)
        explicit_one_engine = self._engine_with_multiplier(settings, 1.0)

        default_result = default_engine.run(
            days, bars_frame(rows), lambda d: candidates.get(d, []), INITIAL_CASH
        )
        explicit_result = explicit_one_engine.run(
            days, bars_frame(rows), lambda d: candidates.get(d, []), INITIAL_CASH
        )

        assert default_result == explicit_result

    def test_higher_multiplier_worsens_entry_and_exit_execution_prices(self, settings):
        days = TRADING_DAYS[:3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        normal_engine = self._engine_with_multiplier(settings, 1.0)
        pessimistic_engine = self._engine_with_multiplier(settings, 1.75)

        normal_result = normal_engine.run(
            days, bars_frame(rows), lambda d: candidates.get(d, []), INITIAL_CASH
        )
        pessimistic_result = pessimistic_engine.run(
            days, bars_frame(rows), lambda d: candidates.get(d, []), INITIAL_CASH
        )

        normal_trade = normal_result.trades[0]
        pessimistic_trade = pessimistic_result.trades[0]
        # Buy side: higher slippage means a higher (worse) entry price.
        assert pessimistic_trade.entry_price > normal_trade.entry_price
        # Sell side (end-of-backtest liquidation): higher slippage means a
        # lower (worse) exit price.
        assert pessimistic_trade.exit_price < normal_trade.exit_price
        assert pessimistic_result.final_equity < normal_result.final_equity

    def test_forced_liquidation_exit_also_scales_with_multiplier(self, settings):
        max_hold = settings.trade_plan.max_hold_days
        days = LONG_TRADING_DAYS[: max_hold + 3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        normal_engine = self._engine_with_multiplier(settings, 1.0)
        pessimistic_engine = self._engine_with_multiplier(settings, 1.75)

        normal_result = normal_engine.run(
            days, bars_frame(rows), lambda d: candidates.get(d, []), INITIAL_CASH
        )
        pessimistic_result = pessimistic_engine.run(
            days, bars_frame(rows), lambda d: candidates.get(d, []), INITIAL_CASH
        )

        normal_hold_trade = next(
            t for t in normal_result.trades if t.exit_reason == "max_hold"
        )
        pessimistic_hold_trade = next(
            t for t in pessimistic_result.trades if t.exit_reason == "max_hold"
        )
        assert pessimistic_hold_trade.exit_price < normal_hold_trade.exit_price
        assert pessimistic_result.final_equity < normal_result.final_equity

    def test_extreme_multiplier_completes_without_crashing(self, settings):
        days = TRADING_DAYS[:3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}
        engine = self._engine_with_multiplier(settings, 10.0)

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates.get(d, []), INITIAL_CASH
        )

        assert result.final_equity >= 0


class TestRiskAdjustedMetricsWiring:
    """P2-07: BacktestEngine.run() populates the new BacktestResult fields."""

    def test_trade_count_matches_len_trades(self, engine):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates.get(d, []), INITIAL_CASH
        )

        assert result.trade_count == len(result.trades)

    def test_filled_trade_records_initial_stop_price(self, engine):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates.get(d, []), INITIAL_CASH
        )

        assert len(result.trades) == 1
        assert result.trades[0].initial_stop_price is not None
        assert result.trades[0].initial_stop_price < result.trades[0].entry_price
        assert result.trades[0].risk_basis_price == pytest.approx(100.0)

    def test_no_trades_reports_insufficient_sample_warning_and_none_metrics(
        self, engine
    ):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]

        result = engine.run(days, bars_frame(rows), _no_candidates, INITIAL_CASH)

        assert result.trade_count == 0
        assert result.win_rate is None
        assert result.profit_factor is None
        assert result.expectancy_per_trade is None
        assert result.avg_r_multiple is None
        assert any("統計的に不十分" in w for w in result.warnings)

    def test_empty_trading_calendar_still_populates_metric_fields(self, engine):
        result = engine.run([], bars_frame([]), _no_candidates, INITIAL_CASH)

        assert result.trade_count == 0
        assert result.sharpe is None
        assert result.max_drawdown_pct == pytest.approx(0.0)
        assert any("統計的に不十分" in w for w in result.warnings)


class TestEdgeCasesAndDefensiveBranches:
    def test_duplicate_candidate_for_an_already_open_symbol_is_ignored(self, engine):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        bars = bars_frame(rows)
        candidates_by_day = {
            days[1]: [_candidate("AAA", rank=1, as_of=days[1])],
            days[2]: [_candidate("AAA", rank=1, as_of=days[2])],
        }

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        # Only one entry into AAA, despite two days queuing a candidate for it.
        end_trades = [t for t in result.trades if t.exit_reason == "end_of_backtest"]
        assert len(end_trades) == 1

    def test_insufficient_cash_for_the_sized_position_skips_the_entry(self, engine):
        days = TRADING_DAYS[:3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        bars = bars_frame(rows)
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), initial_cash=0.01
        )

        assert result.trades == ()

    def test_zero_atr_making_stop_invalid_skips_the_entry(self, engine):
        days = TRADING_DAYS[:3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        bars = bars_frame(rows)
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=0.0, as_of=days[1])]}

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        assert result.trades == ()

    def test_missing_final_bar_uses_latest_close_and_forces_liquidation(
        self, settings, engine
    ):
        days = TRADING_DAYS[:4]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100, 101, 99, 100)),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
            bar_row("AAA", days[2], (100, 101, 99, 100)),
            # No bar for AAA on days[3] — a data gap after the position opened.
        ]
        bars = bars_frame(rows)
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=1.0, as_of=days[1])]}

        result = engine.run(
            days, bars, lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        [trade] = result.trades
        assert trade.exit_reason == "end_of_backtest"
        assert trade.exit_date == days[-1]
        assert trade.exit_price == pytest.approx(
            100.0 * (1 - settings.backtest.slippage_pct)
        )
        assert result.final_equity == pytest.approx(INITIAL_CASH + trade.pnl)

    def test_missing_benchmark_bar_carries_forward_latest_close(self, engine):
        days = TRADING_DAYS[:3]
        rows = [
            *_spy_bars(days[:2]),
            *flat_bars("AAA", days, 100.0),
        ]

        result = engine.run(days, bars_frame(rows), _no_candidates, INITIAL_CASH)

        assert result.benchmark_curve[-1][1] == pytest.approx(
            result.benchmark_curve[-2][1]
        )
        assert result.benchmark_final_equity == pytest.approx(
            result.benchmark_curve[-1][1]
        )


class _RecordingPolicy:
    """Fake `EntryPolicy` that records exactly what the engine asked it."""

    def __init__(
        self,
        *,
        blocked: frozenset[str] = frozenset(),
        reject_reason: str = ENTRY_BLOCK_REGIME,
    ) -> None:
        self.requests: list[EntryPolicyRequest] = []
        self._blocked = blocked
        self._reject_reason = reject_reason

    def decide(self, request: EntryPolicyRequest) -> dict[str, EntryDecision]:
        self.requests.append(request)
        return {
            candidate.symbol: (
                EntryDecision(is_allowed=False, reject_reason=self._reject_reason)
                if candidate.symbol in self._blocked
                else EntryDecision(is_allowed=True)
            )
            for candidate in request.candidates
        }


def _sized(
    settings: Settings, equity: float, *, price: float = 100.0, atr14: float = 2.0
) -> int:
    limit_price = price + settings.trade_plan.entry_limit_atr_multiple * atr14
    stop_price = price - settings.trade_plan.exit_atr_multiple * atr14
    return calc_position_size(
        equity,
        limit_price,
        stop_price,
        settings.backtest.sim_position_cap_pct,
        settings.backtest.sim_trade_risk_pct,
    ).shares


class TestEquityBasedSizing:
    """Issue #184: the sizing basis is equity, not the shrinking cash balance."""

    def test_second_entry_sizes_from_equity_not_remaining_cash(self, settings, engine):
        days = TRADING_DAYS[:6]
        rows = [
            *_spy_bars(days),
            *flat_bars("AAA", days, 100.0),
            *flat_bars("BBB", days, 100.0),
        ]
        candidates_by_day = {
            days[0]: [_candidate("AAA", as_of=days[0])],
            days[1]: [_candidate("BBB", as_of=days[1])],
        }

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        slippage = settings.backtest.slippage_pct
        commission = settings.backtest.commission_pct
        entry_price = 100.0 * (1 + slippage)
        first_shares = _sized(settings, INITIAL_CASH)
        cash_after_first = INITIAL_CASH - first_shares * entry_price * (1 + commission)
        # AAA is marked at the signal day's close, so the second fill is sized
        # against the whole account, not against what is left in cash.
        equity_basis = cash_after_first + first_shares * 100.0
        second_shares = _sized(settings, equity_basis)
        cash_basis_shares = _sized(settings, cash_after_first)

        trades = {trade.symbol: trade for trade in result.trades}
        assert second_shares > cash_basis_shares  # the regression this pins
        assert trades["BBB"].shares == second_shares

        # Hand-calculated exact final equity: both positions survive to the
        # end and are liquidated at the last close, with adverse slippage and
        # commission applied on that exit as well as on both entries.
        cash_after_second = cash_after_first - second_shares * entry_price * (
            1 + commission
        )
        exit_price = 100.0 * (1 - slippage)
        proceeds = (first_shares + second_shares) * exit_price * (1 - commission)
        assert result.final_equity == pytest.approx(cash_after_second + proceeds)

    def test_equity_basis_uses_the_signal_days_close_not_the_fill_days(
        self, settings, engine
    ):
        days = TRADING_DAYS[:5]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100, 101, 99, 100)),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
            # A violent mark-up on BBB's own fill day. Sizing BBB against it
            # would be look-ahead: at that day's open it has not happened yet.
            bar_row("AAA", days[2], (300, 301, 299, 300)),
            bar_row("AAA", days[3], (300, 301, 299, 300)),
            bar_row("AAA", days[4], (300, 301, 299, 300)),
            *flat_bars("BBB", days, 100.0),
        ]
        candidates_by_day = {
            days[0]: [_candidate("AAA", as_of=days[0])],
            days[1]: [_candidate("BBB", as_of=days[1])],
        }

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        commission = settings.backtest.commission_pct
        entry_price = 100.0 * (1 + settings.backtest.slippage_pct)
        first_shares = _sized(settings, INITIAL_CASH)
        cash_after_first = INITIAL_CASH - first_shares * entry_price * (1 + commission)
        as_of_signal_day = _sized(settings, cash_after_first + first_shares * 100.0)
        as_of_fill_day = _sized(settings, cash_after_first + first_shares * 300.0)

        trades = {trade.symbol: trade for trade in result.trades}
        assert as_of_fill_day > as_of_signal_day
        assert trades["BBB"].shares == as_of_signal_day


class TestEntryPolicyInjection:
    def test_blocked_candidate_never_fills_and_is_counted_under_its_reason(
        self, settings
    ):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        policy = _RecordingPolicy(blocked=frozenset({"AAA"}))
        candidates_by_day = {days[1]: [_candidate("AAA", as_of=days[1])]}

        result = BacktestEngine(settings, policy).run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        assert result.trades == ()
        assert dict(result.entry_block_counts)[ENTRY_BLOCK_REGIME] == 1
        assert dict(result.entry_block_days)[ENTRY_BLOCK_REGIME] == 1
        assert result.final_equity == pytest.approx(INITIAL_CASH)

    def test_policy_is_asked_as_of_the_signal_day_with_the_simulated_equity(
        self, settings
    ):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        policy = _RecordingPolicy(blocked=frozenset({"AAA"}))
        candidates_by_day = {days[1]: [_candidate("AAA", as_of=days[1])]}

        BacktestEngine(settings, policy).run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        assert len(policy.requests) == 1
        request = policy.requests[0]
        # The fill happens on days[2]; the gate is evaluated on days[1].
        assert request.as_of == days[1]

    def test_simulation_risk_budget_is_read_from_backtest_settings(self, settings):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        sim_risk_pct = settings.backtest.sim_trade_risk_pct / 2
        custom_settings = settings.model_copy(
            update={
                "backtest": settings.backtest.model_copy(
                    update={"sim_trade_risk_pct": sim_risk_pct}
                )
            }
        )
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=20.0, as_of=days[1])]}

        result = BacktestEngine(custom_settings).run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        limit_price = 100.0
        stop_price = 100.0 - custom_settings.trade_plan.exit_atr_multiple * 20.0
        expected = calc_position_size(
            INITIAL_CASH,
            limit_price,
            stop_price,
            custom_settings.backtest.sim_position_cap_pct,
            sim_risk_pct,
        ).shares
        assert result.trades[0].shares == expected
        assert expected < _sized(settings, INITIAL_CASH, atr14=20.0)


class TestEntryInstrumentation:
    def test_every_known_reason_is_reported_even_when_it_never_fired(self, engine):
        days = TRADING_DAYS[:3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]

        result = engine.run(days, bars_frame(rows), _no_candidates, INITIAL_CASH)

        assert dict(result.entry_block_counts) == dict.fromkeys(ENTRY_BLOCK_REASONS, 0)
        assert dict(result.entry_block_days) == dict.fromkeys(ENTRY_BLOCK_REASONS, 0)

    def test_repeated_candidate_for_a_held_symbol_counts_as_already_held(self, engine):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates_by_day = {
            days[1]: [_candidate("AAA", as_of=days[1])],
            days[2]: [_candidate("AAA", as_of=days[2])],
        }

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        assert dict(result.entry_block_counts)[ENTRY_BLOCK_ALREADY_HELD] == 1

    def test_candidate_beyond_the_concurrency_cap_counts_as_max_concurrent(
        self, settings, engine
    ):
        days = TRADING_DAYS[:3]
        symbols = [f"SYM{index}" for index in range(20)]
        rows = list(_spy_bars(days))
        for symbol in symbols:
            rows += flat_bars(symbol, days, 10.0)
        candidates_by_day = {
            days[1]: [
                _candidate(symbol, atr14=0.5, rank=index + 1, as_of=days[1])
                for index, symbol in enumerate(symbols)
            ]
        }

        result = engine.run(
            days,
            bars_frame(rows),
            lambda d: candidates_by_day.get(d, []),
            initial_cash=1_000_000.0,
        )

        max_concurrent = settings.backtest.max_concurrent_positions
        counts = dict(result.entry_block_counts)
        assert counts[ENTRY_BLOCK_MAX_CONCURRENT] == len(symbols) - max_concurrent
        assert result.max_concurrent_reached == max_concurrent

    def test_position_sized_beyond_the_cash_balance_counts_as_insufficient_cash(
        self, engine
    ):
        # Equity-based sizing can outrun the cash balance once open positions
        # are marked up: 10% of a $1.08M equity is more than the ~$90k left in
        # cash. The entry is skipped, not filled on credit.
        days = TRADING_DAYS[:5]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[0], (100, 101, 99, 100)),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
            bar_row("AAA", days[2], (10_000, 10_001, 9_999, 10_000)),
            bar_row("AAA", days[3], (10_000, 10_001, 9_999, 10_000)),
            bar_row("AAA", days[4], (10_000, 10_001, 9_999, 10_000)),
            *flat_bars("BBB", days, 100.0),
        ]
        candidates_by_day = {
            days[0]: [_candidate("AAA", as_of=days[0])],
            days[2]: [_candidate("BBB", as_of=days[2])],
        }

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        assert {trade.symbol for trade in result.trades} == {"AAA"}
        assert dict(result.entry_block_counts)[ENTRY_BLOCK_INSUFFICIENT_CASH] == 1

    def test_missing_fill_day_bar_counts_as_missing_data(self, engine):
        days = TRADING_DAYS[:3]
        rows = [
            *_spy_bars(days),
            bar_row("AAA", days[1], (100, 101, 99, 100)),
        ]
        candidates_by_day = {days[1]: [_candidate("AAA", as_of=days[1])]}

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        assert dict(result.entry_block_counts)[ENTRY_BLOCK_MISSING_DATA] == 1

    def test_zero_atr_counts_as_an_invalid_stop(self, engine):
        days = TRADING_DAYS[:3]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates_by_day = {days[1]: [_candidate("AAA", atr14=0.0, as_of=days[1])]}

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        assert dict(result.entry_block_counts)[ENTRY_BLOCK_INVALID_STOP] == 1

    def test_exposure_metrics_report_capital_deployment(self, engine):
        days = TRADING_DAYS[:4]
        rows = [*_spy_bars(days), *flat_bars("AAA", days, 100.0)]
        candidates_by_day = {days[0]: [_candidate("AAA", as_of=days[0])]}

        result = engine.run(
            days, bars_frame(rows), lambda d: candidates_by_day.get(d, []), INITIAL_CASH
        )

        assert result.max_concurrent_reached == 1
        # Day 0 is entirely in cash and the later days hold one ~10%
        # position, so the mean sits strictly between the two.
        assert result.avg_invested_pct is not None
        assert 0.0 < result.avg_invested_pct < 0.10

    def test_empty_calendar_reports_no_deployment(self, engine):
        result = engine.run([], bars_frame([]), _no_candidates, INITIAL_CASH)

        assert result.avg_invested_pct is None
        assert result.max_concurrent_reached == 0
        assert dict(result.entry_block_counts)[ENTRY_BLOCK_REGIME] == 0


class TestExitAtrPeriod:
    """Issue #194: `trade_plan.exit_atr_period` really drives the trailing stop.

    The bars ramp with a true range of exactly 2.0 every session, so the ATR
    is exactly 2.0 for *any* period until volatility collapses on session 20.
    From there the shorter period reacts faster, ratchets the stop higher, and
    closes a position that the 14-period stop keeps holding.
    """

    _SHORT_PERIOD = 5

    def _bars(self, days: list[date]) -> list[dict[str, object]]:
        rows = [*_spy_bars(days)]
        # Sessions 0..19: close +0.5/day with high/low +/-1.0 -> TR == 2.0.
        rows += [
            bar_row(
                "AAA",
                day,
                (
                    100.0 + 0.5 * index,
                    101.0 + 0.5 * index,
                    99.0 + 0.5 * index,
                    100.0 + 0.5 * index,
                ),
            )
            for index, day in enumerate(days[:20])
        ]
        # Session 20: volatility collapses to TR == 0.4 at an unchanged close.
        rows.append(bar_row("AAA", days[20], (109.5, 109.7, 109.3, 109.5)))
        # Session 21: gaps down to 105.20, between the two candidate stops.
        rows.append(bar_row("AAA", days[21], (105.2, 105.5, 105.0, 105.1)))
        rows.append(bar_row("AAA", days[22], (105.1, 105.3, 105.0, 105.1)))
        return rows

    def _run(self, settings: Settings, period: int) -> BacktestResult:
        days = TRADING_DAYS[:23]
        engine = BacktestEngine(
            settings.model_copy(
                update={
                    "trade_plan": settings.trade_plan.model_copy(
                        update={"exit_atr_period": period}
                    )
                }
            )
        )
        candidates_by_day = {days[15]: [_candidate("AAA", as_of=days[15])]}
        return engine.run(
            days,
            bars_frame(self._bars(days)),
            lambda d: candidates_by_day.get(d, []),
            INITIAL_CASH,
        )

    def test_shorter_period_ratchets_the_stop_higher_and_closes_the_position(
        self, settings
    ):
        # ATR(5) on session 20 = 2 + (0.4 - 2) / 5 = 1.68 -> stop
        # 109.50 - 4.20 = 105.30, which session 21's open (105.20) gaps
        # straight through, so the fill is the open and not the stop.
        result = self._run(settings, self._SHORT_PERIOD)

        slippage = settings.backtest.slippage_pct
        commission = settings.backtest.commission_pct
        entry_price = 108.0 * (1 + slippage)
        shares = _sized(settings, INITIAL_CASH, price=107.5)
        cash = INITIAL_CASH - shares * entry_price * (1 + commission)
        exit_price = 105.2 * (1 - slippage)

        assert [trade.exit_reason for trade in result.trades] == ["stop"]
        assert result.trades[0].exit_price == pytest.approx(exit_price)
        assert result.final_equity == pytest.approx(
            cash + shares * exit_price * (1 - commission)
        )

    def test_default_period_keeps_holding_and_liquidates_at_the_end(self, settings):
        # ATR(14) on session 20 = 2 + (0.4 - 2) / 14 = 1.8857... -> trailing
        # stop 104.7857..., below session 21's low (105.00): exactly the
        # pre-Issue-#194 hardcoded-14 behaviour, pinned as the baseline.
        assert settings.trade_plan.exit_atr_period == 14

        result = self._run(settings, settings.trade_plan.exit_atr_period)

        slippage = settings.backtest.slippage_pct
        commission = settings.backtest.commission_pct
        entry_price = 108.0 * (1 + slippage)
        shares = _sized(settings, INITIAL_CASH, price=107.5)
        cash = INITIAL_CASH - shares * entry_price * (1 + commission)
        exit_price = 105.1 * (1 - slippage)

        assert [trade.exit_reason for trade in result.trades] == ["end_of_backtest"]
        assert result.trades[0].exit_price == pytest.approx(exit_price)
        assert result.final_equity == pytest.approx(
            cash + shares * exit_price * (1 - commission)
        )
