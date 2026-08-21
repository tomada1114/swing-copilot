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
    NewsSupply,
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
from swing_copilot.report.daily_brief import (
    PENDING_ANALYSIS_MESSAGE,
    BriefRejectionCount,
    BriefSource,
    DailyBriefContext,
    _sources_for_ids,
    build_daily_brief,
)
from swing_copilot.risk.checks import RiskAssessment
from swing_copilot.screening.base import (
    Candidate,
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
)
from swing_copilot.storage.market_store import FundamentalsRecord
from swing_copilot.universe import UniverseMember

if TYPE_CHECKING:
    from swing_copilot.storage.market_store import MarketStore

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


def _validated_analysis(
    *,
    filings: tuple[ResolvedFiling, ...] | None = None,
    news_supply: NewsSupply | None = None,
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
        news_supply=news_supply,
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
            "score_atr_pct": 0.000,
        },
        rank=1,
    )
    risks = (
        [
            RiskAssessment(
                symbol="AAPL",
                status="approved",
                entry_price=110.0,
                limit_price=113.0,
                stop_price=102.5,
                atr14=3.0,
                stop_distance_pct=(113.0 - 102.5) / 113.0,
                reasons=(),
                warnings=("WIDE_STOP",),
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


class TestNewsSupplyReachesTheBrief:
    """Issue #281: `news_supply` must survive `validate.py` -> `daily_brief.py`.

    `news_summary` stays null whenever `news[]` is empty (AC14 is unchanged),
    but `news_supply` is code-owned and independent of it -- it must reach
    `BriefAnalysis` regardless, so the report can later tell "suppressed"
    (level none/sparse over a non-empty collected set) apart from "genuinely
    zero" (`collected_items == 0`).
    """

    def test_a_suppressed_news_supply_reaches_the_candidate(self) -> None:
        context = replace(
            _context(),
            analysis=_validated_analysis(
                news_supply=NewsSupply(
                    level="sparse",
                    collected_items=8,
                    exported_items=8,
                    symbol_mention_items=1,
                )
            ),
        )

        brief = build_daily_brief(
            context,
            cast("MarketStore", FakeMarketStore()),
        )

        supply = brief.candidates[0].analysis.news_supply
        assert supply is not None
        assert supply.level == "sparse"
        assert supply.collected_items == 8
        assert supply.exported_items == 8
        assert supply.symbol_mention_items == 1

    def test_a_zero_collected_news_supply_reaches_the_candidate(self) -> None:
        context = replace(
            _context(),
            analysis=_validated_analysis(
                news_supply=NewsSupply(
                    level="none",
                    collected_items=0,
                    exported_items=0,
                    symbol_mention_items=0,
                )
            ),
        )

        brief = build_daily_brief(
            context,
            cast("MarketStore", FakeMarketStore()),
        )

        supply = brief.candidates[0].analysis.news_supply
        assert supply is not None
        assert supply.level == "none"
        assert supply.collected_items == 0
        # Distinct from the suppressed case above: a genuinely-zero supply
        # must not be confused with a suppressed-but-nonzero one.
        assert supply.symbol_mention_items == 0

    def test_absent_news_supply_leaves_the_field_none(self) -> None:
        brief = build_daily_brief(
            _context(),
            cast("MarketStore", FakeMarketStore()),
        )

        assert brief.candidates[0].analysis.news_supply is None


def test_builds_full_brief_and_uses_inclusive_as_of_reads() -> None:
    market_store = FakeMarketStore()

    brief = build_daily_brief(
        _context(),
        cast("MarketStore", market_store),
    )

    assert len(brief.market) == 4
    candidate = brief.candidates[0]
    assert candidate.company_name == "Apple Inc."
    assert candidate.pct_change == 0.1
    assert candidate.signals == ("SMA200上抜け", "custom")
    assert candidate.fundamentals.per == "55.0x"
    assert candidate.risk.warnings == ("WIDE_STOP",)
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
    )

    assert brief.rejection_counts == (
        BriefRejectionCount("FILTER_LOW_LIQUIDITY", 2),
        BriefRejectionCount("SIGNAL_TREND_NOT_MET", 1),
    )


def test_zero_rejections_produces_empty_rejection_counts() -> None:
    brief = build_daily_brief(
        _context(),
        cast("MarketStore", FakeMarketStore()),
    )

    assert brief.rejection_counts == ()


def test_risk_brief_propagates_public_trade_plan_from_the_pipeline() -> None:
    context = _context()
    context = replace(
        context,
        risk_assessments=[
            RiskAssessment(
                symbol="AAPL",
                status="approved",
                entry_price=50.0,
                limit_price=50.6,
                stop_price=45.0,
                atr14=2.0,
                stop_distance_pct=(50.6 - 45.0) / 50.6,
                reasons=(),
                warnings=("WIDE_STOP",),
            )
        ],
    )

    brief = build_daily_brief(
        context,
        cast("MarketStore", FakeMarketStore()),
    )

    risk = brief.candidates[0].risk
    assert risk.entry_price == pytest.approx(50.0)
    assert risk.limit_price == pytest.approx(50.6)
    assert risk.stop_price == pytest.approx(45.0)
    assert risk.atr14 == pytest.approx(2.0)
    assert risk.stop_distance_pct == pytest.approx((50.6 - 45.0) / 50.6)
    assert risk.warnings == ("WIDE_STOP",)


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
        )

        assert brief.candidates[0].analysis.filings == ()
