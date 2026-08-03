"""Hand-calculated fixture tests for backtest/metrics.py (Issue #16, P2-07)."""

from __future__ import annotations

import math
import statistics
from datetime import date

import pytest

from swing_copilot.backtest.engine import Trade
from swing_copilot.backtest.metrics import (
    LOOKAHEAD_SUSPICION_WARNING,
    HoldingDaysStats,
    compute_avg_r_multiple,
    compute_expectancy_per_trade,
    compute_max_drawdown_pct,
    compute_profit_factor,
    compute_reliability_warnings,
    compute_sharpe,
    compute_win_rate,
    exit_reason_breakdown,
    holding_days_stats,
    max_hold_binding_rate,
)
from swing_copilot.config import BacktestConfig

_D0 = date(2027, 1, 1)
_D1 = date(2027, 1, 2)


def _trade(
    *,
    pnl: float,
    shares: int = 10,
    initial_stop_price: float | None = 90.0,
    entry_price: float = 100.0,
) -> Trade:
    exit_price = entry_price + pnl / shares
    return Trade(
        symbol="AAA",
        entry_date=_D0,
        entry_price=entry_price,
        exit_date=_D1,
        exit_price=exit_price,
        shares=shares,
        exit_reason="stop",
        initial_stop_price=initial_stop_price,
    )


class TestComputeSharpe:
    def test_hand_calculated_returns_match_stdlib_formula(self):
        # Equity path 100 -> 110 -> 121 -> 108.9 gives clean returns:
        # r1 = 110/100 - 1 = 0.10, r2 = 121/110 - 1 = 0.10, r3 = 108.9/121 - 1 = -0.10
        equity_curve = (
            (_D0, 100.0),
            (_D1, 110.0),
            (date(2027, 1, 3), 121.0),
            (date(2027, 1, 4), 108.9),
        )
        returns = [0.10, 0.10, -0.10]
        expected = (
            statistics.fmean(returns) / statistics.stdev(returns) * math.sqrt(252)
        )

        assert compute_sharpe(equity_curve) == pytest.approx(expected)

    def test_single_return_is_none(self):
        equity_curve = ((_D0, 100.0), (_D1, 110.0))
        assert compute_sharpe(equity_curve) is None

    def test_empty_curve_is_none(self):
        assert compute_sharpe(()) is None

    def test_zero_variance_returns_is_none(self):
        # Every daily return is identical (+10%): stdev == 0, ratio undefined.
        equity_curve = (
            (_D0, 100.0),
            (_D1, 110.0),
            (date(2027, 1, 3), 121.0),
        )
        assert compute_sharpe(equity_curve) is None


class TestComputeMaxDrawdownPct:
    def test_hand_calculated_peak_to_trough(self):
        # Peak 120 at day 2, trough 90 at day 3: (120-90)/120 = 0.25.
        # Day 4's 110 is a smaller drawdown (120-110)/120 = 0.0833, ignored.
        equity_curve = (
            (_D0, 100.0),
            (_D1, 120.0),
            (date(2027, 1, 3), 90.0),
            (date(2027, 1, 4), 110.0),
        )
        assert compute_max_drawdown_pct(equity_curve) == pytest.approx(0.25)

    def test_monotonically_rising_curve_is_zero(self):
        equity_curve = ((_D0, 100.0), (_D1, 110.0), (date(2027, 1, 3), 120.0))
        assert compute_max_drawdown_pct(equity_curve) == pytest.approx(0.0)

    def test_empty_curve_is_zero(self):
        assert compute_max_drawdown_pct(()) == pytest.approx(0.0)


class TestWinRateProfitFactorExpectancy:
    def test_hand_calculated_mixed_win_loss(self):
        # 3 winners @ +200, 2 losers @ -100: win_rate = 3/5 = 0.6,
        # profit_factor = 600/200 = 3.0, expectancy = (600-200)/5 = 80.0.
        trades = (
            _trade(pnl=200.0),
            _trade(pnl=200.0),
            _trade(pnl=200.0),
            _trade(pnl=-100.0),
            _trade(pnl=-100.0),
        )

        assert compute_win_rate(trades) == pytest.approx(0.6)
        assert compute_profit_factor(trades) == pytest.approx(3.0)
        assert compute_expectancy_per_trade(trades) == pytest.approx(80.0)

    def test_zero_pnl_trade_counts_as_neutral_not_a_win(self):
        trades = (_trade(pnl=100.0), _trade(pnl=0.0))
        # 1 win / 2 trades = 0.5; the zero-pnl trade is in the denominator
        # but not the win numerator.
        assert compute_win_rate(trades) == pytest.approx(0.5)

    def test_no_losing_trades_profit_factor_is_none(self):
        trades = (_trade(pnl=100.0), _trade(pnl=50.0))
        assert compute_profit_factor(trades) is None

    def test_empty_trades_all_none(self):
        assert compute_win_rate(()) is None
        assert compute_profit_factor(()) is None
        assert compute_expectancy_per_trade(()) is None


class TestAvgRMultiple:
    def test_hand_calculated_symmetric_win_and_loss(self):
        # Trade 1: entry=100, stop=90, shares=10, pnl=+200 -> R = 200/(10*10) = 2.0
        # Trade 2: entry=50, stop=45, shares=20, pnl=-200 -> R = -200/(5*20) = -2.0
        # Trade 3: no initial_stop_price recorded -> omitted from the average.
        trades = (
            _trade(pnl=200.0, entry_price=100.0, initial_stop_price=90.0, shares=10),
            _trade(pnl=-200.0, entry_price=50.0, initial_stop_price=45.0, shares=20),
            _trade(pnl=999.0, initial_stop_price=None),
        )

        assert compute_avg_r_multiple(trades) == pytest.approx(0.0)

    def test_stop_at_or_above_entry_is_omitted(self):
        trade = _trade(pnl=100.0, entry_price=100.0, initial_stop_price=100.0)
        assert compute_avg_r_multiple((trade,)) is None

    def test_no_trades_is_none(self):
        assert compute_avg_r_multiple(()) is None


