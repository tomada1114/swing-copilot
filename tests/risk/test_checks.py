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

    def test_not_calculable_when_atr_is_zero(self, checker):
        result = checker.check(
            [_candidate("AAPL", atr14=0.0)], portfolio=[], account_equity=100_000.0
        )
        assert result[0].status == "not_calculable"
        assert result[0].max_shares is None

    def test_not_calculable_when_candidate_metrics_missing_price_data(self, checker):
        candidate = Candidate(
            symbol="AAPL", as_of=AS_OF, signal_names=(), metrics={}, rank=1
        )
        result = checker.check([candidate], portfolio=[], account_equity=100_000.0)
        assert result[0].status == "not_calculable"


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
