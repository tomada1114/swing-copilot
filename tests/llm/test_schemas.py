"""Tests for structured LLM output schemas (FR-08, CON-03)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary, SourcedFact


def _news_summary_kwargs(**overrides: object) -> dict[str, object]:
    """P2-12: default valid `NewsSummary` kwargs, easy to override per test."""
    base: dict[str, object] = {
        "symbol": "AAPL",
        "period": "2027-01",
        "facts": [SourcedFact(statement="Revenue grew 10%", source_ids=["news:1"])],
        "interpretation": ["This may suggest continued growth."],
        "sentiment": 1,
        "risk_flags": [],
        "sources": ["https://example.com/1"],
        "catalyst_quality": "none",
        "catalyst_quality_source_ids": ["news:1"],
    }
    base.update(overrides)
    return base


class TestNewsSummary:
    def test_facts_and_interpretation_are_separate_fields(self):
        summary = NewsSummary.model_validate(_news_summary_kwargs())
        assert summary.facts[0].statement == "Revenue grew 10%"
        assert summary.interpretation == ["This may suggest continued growth."]

    def test_sentiment_rejects_out_of_range_values(self):
        with pytest.raises(ValidationError):
            NewsSummary.model_validate(_news_summary_kwargs(sentiment=2))

    def test_fact_requires_source_ids(self):
        with pytest.raises(ValidationError):
            SourcedFact.model_validate({"statement": "Revenue grew 10%"})

    def test_fact_rejects_empty_source_ids(self):
        with pytest.raises(ValidationError):
            SourcedFact(statement="Revenue grew 10%", source_ids=[])

    def test_fact_rejects_blank_source_id(self):
        with pytest.raises(ValidationError):
            SourcedFact(statement="Revenue grew 10%", source_ids=["   "])


class TestCatalystQuality:
    """P2-12 (REQ-006/007/008): catalyst_quality's literal enum and provenance."""

    @pytest.mark.parametrize("value", ["high", "medium", "low", "none"])
    def test_accepts_every_valid_catalyst_quality_value(self, value):
        summary = NewsSummary.model_validate(
            _news_summary_kwargs(catalyst_quality=value)
        )
        assert summary.catalyst_quality == value

    def test_rejects_unknown_catalyst_quality_value(self):
        with pytest.raises(ValidationError):
            NewsSummary.model_validate(_news_summary_kwargs(catalyst_quality="strong"))

    def test_requires_catalyst_quality_field(self):
        kwargs = _news_summary_kwargs()
        del kwargs["catalyst_quality"]
        with pytest.raises(ValidationError):
            NewsSummary.model_validate(kwargs)

    def test_rejects_empty_catalyst_quality_source_ids(self):
        with pytest.raises(ValidationError):
            NewsSummary.model_validate(
                _news_summary_kwargs(catalyst_quality_source_ids=[])
            )

    def test_rejects_blank_catalyst_quality_source_id(self):
        with pytest.raises(ValidationError):
            NewsSummary.model_validate(
                _news_summary_kwargs(catalyst_quality_source_ids=["   "])
            )

    def test_requires_catalyst_quality_source_ids_field(self):
        kwargs = _news_summary_kwargs()
        del kwargs["catalyst_quality_source_ids"]
        with pytest.raises(ValidationError):
            NewsSummary.model_validate(kwargs)


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
