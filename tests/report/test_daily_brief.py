"""Shared point-in-time view-model construction for terminal and Markdown."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pandas as pd
import pytest

from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary, SourcedFact
from swing_copilot.report.daily_brief import DailyBriefContext, build_daily_brief
from swing_copilot.risk.checks import CorrelationWarning, RiskAssessment
from swing_copilot.screening.base import Candidate
from swing_copilot.storage.market_store import FundamentalsRecord
from swing_copilot.universe import UniverseMember

if TYPE_CHECKING:
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore

AS_OF = date(2026, 7, 22)


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
    def get_source_urls(self, source_ids):
        return {
            source_id: f"https://example.com/{source_id}" for source_id in source_ids
        }


def _context(*, with_llm: bool = True, with_risk: bool = True) -> DailyBriefContext:
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
    news = (
        [
            NewsSummary(
                symbol="AAPL",
                period="test",
                facts=[SourcedFact(statement="Revenue grew", source_ids=["news:1"])],
                interpretation=["Growth may continue", "Demand remains uncertain"],
                sentiment=1,
                risk_flags=["Valuation risk"],
                sources=["https://example.com/news:1"],
            )
        ]
        if with_llm
        else None
    )
    filing = (
        [
            FilingAnalysis(
                symbol="AAPL",
                filing_type="10-Q",
                facts=[
                    SourcedFact(statement="FCF was positive", source_ids=["filing:1"])
                ],
                interpretation=["Cash generation appears stable"],
                red_flags=["Margin pressure"],
                yoy_changes=[],
                guidance_direction="neutral",
            )
        ]
        if with_llm
        else None
    )
    return DailyBriefContext(
        run_id=uuid4(),
        run_date=AS_OF,
        generated_at=datetime(2026, 7, 22, 12, tzinfo=UTC),
        universe=(UniverseMember("AAPL", "Apple Inc.", "Technology", "AAPL"),),
        candidates=[candidate],
        risk_assessments=risks,
        news_summaries=news,
        filing_analyses=filing,
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
    assert candidate.llm.conclusion == "Growth may continue"
    assert candidate.llm.facts == ("Revenue grew", "FCF was positive")
    assert {source.source_id for source in candidate.llm.sources} == {
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
        _context(with_llm=False, with_risk=False),
        cast("MarketStore", FakeMarketStore(with_data=False)),
        cast("StateStore", FakeStateStore()),
    )

    assert all(item.value is None for item in brief.market)
    candidate = brief.candidates[0]
    assert candidate.pct_change is None
    assert candidate.fundamentals.per == "N/A"
    assert candidate.risk.status == "not_calculable"
    assert candidate.llm.degraded is True
    assert "取得できませんでした" in candidate.llm.conclusion
