"""Tests for structured LLM output schemas (FR-08, CON-03)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary, SourcedFact


class TestNewsSummary:
    def test_facts_and_interpretation_are_separate_fields(self):
        summary = NewsSummary(
            symbol="AAPL",
            period="2027-01",
            facts=[SourcedFact(statement="Revenue grew 10%", source_ids=["news:1"])],
            interpretation=["This may suggest continued growth."],
            sentiment=1,
            risk_flags=[],
            sources=["https://example.com/1"],
        )
        assert summary.facts[0].statement == "Revenue grew 10%"
        assert summary.interpretation == ["This may suggest continued growth."]

    def test_sentiment_rejects_out_of_range_values(self):
        with pytest.raises(ValidationError):
            NewsSummary.model_validate(
                {
                    "symbol": "AAPL",
                    "period": "2027-01",
                    "facts": [],
                    "interpretation": [],
                    "sentiment": 2,
                    "risk_flags": [],
                    "sources": [],
                }
            )

    def test_fact_requires_source_ids(self):
        with pytest.raises(ValidationError):
            SourcedFact.model_validate({"statement": "Revenue grew 10%"})

    def test_fact_rejects_empty_source_ids(self):
        with pytest.raises(ValidationError):
            SourcedFact(statement="Revenue grew 10%", source_ids=[])

    def test_fact_rejects_blank_source_id(self):
        with pytest.raises(ValidationError):
            SourcedFact(statement="Revenue grew 10%", source_ids=["   "])


class TestFilingAnalysis:
    def test_guidance_direction_rejects_unknown_values(self):
        with pytest.raises(ValidationError):
            FilingAnalysis.model_validate(
                {
                    "symbol": "AAPL",
                    "filing_type": "10-Q",
                    "facts": [],
                    "interpretation": [],
                    "red_flags": [],
                    "yoy_changes": [],
                    "guidance_direction": "bullish",
                }
            )

    def test_accepts_valid_guidance_direction(self):
        analysis = FilingAnalysis(
            symbol="AAPL",
            filing_type="10-Q",
            facts=[
                SourcedFact(statement="Revenue: $100M", source_ids=["edgar:acc-1:0"])
            ],
            interpretation=["Growth appears steady."],
            red_flags=[],
            yoy_changes=["Revenue +10% YoY"],
            guidance_direction="positive",
        )
        assert analysis.guidance_direction == "positive"
