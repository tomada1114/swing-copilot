"""Tests for BacktestEngine: no look-ahead, fills, stops, costs, benchmark (FR-10)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from swing_copilot.backtest.engine import BacktestEngine, Trade
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
        expected_stop = expected_entry_price - settings.backtest.exit_atr_multiple * 2.0
        expected_shares = calc_position_size(
            INITIAL_CASH,
            expected_entry_price,
            expected_stop,
            settings.risk.max_position_pct,
            settings.risk.max_trade_risk_pct,
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
        raw_stop = 100.0 * 1.001 - 2.5 * 1.0
        assert stop_trades[0].exit_price == pytest.approx(raw_stop * (1 - 0.001))
        assert stop_trades[0].exit_date == days[3]


class TestMaxHold:
    def test_forced_exit_at_max_hold_days(self, settings, engine):
        max_hold = settings.backtest.max_hold_days
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
                "backtest": settings.backtest.model_copy(update={"max_hold_days": 2})
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

    def test_concurrent_positions_capped_by_max_position_pct(self, settings, engine):
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

        max_concurrent = max(1, int(1 / settings.risk.max_position_pct))
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

        entry = 100.0 * (1 + settings.backtest.slippage_pct)
        stop = entry - settings.backtest.exit_atr_multiple
        shares = calc_position_size(
            INITIAL_CASH,
            entry,
            stop,
            settings.risk.max_position_pct,
            settings.risk.max_trade_risk_pct,
        ).shares
        entry_cost = shares * entry * (1 + settings.backtest.commission_pct)
        exit_price = 100.0 * (1 - settings.backtest.slippage_pct)
        exit_proceeds = shares * exit_price * (1 - settings.backtest.commission_pct)
        assert result.final_equity == pytest.approx(
            INITIAL_CASH - entry_cost + exit_proceeds
        )
        assert result.equity_curve[-1][1] == pytest.approx(result.final_equity)


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
        max_hold = settings.backtest.max_hold_days
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

    def test_missing_bar_on_a_held_symbol_is_skipped_gracefully(self, engine):
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

        # No crash; the position simply isn't marked-to-market or liquidated
        # on the day its bar is missing.
        assert result.final_equity > 0
