"""Strictness of the pipeline <-> skill JSON contract (`analysis/schemas.py`)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.schemas import (
    INPUT_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    AnalysisInput,
    AnalysisResult,
    SourcedFact,
    SymbolAnalysis,
    Verdict,
    VerdictReason,
)
from tests.analysis.conftest import (
    NEWS_ID,
    input_payload,
    result_payload,
    symbol_payload,
)


class TestSchemaVersions:
    def test_input_and_result_versions_are_the_agreed_constants(self):
        assert INPUT_SCHEMA_VERSION == "analysis-input-v1"
        assert RESULT_SCHEMA_VERSION == "analysis-result-v1"

    def test_a_wrong_result_schema_version_is_rejected(self):
        with pytest.raises(ValidationError, match="schema_version"):
            AnalysisResult.model_validate(result_payload(schema_version="v2"))


class TestUnknownFieldsAreRejected:
    def test_unknown_top_level_result_field_is_rejected(self):
        with pytest.raises(ValidationError, match="confidence"):
            AnalysisResult.model_validate(result_payload(confidence=0.9))

    def test_unknown_nested_field_is_rejected(self):
        payload = result_payload(
            symbols=[symbol_payload(verdict={"recommendation": "skip", "urgency": 3})]
        )
        with pytest.raises(ValidationError, match="urgency"):
            AnalysisResult.model_validate(payload)

    def test_unknown_input_field_is_rejected(self):
        with pytest.raises(ValidationError, match="operator"):
            AnalysisInput.model_validate(input_payload(operator="tomada"))


class TestProvenanceShape:
    def test_a_fact_without_source_ids_is_rejected(self):
        with pytest.raises(ValidationError, match="source_ids"):
            SourcedFact.model_validate(
                {"text": "Something happened.", "source_ids": []}
            )

    def test_a_blank_source_id_is_rejected(self):
        with pytest.raises(ValidationError, match="source_ids"):
            SourcedFact.model_validate({"text": "Something.", "source_ids": ["   "]})

    def test_a_blank_fact_text_is_rejected(self):
        with pytest.raises(ValidationError, match="text"):
            SourcedFact.model_validate({"text": "   ", "source_ids": [NEWS_ID]})

    def test_a_verdict_reason_may_cite_nothing(self):
        reason = VerdictReason.model_validate(
            {"text": "Score alone justifies proceeding."}
        )
        assert reason.source_ids == []


class TestVerdictValues:
    @pytest.mark.parametrize("recommendation", ["proceed", "skip"])
    def test_both_agreed_recommendations_are_accepted(self, recommendation):
        verdict = Verdict.model_validate({"recommendation": recommendation})
        assert verdict.recommendation == recommendation

    def test_any_other_recommendation_is_rejected(self):
        with pytest.raises(ValidationError, match="recommendation"):
            Verdict.model_validate({"recommendation": "hold"})


class TestRequiredPerSymbolSections:
    @pytest.mark.parametrize("missing", ["screening_assessment", "verdict"])
    def test_screening_assessment_and_verdict_are_required(self, missing):
        payload = symbol_payload()
        del payload[missing]
        with pytest.raises(ValidationError, match=missing):
            SymbolAnalysis.model_validate(payload)

    def test_news_summary_may_be_null_and_filings_may_be_empty(self):
        analysis = SymbolAnalysis.model_validate(
            symbol_payload(news_summary=None, filing_analyses=[])
        )
        assert analysis.news_summary is None
        assert analysis.filing_analyses == []


class TestInputRoundTrip:
    def test_a_generated_input_reparses_unchanged(self):
        parsed = AnalysisInput.model_validate(input_payload())
        assert AnalysisInput.model_validate(parsed.model_dump(mode="json")) == parsed
