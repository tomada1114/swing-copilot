"""Tests for account-independent symbol risk checks."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from swing_copilot.data.earnings import EarningsEvent, EarningsLookup
from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    DistributionResult,
)
from swing_copilot.regime.exposure import ExposureDecision, determine_exposure
from swing_copilot.regime.gate import GateVerdict, MarketGate, RegimeSnapshot
from swing_copilot.risk.checks import (
    EARNINGS_DATE_UNKNOWN_WARNING,
    EARNINGS_PROXIMITY_BLOCK_REASON,
    EARNINGS_PROXIMITY_WARN_WARNING,
    EARNINGS_RECENTLY_REPORTED_WARNING,
    REGIME_CASH_PRIORITY_REASON,
    RISK_WARNING_WIDE_STOP,
    EarningsGuardInput,
    RiskChecker,
    RiskRunContext,
)
from swing_copilot.screening.base import Candidate

if TYPE_CHECKING:
    from swing_copilot.config import Settings

AS_OF = date(2027, 1, 1)


def _candidate(
    symbol: str = "AAPL", *, close: float | None = 100.0, atr14: float | None = 2.0
) -> Candidate:
    metrics = {"rsi14": 40.0, "avg_volume": 2_000_000.0}
    if close is not None:
        metrics["close"] = close
    if atr14 is not None:
        metrics["atr14"] = atr14
    return Candidate(
        symbol=symbol,
        as_of=AS_OF,
        signal_names=("trend_sma",),
        metrics=metrics,
        rank=1,
    )


def _exposure(gate: GateVerdict, level: DistributionLevel) -> ExposureDecision:
    distribution = DistributionResult(0.0, 0.0, 0.0, level, DataQuality.OK)
    return determine_exposure(
        RegimeSnapshot(
            AS_OF,
            MarketGate(gate, 100.0, 90.0, 15.0),
            distribution,
            distribution,
            level,
            DataQuality.OK,
        )
    )


def _event(day: date) -> EarningsEvent:
    return EarningsEvent("AAPL", day, "amc", datetime(2027, 1, 1, tzinfo=UTC))


def _checker_with_lookup(settings: Settings, lookup: EarningsLookup) -> RiskChecker:
    return RiskChecker(
        settings,
        RiskRunContext(earnings_guard=EarningsGuardInput(True, {"AAPL": lookup})),
    )


class TestTradePlan:
    def test_approved_plan_exposes_close_limit_stop_atr_and_one_r(self, settings):
        result = RiskChecker(settings).check([_candidate()])[0]

        assert result.status == "approved"
        assert result.entry_price == pytest.approx(100.0)
        assert result.limit_price == pytest.approx(100.0)
        assert result.stop_price == pytest.approx(95.0)
        assert result.atr14 == pytest.approx(2.0)
        assert result.stop_distance_pct == pytest.approx(0.05)
        assert result.binding_constraint is None

    def test_public_plan_has_no_account_or_correlation_constraints(self, settings):
        result = RiskChecker(settings).check([_candidate()])[0]

        assert result.binding_constraint is None
        assert not hasattr(result, "shares_by_risk")
        assert not hasattr(result, "shares_by_position_cap")
        assert not hasattr(result, "correlation")
        assert not hasattr(result, "max_shares")

    def test_nonzero_limit_uses_worst_case_fill_for_one_r(self, settings):
        trade_plan = settings.trade_plan.model_copy(
            update={"entry_limit_atr_multiple": 0.3}
        )
        checker = RiskChecker(settings.model_copy(update={"trade_plan": trade_plan}))

        result = checker.check([_candidate(close=50.0, atr14=2.0)])[0]

        assert result.limit_price == pytest.approx(50.6)
        assert result.stop_price == pytest.approx(45.0)
        assert result.stop_distance_pct == pytest.approx((50.6 - 45.0) / 50.6)

    @pytest.mark.parametrize("missing", ["close", "atr14"])
    def test_missing_price_input_is_explicitly_not_calculable(self, settings, missing):
        candidate = _candidate(
            close=None if missing == "close" else 100.0,
            atr14=None if missing == "atr14" else 2.0,
        )

        result = RiskChecker(settings).check([candidate])[0]

        assert result.status == "not_calculable"
        assert result.stop_distance_pct is None
        assert result.binding_constraint == "not_calculable"

    def test_zero_atr_is_not_a_usable_stop(self, settings):
        result = RiskChecker(settings).check([_candidate(atr14=0.0)])[0]

        assert result.status == "not_calculable"
        assert result.stop_distance_pct is None
        assert result.binding_constraint == "not_calculable"

    def test_wide_stop_warning_depends_only_on_symbol_prices(self, settings):
        result = RiskChecker(settings).check([_candidate(atr14=5.0)])[0]

        assert result.stop_distance_pct == pytest.approx(0.125)
        assert result.warnings == (RISK_WARNING_WIDE_STOP,)

    def test_preserves_candidate_order(self, settings):
        results = RiskChecker(settings).check([_candidate("MSFT"), _candidate("AAPL")])

        assert [result.symbol for result in results] == ["MSFT", "AAPL"]


class TestMarketState:
    def test_cash_priority_keeps_plan_but_blocks_every_candidate(self, settings):
        result = RiskChecker(settings).check(
            [_candidate()], _exposure(GateVerdict.BEAR, DistributionLevel.NORMAL)
        )[0]

        assert result.status == "rejected"
        assert result.stop_distance_pct == pytest.approx(0.05)
        assert result.binding_constraint == "regime"
        assert result.reasons == (REGIME_CASH_PRIORITY_REASON,)

    @pytest.mark.parametrize("missing", ["close", "atr14"])
    def test_cash_priority_wins_when_a_trade_plan_is_missing_data(
        self, settings, missing
    ):
        candidate = _candidate(
            close=None if missing == "close" else 100.0,
            atr14=None if missing == "atr14" else 2.0,
        )

        result = RiskChecker(settings).check(
            [candidate], _exposure(GateVerdict.BEAR, DistributionLevel.NORMAL)
        )[0]

        assert result.status == "rejected"
        assert result.binding_constraint == "regime"
        assert result.reasons == (REGIME_CASH_PRIORITY_REASON,)

    def test_cash_priority_wins_when_the_stop_is_not_usable(self, settings):
        result = RiskChecker(settings).check(
            [_candidate(atr14=0.0)],
            _exposure(GateVerdict.BEAR, DistributionLevel.NORMAL),
        )[0]

        assert result.status == "rejected"
        assert result.binding_constraint == "regime"
        assert result.reasons == (REGIME_CASH_PRIORITY_REASON,)

    def test_reduce_only_is_a_label_without_filtering_or_risk_warning(self, settings):
        result = RiskChecker(settings).check(
            [_candidate()], _exposure(GateVerdict.NEUTRAL, DistributionLevel.NORMAL)
        )[0]

        assert result.status == "approved"
        assert result.binding_constraint is None
        assert result.warnings == ()


class TestEarningsGuard:
    @pytest.mark.parametrize("event_date", [AS_OF, date(2027, 1, 5)])
    def test_event_at_or_within_two_business_days_blocks(self, settings, event_date):
        checker = _checker_with_lookup(
            settings,
            EarningsLookup(status="found", event=_event(event_date), recent_event=None),
        )

        result = checker.check([_candidate()])[0]

        assert result.status == "rejected"
        assert EARNINGS_PROXIMITY_BLOCK_REASON in result.reasons
        assert result.binding_constraint == "earnings"

    def test_five_business_days_warns_without_rejecting(self, settings):
        checker = _checker_with_lookup(
            settings,
            EarningsLookup(
                status="found", event=_event(date(2027, 1, 8)), recent_event=None
            ),
        )

        result = checker.check([_candidate()])[0]

        assert result.status == "approved"
        assert any(
            warning.startswith(EARNINGS_PROXIMITY_WARN_WARNING)
            for warning in result.warnings
        )

    @pytest.mark.parametrize(
        ("lookup", "has_warning"),
        [
            (EarningsLookup("fetch_failed", None, None), True),
            (EarningsLookup("none_in_window", None, None), False),
        ],
    )
    def test_unknown_warning_distinguishes_fetch_failure_from_empty_window(
        self, settings, lookup, has_warning
    ):
        result = _checker_with_lookup(settings, lookup).check([_candidate()])[0]

        assert (EARNINGS_DATE_UNKNOWN_WARNING in result.warnings) is has_warning

    def test_past_event_warns_instead_of_blocking_forever(self, settings):
        checker = _checker_with_lookup(
            settings,
            EarningsLookup(
                status="found",
                event=_event(AS_OF - timedelta(days=1)),
                recent_event=None,
            ),
        )

        result = checker.check([_candidate()])[0]

        assert result.status == "approved"
        assert EARNINGS_DATE_UNKNOWN_WARNING in result.warnings

    @pytest.mark.parametrize(
        ("recent_date", "expected"),
        [(date(2026, 12, 29), True), (date(2026, 12, 28), False)],
    )
    def test_recent_report_warning_has_inclusive_three_business_day_boundary(
        self, settings, recent_date, expected
    ):
        checker = _checker_with_lookup(
            settings,
            EarningsLookup(
                status="none_in_window",
                event=None,
                recent_event=_event(recent_date),
            ),
        )

        result = checker.check([_candidate()])[0]

        assert (
            any(
                warning.startswith(EARNINGS_RECENTLY_REPORTED_WARNING)
                for warning in result.warnings
            )
            is expected
        )

    def test_market_block_remains_binding_when_earnings_also_blocks(self, settings):
        checker = _checker_with_lookup(
            settings,
            EarningsLookup(
                status="found", event=_event(date(2027, 1, 5)), recent_event=None
            ),
        )

        result = checker.check(
            [_candidate()], _exposure(GateVerdict.BEAR, DistributionLevel.NORMAL)
        )[0]

        assert result.binding_constraint == "regime"
        assert REGIME_CASH_PRIORITY_REASON in result.reasons
        assert EARNINGS_PROXIMITY_BLOCK_REASON in result.reasons
