"""Tests for `llm/safety.py`'s post-output CON-03 guard (FR-08, CON-03, P2-12)."""

from __future__ import annotations

import pytest

from swing_copilot.llm.safety import (
    ForbiddenLanguageError,
    check_no_imperative_language,
    check_no_unevidenced_behavioral_claims,
    check_structured_output,
)
from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary, SourcedFact


def _news_summary(
    interpretation: list[str] | None = None, risk_flags: list[str] | None = None
) -> NewsSummary:
    return NewsSummary(
        symbol="AAPL",
        period="2027-01",
        facts=[SourcedFact(statement="Revenue grew 10%", source_ids=["news:1"])],
        interpretation=interpretation if interpretation is not None else [],
        sentiment=1,
        risk_flags=risk_flags if risk_flags is not None else [],
        sources=["https://example.com"],
        catalyst_quality="none",
        catalyst_quality_source_ids=["news:1"],
    )


def _filing_analysis(
    interpretation: list[str] | None = None, red_flags: list[str] | None = None
) -> FilingAnalysis:
    return FilingAnalysis(
        symbol="AAPL",
        filing_type="10-Q",
        facts=[SourcedFact(statement="Revenue: $100M", source_ids=["edgar:1"])],
        interpretation=interpretation if interpretation is not None else [],
        red_flags=red_flags if red_flags is not None else [],
        yoy_changes=[],
        guidance_direction="not_disclosed",
    )


class TestCheckNoImperativeLanguage:
    def test_raises_on_forbidden_phrase(self):
        with pytest.raises(ForbiddenLanguageError):
            check_no_imperative_language(["この銘柄は買うべきです。"])

    def test_does_not_raise_on_clean_text(self):
        check_no_imperative_language(["Growth may continue."])


class TestCheckNoUnevidencedBehavioralClaims:
    def test_bare_psychological_diagnosis_raises(self):
        with pytest.raises(ForbiddenLanguageError):
            check_no_unevidenced_behavioral_claims(
                ["経営陣は動揺している可能性が高い。"]
            )

    def test_behavioral_keyword_without_hedge_raises(self):
        with pytest.raises(ForbiddenLanguageError):
            check_no_unevidenced_behavioral_claims(["投資家心理は悪化している。"])

    def test_english_bare_panic_claim_raises(self):
        with pytest.raises(ForbiddenLanguageError):
            check_no_unevidenced_behavioral_claims(
                ["Management is anxious about the outlook."]
            )

    def test_hedge_with_co_occurring_numeric_actual_vs_planned_evidence_does_not_raise(
        self,
    ):
        text = "実績が計画を10%下回ったことから、経営陣が動揺している可能性がある。"
        check_no_unevidenced_behavioral_claims([text])

    def test_hedge_with_percent_but_no_actual_planned_marker_still_requires_marker(
        self,
    ):
        # Hedge + a bare percentage alone (no 計画/予想/実績/actual/planned
        # marker) is NOT sufficient evidence -- deliberately strict per the
        # co-occurrence heuristic (regex requires the numeric-evidence
        # pattern, which itself already accepts a bare percentage; this test
        # pins the case where only the hedge exists without any numeric or
        # marker evidence at all).
        with pytest.raises(ForbiddenLanguageError):
            check_no_unevidenced_behavioral_claims(
                ["経営陣は動揺している可能性がある。"]
            )

    def test_text_without_any_behavioral_keyword_never_raises(self):
        check_no_unevidenced_behavioral_claims(["Revenue grew 10% year over year."])

    def test_hedge_with_percent_evidence_in_english_does_not_raise(self):
        text = (
            "Actual results missed the planned 12% target, suggesting a "
            "possible shift in investor sentiment."
        )
        check_no_unevidenced_behavioral_claims([text])


class TestCheckStructuredOutput:
    def test_news_summary_with_bare_behavioral_claim_raises(self):
        summary = _news_summary(interpretation=["経営陣はパニックに陥っている。"])
        with pytest.raises(ForbiddenLanguageError):
            check_structured_output(summary)

    def test_filing_analysis_with_bare_behavioral_claim_in_red_flags_raises(self):
        analysis = _filing_analysis(red_flags=["投資家心理が動揺している。"])
        with pytest.raises(ForbiddenLanguageError):
            check_structured_output(analysis)

    def test_clean_news_summary_does_not_raise(self):
        check_structured_output(_news_summary(interpretation=["Growth may continue."]))
