"""Tests for calc_position_size (FR-06)."""

from __future__ import annotations

import pytest

from swing_copilot.risk.position_sizing import calc_position_size


class TestCalcPositionSize:
    def test_risk_based_cap_binds_when_tighter(self):
        # Risk cap: (100_000 * 0.01) / (100 - 95) = 200 shares
        # Position cap: (100_000 * 0.10) / 100 = 100 shares  <- binds
        shares = calc_position_size(
            account_equity=100_000,
            entry_price=100.0,
            stop_price=95.0,
            max_position_pct=0.10,
            max_trade_risk_pct=0.01,
        )
        assert shares == 100

    def test_position_based_cap_binds_when_tighter(self):
        # Risk cap: (100_000 * 0.01) / (100 - 99) = 1000 shares  <- binds
        # Position cap: (100_000 * 0.10) / 100 = 100 shares
        shares = calc_position_size(
            account_equity=100_000,
            entry_price=100.0,
            stop_price=99.0,
            max_position_pct=0.10,
            max_trade_risk_pct=0.01,
        )
        assert shares == 100

    def test_result_is_floored_to_whole_shares(self):
        shares = calc_position_size(
            account_equity=10_050,
            entry_price=100.0,
            stop_price=95.0,
            max_position_pct=0.10,
            max_trade_risk_pct=0.01,
        )
        # position cap: (10_050 * 0.10) / 100 = 10.05 -> floor 10
        assert shares == 10
        assert isinstance(shares, int)

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