class TestComputeReliabilityWarnings:
    def test_below_insufficient_threshold_warns_statistically_insufficient(self):
        config = BacktestConfig()
        warnings = compute_reliability_warnings(29, 0.5, 0.10, config)
        assert any("統計的に不十分" in w for w in warnings)
        assert not any("予備的" in w for w in warnings)

    def test_at_insufficient_threshold_no_longer_statistically_insufficient(self):
        config = BacktestConfig()
        warnings = compute_reliability_warnings(30, 0.5, 0.10, config)
        assert not any("統計的に不十分" in w for w in warnings)
        assert any("予備的" in w for w in warnings)

    def test_below_preliminary_threshold_warns_preliminary(self):
        config = BacktestConfig()
        warnings = compute_reliability_warnings(99, 0.5, 0.10, config)
        assert any("予備的" in w for w in warnings)

    def test_at_preliminary_threshold_no_warning(self):
        config = BacktestConfig()
        warnings = compute_reliability_warnings(100, 0.5, 0.10, config)
        assert not any("予備的" in w for w in warnings)
        assert not any("統計的に不十分" in w for w in warnings)

    def test_zero_trades_is_none_metrics_and_insufficient_warning(self):
        config = BacktestConfig()
        warnings = compute_reliability_warnings(0, None, 0.0, config)
        assert any("統計的に不十分" in w for w in warnings)
        # No trades at all isn't a lookahead-bias red flag; it's just idle.
        assert LOOKAHEAD_SUSPICION_WARNING not in warnings

    @pytest.mark.parametrize(
        ("win_rate", "expect_warning"),
        [(0.899, False), (0.900, False), (0.901, True)],
    )
    def test_win_rate_boundary_is_strictly_greater_than(self, win_rate, expect_warning):
        config = BacktestConfig()
        # trade_count=200 keeps it above the preliminary threshold so only
        # the lookahead-suspicion warning is under test here.
        warnings = compute_reliability_warnings(200, win_rate, 0.10, config)
        assert (LOOKAHEAD_SUSPICION_WARNING in warnings) is expect_warning

    @pytest.mark.parametrize(
        ("max_drawdown_pct", "expect_warning"),
        [(0.011, False), (0.010, False), (0.009, True)],
    )
    def test_max_drawdown_boundary_is_strictly_less_than(
        self, max_drawdown_pct, expect_warning
    ):
        config = BacktestConfig()
        warnings = compute_reliability_warnings(200, 0.5, max_drawdown_pct, config)
        assert (LOOKAHEAD_SUSPICION_WARNING in warnings) is expect_warning


def _exit_trade(reason: str, days_held: int) -> Trade:
    return Trade(
        symbol="AAA",
        entry_date=_D0,
        entry_price=100.0,
        exit_date=_D1,
        exit_price=101.0,
        shares=10,
        exit_reason=reason,
        days_held=days_held,
    )


class TestExitReasonBreakdown:
    def test_counts_every_reason_including_the_absent_ones(self):
        trades = (
            _exit_trade("stop", 3),
            _exit_trade("stop", 4),
            _exit_trade("max_hold", 25),
        )

        assert exit_reason_breakdown(trades) == {
            "stop": 2,
            "max_hold": 1,
            "end_of_backtest": 0,
        }

    def test_reports_all_zeros_for_an_empty_trade_log(self):
        assert exit_reason_breakdown(()) == {
            "stop": 0,
            "max_hold": 0,
            "end_of_backtest": 0,
        }

    def test_counts_the_forced_final_liquidation_separately(self):
        trades = (_exit_trade("stop", 1), _exit_trade("end_of_backtest", 12))

        assert exit_reason_breakdown(trades)["end_of_backtest"] == 1


class TestMaxHoldBindingRate:
    def test_is_the_max_hold_share_of_all_exits(self):
        trades = (
            _exit_trade("max_hold", 25),
            _exit_trade("stop", 4),
            _exit_trade("stop", 5),
            _exit_trade("end_of_backtest", 9),
        )

        assert max_hold_binding_rate(trades) == 0.25

    def test_is_none_without_trades(self):
        assert max_hold_binding_rate(()) is None

    def test_is_one_when_every_exit_hit_max_hold(self):
        trades = (_exit_trade("max_hold", 25), _exit_trade("max_hold", 25))

        assert max_hold_binding_rate(trades) == 1.0


class TestHoldingDaysStats:
    def test_returns_hand_calculated_median_and_quartiles(self):
        # Sorted holding days: 1, 3, 5, 7, 9 -> median 5, p25 3, p75 7
        trades = tuple(_exit_trade("stop", days) for days in (5, 1, 9, 3, 7))

        assert holding_days_stats(trades) == HoldingDaysStats(
            median=5.0, p25=3.0, p75=7.0
        )

    def test_is_none_without_trades(self):
        assert holding_days_stats(()) is None

    def test_a_single_trade_collapses_every_quantile_onto_it(self):
        assert holding_days_stats((_exit_trade("stop", 6),)) == HoldingDaysStats(
            median=6.0, p25=6.0, p75=6.0
        )
