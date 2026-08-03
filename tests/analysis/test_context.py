"""Deterministic context blocks exported for analysis (`analysis/context.py`)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from swing_copilot.analysis.context import (
    format_decision_history,
    format_market_regime,
    format_performance_summary,
    format_risk_constraints,
    format_score_breakdown,
)
from swing_copilot.paper.journal import PerformanceSummary
from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    DistributionResult,
)
from swing_copilot.regime.exposure import ExposureDecision, ExposureVerdict
from swing_copilot.regime.gate import GateVerdict, MarketGate, RegimeSnapshot
from swing_copilot.risk.checks import RiskAssessment
from swing_copilot.screening.base import Candidate
from swing_copilot.storage.paper_records import DecisionHistoryEntry

AS_OF = date(2027, 3, 1)


def _candidate(**metrics: float) -> Candidate:
    return Candidate(
        symbol="AAPL",
        as_of=AS_OF,
        signal_names=("trend_sma",),
        metrics=metrics,
        rank=1,
    )


def _full_score_metrics() -> dict[str, float]:
    return {
        "score": 0.812,
        "score_rsi_pullback": 0.401,
        "score_trend_quality": 0.251,
        "score_liquidity": 0.160,
        "score_atr_pct": 0.000,
    }


def _performance(closed: int) -> PerformanceSummary:
    return PerformanceSummary(
        closed_trade_count=closed,
        total_pnl_usd=100.0,
        win_rate=0.5,
        spy_return_pct=0.01,
        expectancy_usd=50.0,
        profit_factor=1.5,
        avg_r_multiple=0.75,
        r_multiple_omitted_count=0,
        r_multiple_omitted_warning=None,
        by_exit_reason=(),
        by_strategy=(),
    )


class TestScoreBreakdown:
    def test_it_renders_every_weighted_component(self):
        block = format_score_breakdown(_candidate(**_full_score_metrics()))

        assert "<score_breakdown>" in block
        assert "合計スコア: 0.812" in block
        assert "rsi_pullback（加重後）: 0.401" in block
        assert "trend_quality（加重後）: 0.251" in block
        assert "liquidity（加重後）: 0.160" in block
        assert "atr_pct（加重後）: 0.000" in block

    @pytest.mark.parametrize(
        "missing",
        [
            "score",
            "score_rsi_pullback",
            "score_trend_quality",
            "score_liquidity",
            "score_atr_pct",
        ],
    )
    def test_any_missing_component_degrades_to_an_empty_block(self, missing):
        metrics = _full_score_metrics()
        del metrics[missing]

        assert format_score_breakdown(_candidate(**metrics)) == ""


class TestRiskConstraints:
    def test_it_renders_the_binding_constraint_and_share_counts(self):
        assessment = RiskAssessment(
            symbol="AAPL",
            status="approved",
            max_shares=128,
            entry_price=100.0,
            stop_price=95.0,
            reasons=(),
            shares_by_risk=128,
            shares_by_position_cap=200,
            binding_constraint="trade_risk",
            sizing_warnings=("WIDE_STOP",),
        )

        block = format_risk_constraints(assessment)

        assert "binding_constraint: trade_risk" in block
        assert "リスク基準の株数(shares_by_risk): 128" in block
        assert "ポジション上限基準の株数(shares_by_position_cap): 200" in block
        assert "最終株数(shares): 128" in block
        assert "warnings: WIDE_STOP" in block

    def test_a_not_calculable_assessment_still_renders_a_block(self):
        assessment = RiskAssessment(
            symbol="AAPL",
            status="not_calculable",
            max_shares=None,
            entry_price=None,
            stop_price=None,
            reasons=("account equity unset",),
        )

        block = format_risk_constraints(assessment)

        # The "code already declined to size this" signal must reach the
        # analysis, so this never degrades to an empty string.
        assert "binding_constraint: not_calculable" in block
        assert "最終株数(shares): 不明" in block
        assert "warnings: なし" in block


class TestMarketRegime:
    @staticmethod
    def _snapshot(quality: DataQuality) -> RegimeSnapshot:
        distribution = DistributionResult(
            d25=1.0,
            d15=0.0,
            d5=0.0,
            level=DistributionLevel.NORMAL,
            data_quality=DataQuality.OK,
        )
        return RegimeSnapshot(
            as_of=AS_OF,
            gate=MarketGate(GateVerdict.BULL, 100.0, 95.0, 15.0),
            spy_distribution=distribution,
            qqq_distribution=distribution,
            dd_level=DistributionLevel.NORMAL,
            data_quality=quality,
        )

    @staticmethod
    def _exposure() -> ExposureDecision:
        return ExposureDecision(
            verdict=ExposureVerdict.NEW_ENTRY_ALLOWED,
            gate=GateVerdict.BULL,
            dd_level=DistributionLevel.NORMAL,
            data_quality=DataQuality.OK,
            is_conservatively_downgraded=False,
        )

    def test_it_renders_the_code_owned_gate_and_ceiling(self):
        block = format_market_regime(self._snapshot(DataQuality.OK), self._exposure())

        assert "Gate: BULL" in block
        assert "Exposure Ceiling: NEW_ENTRY_ALLOWED" in block
        assert "Warning:" not in block

    def test_insufficient_data_adds_an_explicit_warning(self):
        block = format_market_regime(
            self._snapshot(DataQuality.INSUFFICIENT), self._exposure()
        )

        assert "Warning: Market regime is UNKNOWN" in block


class TestDecisionHistory:
    @staticmethod
    def _entry(reason: str | None, realized: float | None) -> DecisionHistoryEntry:
        return DecisionHistoryEntry(
            run_id=uuid4(),
            run_date=date(2027, 2, 20),
            symbol="AAPL",
            strategy_key="default",
            decision="buy",
            reason_memo=reason,
            virtual_fill_price=100.0,
            realized_return_pct=realized,
        )

    def test_empty_history_renders_nothing(self):
        assert format_decision_history(()) == ""

    def test_entries_are_rendered_as_escaped_data(self):
        block = format_decision_history((self._entry("<b>strong</b>", 0.0512),))

        assert "<decision_history>" in block
        assert "&lt;b&gt;strong&lt;/b&gt;" in block
        assert "<b>strong</b>" not in block
        assert "確定リターン: +5.12%" in block

    def test_a_missing_memo_and_return_degrade_to_explicit_placeholders(self):
        block = format_decision_history((self._entry(None, None),))

        assert "理由: (理由なし)" in block
        assert "確定リターン: 未確定/対象外" in block


class TestPerformanceSummaryBlock:
    def test_none_and_zero_closed_trades_both_render_nothing(self):
        assert format_performance_summary(None) == ""
        assert format_performance_summary(_performance(0)) == ""

    def test_closed_trades_render_the_realized_summary(self):
        block = format_performance_summary(_performance(3))

        assert "クローズ済み取引数: 3" in block
        assert "勝率: 50.0%" in block
        assert "profit_factor: 1.500" in block
