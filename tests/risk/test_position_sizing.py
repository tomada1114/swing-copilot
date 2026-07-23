"""Tests for calc_position_size (FR-06, P1-03 sizing breakdown)."""

from __future__ import annotations

import pytest

from swing_copilot.risk.position_sizing import PositionSizeResult, calc_position_size


class TestCalcPositionSize:
    def test_risk_based_cap_binds_when_tighter(self):
        # Risk cap: (100_000 * 0.01) / (100 - 95) = 200 shares
        # Position cap: (100_000 * 0.10) / 100 = 100 shares  <- binds
        result = calc_position_size(
            account_equity=100_000,
            entry_price=100.0,
            stop_price=95.0,
            max_position_pct=0.10,
            max_trade_risk_pct=0.01,
        )
        assert result.shares_by_risk == 200
        assert result.shares_by_position_cap == 100
        assert result.shares == 100

    def test_position_based_cap_binds_when_tighter(self):
        # Risk cap: (100_000 * 0.01) / (100 - 99) = 1000 shares  <- binds
        # Position cap: (100_000 * 0.10) / 100 = 100 shares
        result = calc_position_size(
            account_equity=100_000,
            entry_price=100.0,
            stop_price=99.0,
            max_position_pct=0.10,
            max_trade_risk_pct=0.01,
        )
        assert result.shares_by_risk == 1000
        assert result.shares_by_position_cap == 100
        assert result.shares == 100

    def test_result_is_floored_to_whole_shares(self):
        result = calc_position_size(
            account_equity=10_050,
            entry_price=100.0,
            stop_price=95.0,
            max_position_pct=0.10,
            max_trade_risk_pct=0.01,
        )
        # position cap: (10_050 * 0.10) / 100 = 10.05 -> floor 10
        assert result.shares_by_position_cap == 10
        assert result.shares == 10
        assert isinstance(result.shares, int)
        assert isinstance(result, PositionSizeResult)

    def test_raises_when_stop_price_at_or_above_entry_price(self):
        with pytest.raises(ValueError, match="stop_price"):
            calc_position_size(
                account_equity=100_000,
                entry_price=100.0,
                stop_price=100.0,
                max_position_pct=0.10,
                max_trade_risk_pct=0.01,
            )

    def test_raises_when_entry_price_not_positive(self):
        with pytest.raises(ValueError, match="entry_price"):
            calc_position_size(
                account_equity=100_000,
                entry_price=0.0,
                stop_price=-1.0,
                max_position_pct=0.10,
                max_trade_risk_pct=0.01,
            )

    def test_issue_example_1_trade_risk_binds(self):
        # equity=100000, risk_pct=1%->risk_budget=1000, entry=50, stop=45
        # ->risk_per_share=5, max_position_pct=25%
        result = calc_position_size(
            account_equity=100_000,
            entry_price=50.0,
            stop_price=45.0,
            max_position_pct=0.25,
            max_trade_risk_pct=0.01,
        )
        assert result.shares_by_risk == 200
        assert result.shares_by_position_cap == 500
        assert result.shares == 200

    def test_issue_example_2_position_cap_binds(self):
        # Same as example 1 but max_position_pct=2%.
        result = calc_position_size(
            account_equity=100_000,
            entry_price=50.0,
            stop_price=45.0,
            max_position_pct=0.02,
            max_trade_risk_pct=0.01,
        )
        assert result.shares_by_risk == 200
        assert result.shares_by_position_cap == 40
        assert result.shares == 40

    def test_tie_produces_identical_intermediate_values_deterministically(self):
        # Risk cap: (100_000 * 0.01) / (100 - 90) = 100 shares
        # Position cap: (100_000 * 0.10) / 100 = 100 shares  <- tie
        # RiskChecker breaks the tie toward trade_risk; calc_position_size
        # itself just reports both equal intermediate values.
        results = [
            calc_position_size(
                account_equity=100_000,
                entry_price=100.0,
                stop_price=90.0,
                max_position_pct=0.10,
                max_trade_risk_pct=0.01,
            )
            for _ in range(5)
        ]
        assert all(result.shares_by_risk == 100 for result in results)
        assert all(result.shares_by_position_cap == 100 for result in results)
        assert all(result.shares == 100 for result in results)
