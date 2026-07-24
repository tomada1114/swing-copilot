"""Tests for RiskChecker: sizing, sector concentration, correlation (FR-06)."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pandas as pd
import pytest

from swing_copilot.models import Position
from swing_copilot.risk.checks import RiskChecker
from swing_copilot.screening.base import Candidate
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.universe import UniverseMember

AS_OF = date(2027, 1, 1)


def _member(symbol: str, sector: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol, company_name=symbol, gics_sector=sector, source_symbol=symbol
    )


def _candidate(symbol: str, *, close: float = 100.0, atr14: float = 2.0) -> Candidate:
    return Candidate(
        symbol=symbol,
        as_of=AS_OF,
        signal_names=("trend_sma",),
        metrics={
            "close": close,
            "atr14": atr14,
            "rsi14": 40.0,
            "avg_volume": 2_000_000.0,
        },
        rank=1,
    )


def _position(symbol: str, *, shares: int = 100, entry_price: float = 90.0) -> Position:
    return Position(
        position_id=uuid4(),
        symbol=symbol,
        is_paper=True,
        entry_date=date(2026, 12, 1),
        entry_price=entry_price,
        shares=shares,
        status="open",
    )


def _write_price_series(
    market_store: MarketStore, symbol: str, closes: list[float]
) -> None:
    start = AS_OF - timedelta(days=len(closes))
    rows = [
        {
            "symbol": symbol,
            "date": start + timedelta(days=i),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1_000_000,
            "provider": "test",
            "fetched_at": pd.Timestamp("2027-01-01", tz="UTC"),
        }
        for i, close in enumerate(closes)
    ]
    market_store.write_bars(pd.DataFrame(rows))


@pytest.fixture
def market_store(tmp_path):
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


@pytest.fixture
def checker(settings, market_store):
    universe = (
        _member("AAPL", "Information Technology"),
        _member("MSFT", "Information Technology"),
    )
    return RiskChecker(settings, universe, market_store)


class TestCheckSizing:
    def test_approved_with_calculable_position_size(self, checker):
        result = checker.check(
            [_candidate("AAPL")], portfolio=[], account_equity=100_000.0
        )
        assert result[0].status == "approved"
        assert result[0].max_shares is not None
        assert result[0].max_shares > 0
        assert result[0].stop_price == pytest.approx(100.0 - 2.5 * 2.0)

    def test_sizing_breakdown_is_populated_on_approval(self, checker):
        result = checker.check(
            [_candidate("AAPL")], portfolio=[], account_equity=100_000.0
        )
        assert result[0].shares_by_risk is not None
        assert result[0].shares_by_position_cap is not None
        assert result[0].max_shares == min(
            result[0].shares_by_risk, result[0].shares_by_position_cap
        )

    def test_approved_when_symbol_has_no_known_sector(self, settings, market_store):
        # An empty universe means the candidate's symbol isn't in the
        # sector map; sector concentration must simply be skipped, not error.
        checker = RiskChecker(settings, universe=(), market_store=market_store)
        result = checker.check(
            [_candidate("UNKNOWN")], portfolio=[], account_equity=100_000.0
        )
        assert result[0].status == "approved"

    def test_not_calculable_when_account_equity_missing(self, checker):
        result = checker.check([_candidate("AAPL")], portfolio=[], account_equity=None)
        assert result[0].status == "not_calculable"
        assert result[0].max_shares is None
        assert result[0].reasons
        # REQ-021: binding_constraint reflects the calculability failure, and
        # the specific reason is visible in `reasons`.
        assert result[0].binding_constraint == "not_calculable"

    def test_not_calculable_when_atr_is_zero(self, checker):
        result = checker.check(
            [_candidate("AAPL", atr14=0.0)], portfolio=[], account_equity=100_000.0
        )
        assert result[0].status == "not_calculable"
        assert result[0].max_shares is None
        assert result[0].binding_constraint == "not_calculable"
        assert result[0].reasons

    def test_not_calculable_when_candidate_metrics_missing_price_data(self, checker):
        candidate = Candidate(
            symbol="AAPL", as_of=AS_OF, signal_names=(), metrics={}, rank=1
        )
        result = checker.check([candidate], portfolio=[], account_equity=100_000.0)
        assert result[0].status == "not_calculable"
        assert result[0].binding_constraint == "not_calculable"
        assert result[0].reasons


class TestSectorConcentration:
    def test_rejected_when_sector_limit_exceeded(self, settings, market_store):
        universe = (_member("AAPL", "Information Technology"),)
        checker = RiskChecker(settings, universe, market_store)
        # Existing portfolio already holds a huge IT position relative to equity.
        portfolio = [_position("AAPL", shares=1000, entry_price=100.0)]

        result = checker.check(
            [_candidate("AAPL")], portfolio=portfolio, account_equity=10_000.0
        )

        assert result[0].status == "rejected"
        assert any("sector" in reason for reason in result[0].reasons)
        # Sector always wins as the binding constraint on rejection,
        # regardless of what shares_by_risk/shares_by_position_cap said.
        assert result[0].binding_constraint == "sector"

    def test_approved_when_sector_exposure_within_limit(self, checker):
        result = checker.check(
            [_candidate("AAPL")], portfolio=[], account_equity=1_000_000.0
        )
        assert result[0].status == "approved"


class TestCorrelationWarnings:
    def test_high_correlation_produces_warning_without_blocking_approval(
        self, settings, market_store
    ):
        # Two symbols moving in lockstep.
        closes = [100.0 + i for i in range(70)]
        _write_price_series(market_store, "AAPL", closes)
        _write_price_series(market_store, "MSFT", closes)
        universe = (
            _member("AAPL", "Information Technology"),
            _member("MSFT", "Information Technology"),
        )
        checker = RiskChecker(settings, universe, market_store)
        portfolio = [_position("MSFT")]

        result = checker.check(
            [_candidate("AAPL")], portfolio=portfolio, account_equity=1_000_000.0
        )

        assert result[0].status == "approved"
        assert len(result[0].warnings) == 1
        assert result[0].warnings[0].warning_type == "high_correlation"
        assert result[0].warnings[0].correlated_symbol == "MSFT"

    def test_low_correlation_produces_no_warning(self, settings, market_store):
        closes_up = [100.0 + i for i in range(70)]
        closes_down = [100.0 - 0.3 * i + (5 if i % 2 == 0 else -5) for i in range(70)]
        _write_price_series(market_store, "AAPL", closes_up)
        _write_price_series(market_store, "MSFT", closes_down)
        universe = (
            _member("AAPL", "Information Technology"),
            _member("MSFT", "Information Technology"),
        )
        checker = RiskChecker(settings, universe, market_store)
        portfolio = [_position("MSFT")]

        result = checker.check(
            [_candidate("AAPL")], portfolio=portfolio, account_equity=1_000_000.0
        )

        assert result[0].warnings == ()

    def test_no_portfolio_means_no_correlation_warnings(self, checker):
        result = checker.check(
            [_candidate("AAPL")], portfolio=[], account_equity=100_000.0
        )
        assert result[0].warnings == ()

    def test_insufficient_history_produces_data_quality_warning(
        self, settings, market_store
    ):
        _write_price_series(market_store, "AAPL", [100.0, 101.0, 102.0])
        _write_price_series(market_store, "MSFT", [100.0 + i for i in range(70)])
        universe = (
            _member("AAPL", "Information Technology"),
            _member("MSFT", "Information Technology"),
        )
        checker = RiskChecker(settings, universe, market_store)
        portfolio = [_position("MSFT")]

        result = checker.check(
            [_candidate("AAPL")], portfolio=portfolio, account_equity=1_000_000.0
        )

        assert len(result[0].warnings) == 1
        assert result[0].warnings[0].warning_type == "data_quality"

    def test_insufficient_history_on_the_held_position_side_is_a_data_quality_warning(
        self, settings, market_store
    ):
        _write_price_series(market_store, "AAPL", [100.0 + i for i in range(70)])
        _write_price_series(market_store, "MSFT", [100.0, 101.0, 102.0])
        universe = (
            _member("AAPL", "Information Technology"),
            _member("MSFT", "Information Technology"),
        )
        checker = RiskChecker(settings, universe, market_store)
        portfolio = [_position("MSFT")]

        result = checker.check(
            [_candidate("AAPL")], portfolio=portfolio, account_equity=1_000_000.0
        )

        assert len(result[0].warnings) == 1
        assert result[0].warnings[0].warning_type == "data_quality"

    def test_zero_variance_series_produces_data_quality_warning_not_a_crash(
        self, settings, market_store
    ):
        _write_price_series(market_store, "AAPL", [100.0] * 70)
        _write_price_series(market_store, "MSFT", [100.0] * 70)
        universe = (
            _member("AAPL", "Information Technology"),
            _member("MSFT", "Information Technology"),
        )
        checker = RiskChecker(settings, universe, market_store)
        portfolio = [_position("MSFT")]

        result = checker.check(
            [_candidate("AAPL")], portfolio=portfolio, account_equity=1_000_000.0
        )

        assert len(result[0].warnings) == 1
        assert result[0].warnings[0].warning_type == "data_quality"

    def test_misaligned_trading_dates_produce_data_quality_warning(
        self, settings, market_store
    ):
        lookback = settings.risk.correlation_lookback_days
        candidate_start = AS_OF - timedelta(days=lookback + 1)
        held_start = candidate_start + timedelta(days=1)
        rows = []
        for symbol, start in (("AAPL", candidate_start), ("MSFT", held_start)):
            for index in range(lookback + 1):
                close = 100.0 + index
                rows.append(
                    {
                        "symbol": symbol,
                        "date": start + timedelta(days=index),
                        "open": close,
                        "high": close + 1,
                        "low": close - 1,
                        "close": close,
                        "volume": 1_000_000,
                        "provider": "test",
                        "fetched_at": pd.Timestamp("2027-01-01", tz="UTC"),
                    }
                )
        market_store.write_bars(pd.DataFrame(rows))
        checker = RiskChecker(
            settings,
            (
                _member("AAPL", "Technology"),
                _member("MSFT", "Technology"),
            ),
            market_store,
        )

        result = checker.check(
            [_candidate("AAPL")],
            portfolio=[_position("MSFT")],
            account_equity=1_000_000.0,
        )

        assert result[0].warnings[0].warning_type == "data_quality"


def _checker_with_risk_overrides(settings, market_store, **overrides):
    universe = (_member("AAPL", "Information Technology"),)
    risk = settings.risk.model_copy(update=overrides)
    return RiskChecker(
        settings.model_copy(update={"risk": risk}), universe, market_store
    )


class TestBindingConstraint:
    """P1-03 (REQ-004): which constraint determined the final share count."""

    def test_issue_example_1_trade_risk_binds(self, settings, market_store):
        # equity=100000, risk_pct=1%->risk_budget=1000, entry=50, stop=45
        # (atr14=2.0)->risk_per_share=5, max_position_pct=25%.
        checker = _checker_with_risk_overrides(
            settings, market_store, max_position_pct=0.25
        )
        result = checker.check(
            [_candidate("AAPL", close=50.0, atr14=2.0)],
            portfolio=[],
            account_equity=100_000.0,
        )
        assert result[0].shares_by_risk == 200
        assert result[0].shares_by_position_cap == 500
        assert result[0].max_shares == 200
        assert result[0].binding_constraint == "trade_risk"

    def test_issue_example_2_position_cap_binds(self, settings, market_store):
        # Same as example 1 but max_position_pct=2%.
        checker = _checker_with_risk_overrides(
            settings, market_store, max_position_pct=0.02
        )
        result = checker.check(
            [_candidate("AAPL", close=50.0, atr14=2.0)],
            portfolio=[],
            account_equity=100_000.0,
        )
        assert result[0].shares_by_risk == 200
        assert result[0].shares_by_position_cap == 40
        assert result[0].max_shares == 40
        assert result[0].binding_constraint == "position_cap"

    def test_tie_deterministically_favors_trade_risk(self, settings, market_store):
        # Risk cap: (100_000 * 0.01) / (100 - 90) = 100 shares
        # Position cap: (100_000 * 0.10) / 100 = 100 shares  <- tie
        checker = _checker_with_risk_overrides(
            settings, market_store, max_position_pct=0.10, max_trade_risk_pct=0.01
        )
        candidate = _candidate(
            "AAPL", close=100.0, atr14=4.0
        )  # stop = 100 - 2.5*4 = 90

        for _ in range(5):
            result = checker.check([candidate], portfolio=[], account_equity=100_000.0)
            assert result[0].shares_by_risk == 100
            assert result[0].shares_by_position_cap == 100
            assert result[0].binding_constraint == "trade_risk"


class TestWideStopWarning:
    """P1-03 (REQ-030): stop distance boundary at wide_stop_threshold_pct (10.0%)."""

    def test_no_warning_at_exactly_the_threshold(self, checker):
        # entry=100, atr14=4.0 -> stop=90.0 -> distance=10.00% exactly.
        result = checker.check(
            [_candidate("AAPL", close=100.0, atr14=4.0)],
            portfolio=[],
            account_equity=100_000.0,
        )
        assert "WIDE_STOP" not in result[0].sizing_warnings

    def test_no_warning_just_below_the_threshold(self, checker):
        # entry=100, atr14=3.996 -> stop=90.01 -> distance=9.99%.
        result = checker.check(
            [_candidate("AAPL", close=100.0, atr14=3.996)],
            portfolio=[],
            account_equity=100_000.0,
        )
        assert "WIDE_STOP" not in result[0].sizing_warnings

    def test_warning_just_above_the_threshold(self, checker):
        # entry=100, atr14=4.004 -> stop=89.99 -> distance=10.01%.
        result = checker.check(
            [_candidate("AAPL", close=100.0, atr14=4.004)],
            portfolio=[],
            account_equity=100_000.0,
        )
        assert "WIDE_STOP" in result[0].sizing_warnings

    def test_issue_example_3_wide_stop(self, checker):
        # entry=50, stop=40 -> 20% stop distance, well above the threshold.
        # atr14 = 10 / 2.5 = 4.0.
        result = checker.check(
            [_candidate("AAPL", close=50.0, atr14=4.0)],
            portfolio=[],
            account_equity=100_000.0,
        )
        assert "WIDE_STOP" in result[0].sizing_warnings


class TestSmallAccountFrictionWarning:
    """P1-03 (REQ-020): floor-to-zero shares and extremely small risk budgets."""

    def test_issue_example_4_zero_shares_from_tight_position_cap(
        self, settings, market_store
    ):
        # equity=500, risk_pct=1%->risk_budget=5, entry=50, stop=45 (atr14=2.0)
        # ->risk_per_share=5->shares_by_risk=1. A very tight max_position_pct
        # floors shares_by_position_cap (and therefore final shares) to 0.
        checker = _checker_with_risk_overrides(
            settings, market_store, max_position_pct=0.001
        )
        result = checker.check(
            [_candidate("AAPL", close=50.0, atr14=2.0)],
            portfolio=[],
            account_equity=500.0,
        )
        assert result[0].shares_by_risk == 1
        assert result[0].max_shares == 0
        assert "SMALL_ACCOUNT_FRICTION" in result[0].sizing_warnings

    def test_fires_on_extremely_small_risk_budget_even_without_zero_shares(
        self, settings, market_store
    ):
        # risk_budget = 50 * 0.01 = $0.50, below the $1 judgment-call
        # threshold, even though a generous position cap keeps final shares
        # well above zero (isolates the risk-budget half of REQ-020 from the
        # floor-to-zero half).
        checker = _checker_with_risk_overrides(
            settings,
            market_store,
            max_trade_risk_pct=0.01,
            max_position_pct=1.0,
        )
        result = checker.check(
            [_candidate("AAPL", close=10.0, atr14=0.0004)],  # stop = 10 - 0.001
            portfolio=[],
            account_equity=50.0,
        )
        assert result[0].max_shares is not None
        assert result[0].max_shares > 0
        assert "SMALL_ACCOUNT_FRICTION" in result[0].sizing_warnings

    def test_no_friction_warning_for_a_healthy_account(self, checker):
        result = checker.check(
            [_candidate("AAPL")], portfolio=[], account_equity=100_000.0
        )
        assert "SMALL_ACCOUNT_FRICTION" not in result[0].sizing_warnings
