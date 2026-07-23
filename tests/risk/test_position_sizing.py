"""Tests for calc_position_size (FR-06, P1-03 sizing breakdown, P1-04 Fraction floor)."""

from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

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


class TestFractionFloorRegressions:
    """P1-04 (Issue #13) worked examples, verbatim from the issue body."""

    def test_p1_04_example_1_happy_path_exact_floor(self):
        # Concrete Examples / Example 1: equity=100000, risk_pct=1.0%,
        # entry=33.33, stop=30.00 -> risk_per_share=3.33,
        # risk_budget=Fraction(100000)*Fraction(0.01), shares=floor(...)=300.
        result = calc_position_size(
            account_equity=100_000,
            entry_price=33.33,
            stop_price=30.00,
            max_position_pct=0.25,
            max_trade_risk_pct=0.01,
        )
        assert result.shares_by_risk == 300
        risk_per_share = 33.33 - 30.00
        risk_budget = Fraction(100_000) * Fraction(0.01)
        assert Fraction(result.shares_by_risk) * Fraction(risk_per_share) <= risk_budget

    def test_p1_04_example_2_extreme_small_account_zero_shares_no_exception(self):
        # Boundary Condition / Example 2: account_equity=1, risk_pct=0.0001
        # (=0.01% as a fraction), risk_per_share=5 -> risk_budget=1/10000,
        # shares=0 and no exception (0 * risk_per_share <= risk_budget holds
        # trivially).
        result = calc_position_size(
            account_equity=1,
            entry_price=10.0,
            stop_price=5.0,
            max_position_pct=0.25,
            max_trade_risk_pct=0.0001,
        )
        assert result.shares_by_risk == 0
        assert result.shares == 0


class TestFractionFloorInvariant:
    """P1-04 (Issue #13) REQ-001/REQ-002: exact Fraction floor, property-based."""

    @settings(max_examples=500)
    # REQ-002 boundary conditions, forced in on every run regardless of what
    # Hypothesis's random search happens to generate.
    @example(
        account_equity=1e12,
        max_trade_risk_pct=0.01,
        max_position_pct=0.10,
        entry_price=100.0,
        stop_fraction=0.95,
    )
    @example(
        account_equity=1.0,
        max_trade_risk_pct=0.000001,  # 0.0001%
        max_position_pct=0.10,
        entry_price=100.0,
        stop_fraction=0.95,
    )
    @given(
        account_equity=st.floats(
            min_value=1.0, max_value=1e12, allow_nan=False, allow_infinity=False
        ),
        max_trade_risk_pct=st.floats(
            min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False
        ),
        max_position_pct=st.floats(
            min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False
        ),
        entry_price=st.floats(
            min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False
        ),
        stop_fraction=st.floats(
            min_value=0.0, max_value=0.999, allow_nan=False, allow_infinity=False
        ),
    )
    def test_shares_times_risk_per_share_never_exceeds_risk_budget(
        self,
        account_equity: float,
        max_trade_risk_pct: float,
        max_position_pct: float,
        entry_price: float,
        stop_fraction: float,
    ) -> None:
        stop_price = entry_price * stop_fraction
        assume(stop_price < entry_price)

        result = calc_position_size(
            account_equity=account_equity,
            entry_price=entry_price,
            stop_price=stop_price,
            max_position_pct=max_position_pct,
            max_trade_risk_pct=max_trade_risk_pct,
        )

        risk_per_share = entry_price - stop_price
        risk_budget = Fraction(account_equity) * Fraction(max_trade_risk_pct)
        assert Fraction(result.shares_by_risk) * Fraction(risk_per_share) <= risk_budget

        position_budget = Fraction(account_equity) * Fraction(max_position_pct)
        assert (
            Fraction(result.shares_by_position_cap) * Fraction(entry_price)
            <= position_budget
        )
