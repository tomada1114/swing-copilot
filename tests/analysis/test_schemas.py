"""Strictness of the pipeline <-> skill JSON contract (`analysis/schemas.py`)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.schemas import (
    INPUT_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    AnalysisContextBlocks,
    AnalysisInput,
    AnalysisResult,
    CalendarEventInput,
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
        assert INPUT_SCHEMA_VERSION == "analysis-input-v2"
        assert RESULT_SCHEMA_VERSION == "analysis-result-v2"

    def test_a_wrong_result_schema_version_is_rejected(self):
        with pytest.raises(ValidationError, match="schema_version"):
            AnalysisResult.model_validate(result_payload(schema_version="v2"))


class TestRunIdentity:
    def test_input_and_result_preserve_a_full_canonical_input_digest(self):
        analysis_input = input_payload()
        analysis_result = result_payload()

        parsed_input = AnalysisInput.model_validate(analysis_input)
        parsed_result = AnalysisResult.model_validate(analysis_result)

        assert str(parsed_input.run_id) == analysis_input["run_id"]
        assert parsed_input.strategy_key == "default"
        assert parsed_input.input_digest == analysis_input["input_digest"]
        assert str(parsed_result.run_id) == analysis_input["run_id"]
        assert parsed_result.strategy_key == "default"
        assert parsed_result.input_digest == analysis_input["input_digest"]

    def test_a_changed_input_payload_is_rejected_by_its_digest(self):
        payload = input_payload()
        payload["candidates"][0]["news"][0]["summary"] = "Changed after export."

        with pytest.raises(ValidationError, match="input_digest"):
            AnalysisInput.model_validate(payload)


class TestUniqueAnalysisEntities:
    def test_duplicate_candidate_symbols_are_rejected(self):
        candidate = input_payload()["candidates"][0]
        payload = input_payload(candidates=[candidate, dict(candidate)])

        with pytest.raises(ValidationError, match="candidate symbols must be unique"):
            AnalysisInput.model_validate(payload)

    def test_duplicate_source_ids_within_a_candidate_are_rejected(self):
        candidate = input_payload()["candidates"][0]
        candidate["filings"][0]["source_id"] = NEWS_ID
        payload = input_payload(candidates=[candidate])

        with pytest.raises(
            ValidationError, match="candidate source_ids must be unique"
        ):
            AnalysisInput.model_validate(payload)

    def test_duplicate_result_symbols_are_rejected(self):
        payload = result_payload(symbols=[symbol_payload(), symbol_payload()])

        with pytest.raises(ValidationError, match="result symbols must be unique"):
            AnalysisResult.model_validate(payload)


class TestNoTradeContract:
    @pytest.mark.parametrize(
        ("no_trade", "reason", "is_valid"),
        [
            pytest.param(True, "市場環境が不安定。", True, id="true-with-reason"),
            pytest.param(True, None, False, id="true-without-reason"),
            pytest.param(False, None, True, id="false-without-reason"),
            pytest.param(False, "市場環境が不安定。", False, id="false-with-reason"),
        ],
    )
    def test_no_trade_reason_is_bound_to_the_flag(self, no_trade, reason, is_valid):
        payload = result_payload(no_trade=no_trade, no_trade_reason=reason)

        if is_valid:
            assert AnalysisResult.model_validate(payload).no_trade is no_trade
        else:
            with pytest.raises(ValidationError, match="no_trade_reason"):
                AnalysisResult.model_validate(payload)

    def test_no_trade_reason_must_not_be_blank(self):
        with pytest.raises(ValidationError, match="no_trade_reason"):
            AnalysisResult.model_validate(
                result_payload(no_trade=True, no_trade_reason="   ")
            )


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


class TestCalendarEventsContext:
    def test_calendar_events_default_to_an_empty_list(self):
        context = AnalysisContextBlocks.model_validate(
            {"market_regime": None, "performance_summary": None}
        )
        assert context.calendar_events == []

    def test_an_unknown_calendar_event_field_is_rejected(self):
        with pytest.raises(ValidationError, match="unexpected"):
            CalendarEventInput.model_validate(
                {
                    "source_id": "fred:1",
                    "published_at": "2027-03-05T00:00:00Z",
                    "title": "Employment Situation",
                    "summary": "Employment Situation",
                    "url": "https://example.com",
                    "provider": "fred",
                    "unexpected": "field",
                }
            )
