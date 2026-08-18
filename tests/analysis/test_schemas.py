"""Strictness of the pipeline <-> skill JSON contract (`analysis/schemas.py`)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.schemas import (
    INPUT_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    VERDICT_BASES,
    AnalysisContextBlocks,
    AnalysisInput,
    AnalysisResult,
    CalendarEventInput,
    NewsSupply,
    SourcedFact,
    SymbolAnalysis,
    Verdict,
    VerdictReason,
    canonical_json_digest,
)
from tests.analysis.conftest import (
    NEWS_ID,
    input_payload,
    result_payload,
    symbol_payload,
)


class TestSchemaVersions:
    def test_input_and_result_versions_are_the_agreed_constants(self):
        assert INPUT_SCHEMA_VERSION == "analysis-input-v3"
        assert RESULT_SCHEMA_VERSION == "analysis-result-v3"

    def test_v3_requires_filing_coverage_but_v2_archive_remains_readable(self):
        legacy = input_payload()
        legacy["schema_version"] = "analysis-input-v2"
        legacy["candidates"][0]["filings"][0].pop("coverage")
        legacy["input_digest"] = canonical_json_digest(
            legacy, excluded_field="input_digest"
        )
        AnalysisInput.model_validate(legacy)
        legacy["schema_version"] = "analysis-input-v3"

        legacy["input_digest"] = canonical_json_digest(
            legacy, excluded_field="input_digest"
        )

        with pytest.raises(
            ValidationError, match="analysis-input-v3 requires coverage"
        ):
            AnalysisInput.model_validate(legacy)

    @pytest.mark.parametrize(
        "schema_version",
        [
            pytest.param("analysis-input-v2", id="v2-archive"),
            pytest.param("analysis-input-v3", id="v3-archive"),
        ],
    )
    def test_an_archive_without_news_supply_still_parses(self, schema_version):
        archived = input_payload()
        archived["schema_version"] = schema_version
        archived["candidates"][0].pop("news_supply")
        if schema_version == "analysis-input-v2":
            archived["candidates"][0]["filings"][0].pop("coverage")
        archived["input_digest"] = canonical_json_digest(
            archived, excluded_field="input_digest"
        )

        parsed = AnalysisInput.model_validate(archived)

        assert parsed.candidates[0].news_supply is None

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


class TestNewsSupplyCounts:
    def test_a_consistent_supply_block_is_accepted(self):
        supply = NewsSupply.model_validate(
            {
                "collected_items": 40,
                "exported_items": 20,
                "symbol_mention_items": 4,
                "level": "sparse",
            }
        )

        assert supply.level == "sparse"

    def test_exporting_more_than_was_collected_is_rejected(self):
        with pytest.raises(ValidationError, match="exported_items cannot exceed"):
            NewsSupply.model_validate(
                {
                    "collected_items": 2,
                    "exported_items": 3,
                    "symbol_mention_items": 1,
                    "level": "sparse",
                }
            )

    def test_more_mentions_than_exported_items_is_rejected(self):
        with pytest.raises(ValidationError, match="symbol_mention_items cannot exceed"):
            NewsSupply.model_validate(
                {
                    "collected_items": 3,
                    "exported_items": 3,
                    "symbol_mention_items": 4,
                    "level": "sparse",
                }
            )

    @pytest.mark.parametrize(
        ("mentions", "level"),
        [
            pytest.param(0, "sparse", id="empty-supply-graded-sparse"),
            pytest.param(0, "sufficient", id="empty-supply-graded-sufficient"),
            pytest.param(2, "none", id="present-supply-graded-none"),
        ],
    )
    def test_the_none_level_must_mean_exactly_zero_mentions(self, mentions, level):
        with pytest.raises(ValidationError, match="level 'none' means exactly zero"):
            NewsSupply.model_validate(
                {
                    "collected_items": 5,
                    "exported_items": 5,
                    "symbol_mention_items": mentions,
                    "level": level,
                }
            )

    def test_a_negative_count_is_rejected(self):
        with pytest.raises(ValidationError, match="symbol_mention_items"):
            NewsSupply.model_validate(
                {
                    "collected_items": 5,
                    "exported_items": 5,
                    "symbol_mention_items": -1,
                    "level": "none",
                }
            )


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

    def test_an_untagged_reason_keeps_basis_none_rather_than_a_default_kind(self):
        """Issue #191: absent means "not tagged", never "tagged as technical"."""
        reason = VerdictReason.model_validate(
            {"text": "Score alone justifies proceeding."}
        )
        assert reason.basis is None

    @pytest.mark.parametrize("basis", VERDICT_BASES)
    def test_every_documented_basis_value_is_accepted(self, basis):
        reason = VerdictReason.model_validate(
            {"text": "Earnings beat.", "source_ids": [NEWS_ID], "basis": basis}
        )
        assert reason.basis == basis

    def test_a_basis_outside_the_closed_vocabulary_is_rejected(self):
        """A free-text tag cannot be aggregated, which is the whole point."""
        with pytest.raises(ValidationError, match="basis"):
            VerdictReason.model_validate({"text": "Gut feeling.", "basis": "vibes"})

    def test_a_quote_below_the_minimum_length_is_rejected(self):
        # Wording this common occurs in any body, so it would evidence nothing.
        with pytest.raises(ValidationError, match="at least 12 characters"):
            SourcedFact.model_validate(
                {
                    "text": "Something happened.",
                    "source_ids": [NEWS_ID],
                    "evidence_quote": "the company",
                }
            )

    def test_a_quote_is_measured_after_normalization(self):
        with pytest.raises(ValidationError, match="at least 12 characters"):
            SourcedFact.model_validate(
                {
                    "text": "Something happened.",
                    "source_ids": [NEWS_ID],
                    "evidence_quote": "  the \n\n company  ",
                }
            )

    def test_a_quote_at_the_minimum_length_is_accepted(self):
        fact = SourcedFact.model_validate(
            {
                "text": "Something happened.",
                "source_ids": [NEWS_ID],
                "evidence_quote": "the company.",
            }
        )
        assert fact.evidence_quote == "the company."

    def test_a_quote_above_the_maximum_length_is_rejected(self):
        with pytest.raises(ValidationError, match="at most 300 characters"):
            SourcedFact.model_validate(
                {
                    "text": "Something happened.",
                    "source_ids": [NEWS_ID],
                    "evidence_quote": "x" * 301,
                }
            )

    def test_a_quote_at_the_maximum_length_is_accepted(self):
        fact = SourcedFact.model_validate(
            {
                "text": "Something happened.",
                "source_ids": [NEWS_ID],
                "evidence_quote": "x" * 300,
            }
        )
        assert fact.evidence_quote is not None

    def test_a_fact_may_omit_the_quote_so_v2_archives_stay_readable(self):
        fact = SourcedFact.model_validate(
            {"text": "Something happened.", "source_ids": [NEWS_ID]}
        )
        assert fact.evidence_quote is None


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

    def test_an_input_archived_before_prior_verdicts_still_verifies(self):
        """Issue #191 added a field; P8 collect must keep reading old runs.

        The digest is checked against the document as written, so an archive
        that never had the key hashes exactly as it did the day it was
        written -- and comes back as "no prior verdict", not as a failure.
        """
        payload = input_payload()
        for candidate in payload["candidates"]:
            del candidate["prior_verdicts"]
        payload["input_digest"] = canonical_json_digest(
            payload, excluded_field="input_digest"
        )

        parsed = AnalysisInput.model_validate(payload)

        assert parsed.candidates[0].prior_verdicts is None


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
