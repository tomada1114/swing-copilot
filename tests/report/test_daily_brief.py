"""Shared point-in-time view-model construction for terminal and Markdown."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pandas as pd
import pytest

from swing_copilot.analysis.schemas import (
    FilingAnalysis,
    NewsSummary,
    ScreeningAssessment,
    SourcedFact,
    Verdict,
    VerdictReason,
)
from swing_copilot.analysis.validate import (
    ResolvedFiling,
    SymbolOutcome,
    ValidatedAnalysis,
)
from swing_copilot.models import RunMode
from swing_copilot.report.daily_brief import (
    PENDING_ANALYSIS_MESSAGE,
    BriefRejectionCount,
    BriefRisk,
    BriefSource,
    DailyBriefContext,
    _sources_for_ids,
    build_daily_brief,
    format_sizing,
)
from swing_copilot.risk.checks import CorrelationWarning, RiskAssessment
from swing_copilot.screening.base import (
    Candidate,
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
)
from swing_copilot.storage.market_store import FundamentalsRecord
from swing_copilot.storage.paper_records import (
    DecisionHistoryEntry,
    TradeDecisionRecord,
)
from swing_copilot.universe import UniverseMember

if TYPE_CHECKING:
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore

AS_OF = date(2026, 7, 22)

SOURCE_URLS = {
    "news:1": "https://example.com/news:1",
    "filing:1": "https://example.com/filing:1",
}


def test_source_without_a_permitted_url_is_not_attributed() -> None:
    sources = _sources_for_ids(
        ["safe", "not-linkable"], {"safe": "https://example.com/safe"}
    )

    assert sources == (BriefSource("safe", "https://example.com/safe"),)


class FakeMarketStore:
    def __init__(self, *, with_data: bool = True):
        self.with_data = with_data
        self.read_calls: list[tuple[tuple[str, ...], date, date, date]] = []

    def read_bars(self, symbols, start, end, as_of):
        self.read_calls.append((tuple(symbols), start, end, as_of))
        if not self.with_data:
            return pd.DataFrame(columns=["symbol", "date", "close"])
        symbol = symbols[0]
        return pd.DataFrame(
            [
                {"symbol": symbol, "date": date(2026, 7, 21), "close": 100.0},
                {"symbol": symbol, "date": AS_OF, "close": 110.0},
            ]
        )

    def get_latest_fundamentals(self, symbol, as_of):
        assert as_of == AS_OF
        if not self.with_data:
            return None
        return FundamentalsRecord(
            accession_no="acc-1",
            symbol=symbol,
            form="10-Q",
            fiscal_period_end=AS_OF,
            filed_at=datetime(2026, 7, 21, tzinfo=UTC),
            revenue=1_000.0,
            net_income=200.0,
            fcf=150.0,
            equity=500.0,
            assets=1_000.0,
            shares=100.0,
            source_url="https://example.com/filing",
            fetched_at=datetime(2026, 7, 21, tzinfo=UTC),
        )


class FakeStateStore:
    def __init__(self, *, decision_history=()):
        self._decision_history = list(decision_history)
        self.decision_history_calls: list[tuple[str, str, date, int]] = []

    def get_decision_history(self, symbol, strategy_key, before_date, limit):
        self.decision_history_calls.append((symbol, strategy_key, before_date, limit))
        return self._decision_history


def _validated_analysis(
    *, filings: tuple[ResolvedFiling, ...] | None = None
) -> ValidatedAnalysis:
    if filings is None:
        filings = (
            ResolvedFiling(
                form_type="10-Q",
                filed_at=date(2026, 7, 21),
                analysis=FilingAnalysis(
                    source_id="filing:1",
                    facts=[
                        SourcedFact(text="FCF was positive", source_ids=["filing:1"])
                    ],
                    interpretation=["Cash generation appears stable"],
                    red_flags=["Margin pressure"],
                    yoy_changes=[],
                ),
            ),
        )
    outcome = SymbolOutcome(
        symbol="AAPL",
        news_summary=NewsSummary(
            facts=[SourcedFact(text="Revenue grew", source_ids=["news:1"])],
            interpretation=["Growth may continue", "Demand remains uncertain"],
            risk_flags=["Valuation risk"],
        ),
        filings=filings,
        screening_assessment=ScreeningAssessment(
            summary="Growth may continue",
            strengths=["Trend intact"],
            concerns=["Extended from the 50-day"],
        ),
        verdict=Verdict(
            recommendation="proceed",
            reasons=[
                VerdictReason(
                    text="No contradicting disclosure", source_ids=["filing:1"]
                )
            ],
        ),
    )
    return ValidatedAnalysis(
        as_of=AS_OF,
        no_trade=False,
        no_trade_reason=None,
        outcomes=(outcome,),
        source_urls=SOURCE_URLS,
    )


def _context(
    *, with_analysis: bool = True, with_risk: bool = True
) -> DailyBriefContext:
    candidate = Candidate(
        symbol="AAPL",
        as_of=AS_OF,
        signal_names=("volume_min", "trend_sma", "custom"),
        metrics={
            "close": 110.0,
            "rsi14": 45.0,
            "atr14": 3.0,
            "score": 0.627,
            "score_rsi_pullback": 0.167,
            "score_trend_quality": 0.300,
            "score_liquidity": 0.160,
        },
        rank=1,
    )
    risks = (
        [
            RiskAssessment(
                symbol="AAPL",
                status="approved",
                max_shares=10,
                entry_price=110.0,
                stop_price=102.5,
                reasons=(),
                warnings=(CorrelationWarning("MSFT", 0.81),),
            )
        ]
        if with_risk
        else []
    )
    analysis = _validated_analysis() if with_analysis else None
    return DailyBriefContext(
        run_id=uuid4(),
        run_date=AS_OF,
        generated_at=datetime(2026, 7, 22, 12, tzinfo=UTC),
        universe=(UniverseMember("AAPL", "Apple Inc.", "Technology", "AAPL"),),
        candidates=[candidate],
        risk_assessments=risks,
        analysis=analysis,
        strategy_key="default",
        notices=("calendar unavailable",),
    )


def test_builds_full_brief_and_uses_inclusive_as_of_reads() -> None:
    market_store = FakeMarketStore()

    brief = build_daily_brief(
        _context(),
        cast("MarketStore", market_store),
        cast("StateStore", FakeStateStore()),
    )

    assert len(brief.market) == 4
    candidate = brief.candidates[0]
    assert candidate.company_name == "Apple Inc."
    assert candidate.pct_change == 0.1
    assert candidate.signals == ("SMA200上抜け", "custom")
    assert candidate.fundamentals.per == "55.0x"
    assert candidate.risk.warnings == ("MSFTとの相関 0.81",)
    assert candidate.analysis.conclusion == "Growth may continue"
    assert candidate.analysis.facts == ("Revenue grew", "FCF was positive")
    assert {source.source_id for source in candidate.analysis.sources} == {
        "news:1",
        "filing:1",
    }
    assert all(call[3] == AS_OF for call in market_store.read_calls)
    assert candidate.score == pytest.approx(0.627)
    assert candidate.score_rsi_pullback == pytest.approx(0.167)
    assert candidate.score_trend_quality == pytest.approx(0.300)
    assert candidate.score_liquidity == pytest.approx(0.160)


def test_missing_score_fields_produce_none() -> None:
    context = _context()
    no_score_candidate = Candidate(
        symbol="AAPL",
        as_of=AS_OF,
        signal_names=(),
        metrics={"close": 110.0, "rsi14": 45.0, "atr14": 3.0},
        rank=1,
    )
    context = replace(context, candidates=[no_score_candidate])

    brief = build_daily_brief(
        context,
        cast("MarketStore", FakeMarketStore()),
        cast("StateStore", FakeStateStore()),
    )

    candidate = brief.candidates[0]
    assert candidate.score is None
    assert candidate.score_rsi_pullback is None
    assert candidate.score_trend_quality is None
    assert candidate.score_liquidity is None


def test_missing_data_produces_explicit_fallbacks() -> None:
    brief = build_daily_brief(
        _context(with_analysis=False, with_risk=False),
        cast("MarketStore", FakeMarketStore(with_data=False)),
        cast("StateStore", FakeStateStore()),
    )

    assert all(item.value is None for item in brief.market)
    candidate = brief.candidates[0]
    assert candidate.pct_change is None
    assert candidate.fundamentals.per == "N/A"
    assert candidate.risk.status == "not_calculable"
    assert candidate.analysis.degraded is True
    assert candidate.analysis.conclusion == PENDING_ANALYSIS_MESSAGE


def test_rejection_counts_are_tallied_by_reason_code_alphabetically() -> None:
    context = replace(
        _context(),
        rejections=[
            RejectionRecord(
                symbol="A",
                stage=RejectionStage.TECHNICAL_SIGNAL,
                reason_code=RejectionReasonCode.SIGNAL_TREND_NOT_MET,
                detail={},
            ),
            RejectionRecord(
                symbol="B",
                stage=RejectionStage.FUNDAMENTAL_FILTER,
                reason_code=RejectionReasonCode.FILTER_LOW_LIQUIDITY,
                detail={},
            ),
            RejectionRecord(
                symbol="C",
                stage=RejectionStage.FUNDAMENTAL_FILTER,
                reason_code=RejectionReasonCode.FILTER_LOW_LIQUIDITY,
                detail={},
            ),
        ],
    )

    brief = build_daily_brief(
        context,
        cast("MarketStore", FakeMarketStore()),
        cast("StateStore", FakeStateStore()),
    )

    assert brief.rejection_counts == (
        BriefRejectionCount("FILTER_LOW_LIQUIDITY", 2),
        BriefRejectionCount("SIGNAL_TREND_NOT_MET", 1),
    )


def test_zero_rejections_produces_empty_rejection_counts() -> None:
    brief = build_daily_brief(
        _context(),
        cast("MarketStore", FakeMarketStore()),
        cast("StateStore", FakeStateStore()),
    )

    assert brief.rejection_counts == ()


def test_risk_brief_propagates_sizing_breakdown_from_the_pipeline() -> None:
    # REQ-005/REQ-006: RiskAssessment's sizing breakdown and the run's
    # configured percentages both reach the rendered BriefRisk.
    context = _context()
    context = replace(
        context,
        risk_assessments=[
            RiskAssessment(
                symbol="AAPL",
                status="approved",
                max_shares=200,
                entry_price=50.0,
                stop_price=45.0,
                reasons=(),
                warnings=(),
                shares_by_risk=200,
                shares_by_position_cap=500,
                binding_constraint="trade_risk",
                sizing_warnings=("WIDE_STOP",),
            )
        ],
        max_trade_risk_pct=0.01,
        max_position_pct=0.25,
    )

    brief = build_daily_brief(
        context,
        cast("MarketStore", FakeMarketStore()),
        cast("StateStore", FakeStateStore()),
    )

    risk = brief.candidates[0].risk
    assert risk.shares_by_risk == 200
    assert risk.shares_by_position_cap == 500
    assert risk.binding_constraint == "trade_risk"
    assert risk.sizing_warnings == ("WIDE_STOP",)
    assert risk.max_trade_risk_pct == 0.01
    assert risk.max_position_pct == 0.25


class TestFormatSizing:
    """P1-03 (REQ-006): the compact "128株（制約: リスク1.0%）"-style string."""

    def test_not_calculable_renders_dash(self) -> None:
        risk = BriefRisk("not_calculable", None, None, (), ())
        assert format_sizing(risk) == "-"

    def test_zero_shares_uses_example_4_friction_wording(self) -> None:
        # True small-account friction: trade_risk/position_cap bound and
        # the sizing floor itself rounded down to zero.
        risk = BriefRisk(
            "approved",
            0,
            45.0,
            (),
            (),
            binding_constraint="position_cap",
            sizing_warnings=("SMALL_ACCOUNT_FRICTION",),
            max_trade_risk_pct=0.01,
            max_position_pct=0.001,
        )
        assert format_sizing(risk) == "0株（摩擦: 資金規模過小）"

    def test_zero_shares_regime_binding_uses_regime_wording_not_friction(
        self,
    ) -> None:
        # P6-28: an Exposure Ceiling CASH_PRIORITY (or circuit-breaker) halt
        # sizes zero shares via `binding_constraint="regime"`, which is not
        # small-account friction and must not be mislabeled as such.
        risk = BriefRisk(
            "rejected",
            0,
            None,
            ("REGIME_CASH_PRIORITY",),
            (),
            binding_constraint="regime",
            max_trade_risk_pct=0.0,
        )
        rendered = format_sizing(risk)
        assert rendered == "0株（レジーム: 新規建て停止）"
        assert "資金規模過小" not in rendered

    def test_zero_shares_correlation_binding_uses_correlation_wording_not_friction(
        self,
    ) -> None:
        # Same category of bug as the regime case above: a correlation veto
        # (like sector concentration) is not a sizing-floor friction, and
        # must not be mislabeled as small-account friction either.
        risk = BriefRisk(
            "rejected",
            0,
            None,
            (),
            (),
            binding_constraint="correlation",
        )
        rendered = format_sizing(risk)
        assert rendered == "0株（制約: 相関集中）"
        assert "資金規模過小" not in rendered

    def test_issue_example_1_trade_risk_string(self) -> None:
        risk = BriefRisk(
            "approved",
            128,
            None,
            (),
            (),
            binding_constraint="trade_risk",
            max_trade_risk_pct=0.01,
            max_position_pct=0.25,
        )
        assert format_sizing(risk) == "128株（制約: リスク1.0%）"

    def test_issue_example_2_position_cap_string(self) -> None:
        risk = BriefRisk(
            "approved",
            40,
            None,
            (),
            (),
            binding_constraint="position_cap",
            max_trade_risk_pct=0.01,
            max_position_pct=0.02,
        )
        assert format_sizing(risk) == "40株（制約: ポジション上限2.0%）"

    def test_sector_binding_uses_a_dedicated_label(self) -> None:
        risk = BriefRisk("rejected", 12, None, (), (), binding_constraint="sector")
        assert format_sizing(risk) == "12株（制約: セクター集中）"

    def test_correlation_binding_uses_a_dedicated_label(self) -> None:
        # Currently unreachable in production (correlation never blocks),
        # but format_sizing must still render it correctly for
        # completeness/future-proofing.
        risk = BriefRisk("approved", 5, None, (), (), binding_constraint="correlation")
        assert format_sizing(risk) == "5株（制約: 相関）"


class TestPastDecisions:
    """REQ-008: `past_decisions` mapping tests.

    A thin, correctly-scoped mapping over `state_store.get_decision_history()`.
    """

    def test_populates_from_get_decision_history_scoped_to_symbol_strategy_and_run_date(
        self,
    ) -> None:
        history = [
            DecisionHistoryEntry(
                run_id=uuid4(),
                run_date=date(2026, 7, 15),
                symbol="AAPL",
                strategy_key="default",
                decision="followed",
                reason_memo="出来高増加",
                virtual_fill_price=100.0,
                realized_return_pct=0.05,
            )
        ]
        state_store = FakeStateStore(decision_history=history)

        brief = build_daily_brief(
            _context(with_analysis=False),
            cast("MarketStore", FakeMarketStore()),
            cast("StateStore", state_store),
        )

        # `strategy_key`/`run_date` come from `DailyBriefContext`, not
        # hardcoded, and `limit=3` is REQ-008's "直近3件".
        assert state_store.decision_history_calls == [("AAPL", "default", AS_OF, 3)]
        past = brief.candidates[0].past_decisions
        assert len(past) == 1
        assert past[0].run_date == date(2026, 7, 15)
        assert past[0].decision == "followed"
        assert past[0].reason_memo == "出来高増加"
        assert past[0].realized_return_pct == 0.05

    def test_zero_past_decisions_produces_empty_tuple_without_error(self) -> None:
        brief = build_daily_brief(
            _context(with_analysis=False),
            cast("MarketStore", FakeMarketStore()),
            cast("StateStore", FakeStateStore()),
        )

        assert brief.candidates[0].past_decisions == ()

    def test_example_4_four_recorded_decisions_truncate_to_three_newest_first(
        self, state_store: StateStore
    ) -> None:
        # Issue's worked Example 4: 4 prior decisions recorded for AAPL ->
        # only the 3 most recent appear, newest-first; the oldest is absent.
        run_dates = [
            date(2026, 7, 10),
            date(2026, 7, 13),
            date(2026, 7, 16),
            date(2026, 7, 19),
        ]
        for i, run_date in enumerate(run_dates):
            run_id = state_store.start_run(run_date, RunMode.LIVE, "cfg")
            state_store.record_trade_decision(
                TradeDecisionRecord(
                    run_id=run_id,
                    symbol="AAPL",
                    strategy_key="default",
                    position_id=None,
                    decision="followed",
                    reason_memo=f"memo-{i}",
                    virtual_fill_price=100.0,
                )
            )

        brief = build_daily_brief(
            _context(with_analysis=False),
            cast("MarketStore", FakeMarketStore()),
            state_store,
        )

        past = brief.candidates[0].past_decisions
        assert len(past) == 3
        assert [p.run_date for p in past] == [
            date(2026, 7, 19),
            date(2026, 7, 16),
            date(2026, 7, 13),
        ]
        assert date(2026, 7, 10) not in {p.run_date for p in past}


class TestMultipleFilingAnalysesPerCandidate:
    """P6-27: every filing analysis for a symbol reaches the report.

    Previously `_llm_brief()` used `next(...)` and kept only the first
    filing analysis per symbol -- a second filing (e.g. an 8-K following a
    10-Q) was silently dropped from the report even though it was
    successfully analyzed and stored.
    """

    def test_all_filing_analyses_for_the_symbol_are_kept_and_individually_identified(
        self,
    ) -> None:
        context = _context()
        analysis = context.analysis
        assert analysis is not None
        first_filing = analysis.outcomes[0].filings[0]
        second_filing = ResolvedFiling(
            form_type="8-K",
            filed_at=date(2026, 7, 20),
            analysis=FilingAnalysis(
                source_id="filing:2",
                facts=[SourcedFact(text="Guidance raised", source_ids=["filing:2"])],
                interpretation=["Guidance raise may indicate confidence"],
                red_flags=[],
                yoy_changes=[],
            ),
        )
        updated_outcome = replace(
            analysis.outcomes[0], filings=(first_filing, second_filing)
        )
        context = replace(
            context, analysis=replace(analysis, outcomes=(updated_outcome,))
        )

        brief = build_daily_brief(
            context,
            cast("MarketStore", FakeMarketStore()),
            cast("StateStore", FakeStateStore()),
        )

        filings = brief.candidates[0].analysis.filings
        assert len(filings) == 2
        assert {f.filing_type for f in filings} == {"10-Q", "8-K"}
        by_type = {f.filing_type: f for f in filings}
        assert by_type["10-Q"].filed_at == date(2026, 7, 21)
        assert by_type["10-Q"].facts == ("FCF was positive",)
        assert by_type["8-K"].filed_at == date(2026, 7, 20)
        assert by_type["8-K"].facts == ("Guidance raised",)
        # The flat aggregate fields still span every filing (unchanged
        # semantics), now including the previously-dropped second filing.
        assert "Guidance raised" in brief.candidates[0].analysis.facts

    def test_no_filings_produces_an_empty_filings_tuple(self) -> None:
        brief = build_daily_brief(
            _context(with_analysis=False),
            cast("MarketStore", FakeMarketStore()),
            cast("StateStore", FakeStateStore()),
        )

        assert brief.candidates[0].analysis.filings == ()
