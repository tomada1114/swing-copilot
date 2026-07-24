"""Tests for `llm/decision_context.py` (P2-12: REQ-001/002/003/030/040)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from swing_copilot.llm.decision_context import (
    format_performance_summary,
    format_risk_constraints,
    format_score_breakdown,
    is_cache_near_stale,
)
from swing_copilot.paper.journal import PerformanceSummary
from swing_copilot.risk.checks import RiskAssessment
from swing_copilot.screening.base import Candidate

_DEFAULT_RISK_ASSESSMENT = RiskAssessment(
    symbol="AAPL",
    status="approved",
    max_shares=200,
    entry_price=100.0,
    stop_price=90.0,
    reasons=(),
    shares_by_risk=200,
    shares_by_position_cap=500,
    binding_constraint="trade_risk",
    sizing_warnings=(),
)

_DEFAULT_PERFORMANCE_SUMMARY = PerformanceSummary(
    closed_trade_count=10,
    total_pnl_usd=500.0,
    win_rate=0.6,
    spy_return_pct=0.05,
    expectancy_usd=50.0,
    profit_factor=4.4,
    avg_r_multiple=1.2,
    r_multiple_omitted_count=0,
    r_multiple_omitted_warning=None,
    by_exit_reason=(),
    by_strategy=(),
)


def _candidate(metrics: dict[str, float]) -> Candidate:
    return Candidate(
        symbol="AAPL",
        as_of=date(2027, 3, 1),
        signal_names=("trend_sma",),
        metrics=metrics,
        rank=1,
    )


def _risk_assessment(
    **overrides: Any,
) -> RiskAssessment:  # Any: generic test-fixture override dict, checked per-field by RiskAssessment itself
    return replace(_DEFAULT_RISK_ASSESSMENT, **overrides)


def _performance_summary(
    **overrides: Any,
) -> PerformanceSummary:  # Any: see _risk_assessment
    return replace(_DEFAULT_PERFORMANCE_SUMMARY, **overrides)


class TestFormatScoreBreakdown:
    def test_present_data_renders_total_and_each_component(self):
        candidate = _candidate(
            {
                "score": 0.627,
                "score_rsi_pullback": 0.167,
                "score_trend_quality": 0.24,
                "score_liquidity": 0.22,
            }
        )

        text = format_score_breakdown(candidate)

        assert "<score_breakdown>" in text
        assert "0.627" in text
        assert "0.167" in text
        assert "0.240" in text
        assert "0.220" in text

    def test_missing_score_data_returns_empty_string(self):
        candidate = _candidate({"close": 100.0})

        assert format_score_breakdown(candidate) == ""

    def test_partially_missing_score_component_returns_empty_string(self):
        candidate = _candidate({"score": 0.5, "score_rsi_pullback": 0.2})

        assert format_score_breakdown(candidate) == ""


class TestFormatRiskConstraints:
    def test_present_data_renders_binding_constraint_and_shares(self):
        risk_assessment = _risk_assessment(
            binding_constraint="trade_risk",
            shares_by_risk=200,
            shares_by_position_cap=500,
            max_shares=200,
        )

        text = format_risk_constraints(risk_assessment)

        assert "<risk_constraints>" in text
        assert "trade_risk" in text
        assert "200" in text
        assert "500" in text

    def test_not_calculable_status_still_renders_the_constraint_name(self):
        risk_assessment = _risk_assessment(
            status="rejected",
            binding_constraint="not_calculable",
            max_shares=None,
            shares_by_risk=None,
            shares_by_position_cap=None,
        )

        text = format_risk_constraints(risk_assessment)

        assert "not_calculable" in text
        assert "不明" in text

    def test_sizing_warnings_are_included_when_present(self):
        risk_assessment = _risk_assessment(sizing_warnings=("WIDE_STOP",))

        text = format_risk_constraints(risk_assessment)

        assert "WIDE_STOP" in text


class TestFormatPerformanceSummary:
    def test_present_data_renders_win_rate_and_profit_factor(self):
        summary = _performance_summary(win_rate=0.6, profit_factor=4.4)

        text = format_performance_summary(summary)

        assert "<performance_summary>" in text
        assert "60.0%" in text
        assert "4.400" in text

    def test_none_summary_returns_empty_string(self):
        assert format_performance_summary(None) == ""

    def test_zero_closed_trades_returns_empty_string(self):
        summary = _performance_summary(
            closed_trade_count=0,
            total_pnl_usd=0.0,
            win_rate=None,
            expectancy_usd=None,
            profit_factor=None,
            avg_r_multiple=None,
        )

        assert format_performance_summary(summary) == ""


class TestIsCacheNearStale:
    def test_exactly_threshold_days_remaining_is_near_stale(self):
        cached_at = date(2027, 1, 1)
        as_of = date(2027, 1, 6)  # ttl_days=7 -> expires 2027-01-08, 2 days left

        assert is_cache_near_stale(cached_at, as_of, ttl_days=7, threshold_days=2)

    def test_one_more_day_remaining_than_threshold_is_not_near_stale(self):
        cached_at = date(2027, 1, 1)
        as_of = date(2027, 1, 5)  # 3 days left, threshold is 2

        assert not is_cache_near_stale(cached_at, as_of, ttl_days=7, threshold_days=2)

    def test_one_less_day_remaining_than_threshold_is_near_stale(self):
        cached_at = date(2027, 1, 1)
        as_of = date(2027, 1, 7)  # 1 day left, threshold is 2

        assert is_cache_near_stale(cached_at, as_of, ttl_days=7, threshold_days=2)

    def test_already_expired_is_near_stale(self):
        cached_at = date(2027, 1, 1)
        as_of = date(2027, 1, 20)  # ttl expired well before as_of

        assert is_cache_near_stale(cached_at, as_of, ttl_days=7, threshold_days=2)
