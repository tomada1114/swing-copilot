"""Ingest verification rules for skill output (`analysis/validate.py`)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pytest

from swing_copilot.analysis.validate import (
    AnalysisIngestError,
    load_analysis_input,
    load_analysis_result,
    validate_analysis,
)
from tests.analysis.conftest import (
    CALENDAR_ID,
    FILING_ID,
    NEWS_ID,
    input_payload,
    result_payload,
    symbol_payload,
)


def _validated(write_documents, **result_overrides):
    """Load and verify a result built from `result_payload(**overrides)`."""
    input_path, result_path = write_documents(None, result_payload(**result_overrides))
    return validate_analysis(
        load_analysis_input(input_path), load_analysis_result(result_path)
    )


class TestHardFailures:
    def test_a_missing_result_file_is_a_hard_failure(self, tmp_path):
        with pytest.raises(AnalysisIngestError, match="could not be read"):
            load_analysis_result(tmp_path / "nope.json")

    def test_malformed_json_is_a_hard_failure(self, write_documents):
        _input_path, result_path = write_documents(None, "{not json")

        with pytest.raises(AnalysisIngestError, match="not valid JSON"):
            load_analysis_result(result_path)

    def test_an_unknown_field_is_a_hard_failure(self, write_documents):
        _input_path, result_path = write_documents(None, result_payload(extra=1))

        with pytest.raises(AnalysisIngestError, match="failed schema validation"):
            load_analysis_result(result_path)

    def test_an_as_of_mismatch_is_a_hard_failure(self, write_documents):
        input_path, result_path = write_documents(
            None, result_payload(as_of=date(2027, 3, 2).isoformat())
        )

        with pytest.raises(AnalysisIngestError, match="does not match"):
            validate_analysis(
                load_analysis_input(input_path), load_analysis_result(result_path)
            )

    def test_a_matching_as_of_is_accepted(self, write_documents):
        validated = _validated(write_documents)

        assert validated.as_of == date(2027, 3, 1)
        assert validated.outcomes[0].error is None


class TestProvenance:
    def test_a_source_id_absent_from_the_input_withholds_the_symbol(
        self,
        write_documents,
        caplog,
    ):
        payload = symbol_payload(
            news_summary={
                "facts": [{"text": "Invented.", "source_ids": ["finnhub:999"]}],
                "interpretation": [],
                "risk_flags": [],
            }
        )

        with caplog.at_level(logging.WARNING):
            validated = _validated(write_documents, symbols=[payload])

        outcome = validated.outcomes[0]
        assert outcome.error is not None
        assert "finnhub:999" in outcome.error
        assert outcome.news_summary is None
        assert outcome.verdict is None
        assert "withheld (no retry)" in caplog.text

    def test_a_verdict_reason_citing_an_unknown_source_withholds_the_symbol(
        self,
        write_documents,
    ):
        payload = symbol_payload(
            verdict={
                "recommendation": "skip",
                "reasons": [{"text": "Bad news.", "source_ids": ["finnhub:404"]}],
            }
        )

        validated = _validated(write_documents, symbols=[payload])

        assert validated.outcomes[0].error is not None

    def test_a_verdict_reason_with_no_sources_is_accepted(self, write_documents):
        payload = symbol_payload(
            verdict={
                "recommendation": "proceed",
                "reasons": [{"text": "Score alone supports it.", "source_ids": []}],
            }
        )

        validated = _validated(write_documents, symbols=[payload])

        assert validated.outcomes[0].error is None

    def test_a_verdict_reason_citing_a_calendar_event_is_accepted(
        self, write_documents
    ):
        # CALENDAR_ID is run-wide context (context.calendar_events), not tied
        # to any one symbol's candidate, so any symbol may cite it.
        payload = symbol_payload(
            verdict={
                "recommendation": "skip",
                "reasons": [
                    {"text": "Macro event risk nearby.", "source_ids": [CALENDAR_ID]}
                ],
            }
        )

        validated = _validated(write_documents, symbols=[payload])

        assert validated.outcomes[0].error is None

    def test_another_symbols_news_id_is_still_rejected(self, write_documents):
        base_candidate = input_payload()["candidates"][0]
        other_candidate = {
            "symbol": "MSFT",
            "score_breakdown": "<score_breakdown>\n</score_breakdown>\n",
            "risk_constraints": "<risk_constraints>\n</risk_constraints>\n",
            "decision_history": None,
            "news": [
                {
                    "source_id": "finnhub:msft-1",
                    "published_at": "2027-02-28T00:00:00+00:00",
                    "headline": "MSFT news",
                    "summary": "MSFT unrelated news.",
                    "url": "https://example.com/msft-news",
                    "provider": "finnhub",
                }
            ],
            "filings": [],
        }
        custom_input = input_payload(candidates=[base_candidate, other_candidate])
        payload = symbol_payload(
            news_summary={
                "facts": [
                    {"text": "Borrowed from MSFT.", "source_ids": ["finnhub:msft-1"]}
                ],
                "interpretation": [],
                "risk_flags": [],
            }
        )

        input_path, result_path = write_documents(
            custom_input, result_payload(symbols=[payload])
        )
        validated = validate_analysis(
            load_analysis_input(input_path), load_analysis_result(result_path)
        )

        outcome = validated.outcomes[0]
        assert outcome.error is not None
        assert "finnhub:msft-1" in outcome.error

    def test_a_filing_analysis_for_an_unsupplied_filing_withholds_the_symbol(
        self,
        write_documents,
    ):
        payload = symbol_payload(
            filing_analyses=[
                {
                    "source_id": "edgar:unknown",
                    "facts": [],
                    "interpretation": [],
                    "red_flags": [],
                    "yoy_changes": [],
                }
            ]
        )

        validated = _validated(write_documents, symbols=[payload])

        error = validated.outcomes[0].error
        assert error is not None
        assert "edgar:unknown" in error


_VIOLATION = "今すぐ買うべき。"


def _news(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"facts": [], "interpretation": [], "risk_flags": []}
    payload.update(overrides)
    return payload


def _filing(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_id": FILING_ID,
        "facts": [],
        "interpretation": [],
        "red_flags": [],
        "yoy_changes": [],
    }
    payload.update(overrides)
    return payload


def _assessment(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": "中立な評価。",
        "strengths": [],
        "concerns": [],
    }
    payload.update(overrides)
    return payload


class TestCon03:
    # One case per free-text field `validate._display_texts()` yields. Deleting
    # any single `yield` there must fail exactly one of these.
    @pytest.mark.parametrize(
        "override",
        [
            pytest.param(
                {
                    "news_summary": _news(
                        facts=[{"text": _VIOLATION, "source_ids": [NEWS_ID]}]
                    )
                },
                id="news.facts.text",
            ),
            pytest.param(
                {"news_summary": _news(interpretation=[_VIOLATION])},
                id="news.interpretation",
            ),
            pytest.param(
                {"news_summary": _news(risk_flags=[_VIOLATION])},
                id="news.risk_flags",
            ),
            pytest.param(
                {
                    "filing_analyses": [
                        _filing(facts=[{"text": _VIOLATION, "source_ids": [FILING_ID]}])
                    ]
                },
                id="filing.facts.text",
            ),
            pytest.param(
                {"filing_analyses": [_filing(interpretation=[_VIOLATION])]},
                id="filing.interpretation",
            ),
            pytest.param(
                {"filing_analyses": [_filing(red_flags=[_VIOLATION])]},
                id="filing.red_flags",
            ),
            pytest.param(
                {"filing_analyses": [_filing(yoy_changes=[_VIOLATION])]},
                id="filing.yoy_changes",
            ),
            pytest.param(
                {"screening_assessment": _assessment(summary=_VIOLATION)},
                id="screening_assessment.summary",
            ),
            pytest.param(
                {"screening_assessment": _assessment(strengths=[_VIOLATION])},
                id="screening_assessment.strengths",
            ),
            pytest.param(
                {"screening_assessment": _assessment(concerns=[_VIOLATION])},
                id="screening_assessment.concerns",
            ),
            pytest.param(
                {
                    "verdict": {
                        "recommendation": "proceed",
                        "reasons": [{"text": _VIOLATION, "source_ids": []}],
                    }
                },
                id="verdict.reasons.text",
            ),
        ],
    )
    def test_a_violation_in_any_displayed_field_withholds_the_symbol(
        self,
        write_documents,
        override,
    ):
        validated = _validated(write_documents, symbols=[symbol_payload(**override)])

        outcome = validated.outcomes[0]
        assert outcome.error is not None
        assert "CON-03 violation" in outcome.error
        # Withholding is total: no field of a failing symbol may survive.
        assert outcome.news_summary is None
        assert outcome.filings == ()
        assert outcome.screening_assessment is None
        assert outcome.verdict is None

    def test_a_behavioral_violation_in_a_filing_field_withholds_the_symbol(
        self, write_documents
    ):
        payload = symbol_payload(
            filing_analyses=[_filing(red_flags=["投資家心理が悪化している。"])]
        )

        validated = _validated(write_documents, symbols=[payload])

        assert "CON-03 violation" in str(validated.outcomes[0].error)

    def test_one_symbols_violation_does_not_withhold_another(self, write_documents):
        clean = symbol_payload()
        dirty = symbol_payload(
            symbol="AAPL",
            screening_assessment={
                "summary": "今すぐ買う",
                "strengths": [],
                "concerns": [],
            },
        )
        # Both entries name AAPL, but only the violating one is withheld;
        # a per-symbol failure must never take a sibling down with it.
        validated = _validated(write_documents, symbols=[clean, dirty])

        assert validated.outcomes[0].error is None
        assert validated.outcomes[1].error is not None

    def test_a_violating_no_trade_reason_is_withheld_not_rendered(
        self,
        write_documents,
        caplog,
    ):
        with caplog.at_level(logging.WARNING):
            validated = _validated(
                write_documents, no_trade=True, no_trade_reason="全銘柄売るべき。"
            )

        assert validated.no_trade is True
        assert validated.no_trade_reason == "検証不合格のため非表示"
        assert "no_trade_reason withheld" in caplog.text

    def test_a_clean_no_trade_reason_survives(self, write_documents):
        validated = _validated(
            write_documents, no_trade=True, no_trade_reason="地合いが不安定なため。"
        )

        assert validated.no_trade_reason == "地合いが不安定なため。"


class TestSymbolWithoutText:
    def test_a_screening_only_symbol_verifies_with_no_news_or_filings(
        self,
        write_documents,
    ):
        payload = symbol_payload(
            news_summary=None,
            filing_analyses=[],
            verdict={"recommendation": "proceed", "reasons": []},
        )

        validated = _validated(write_documents, symbols=[payload])

        outcome = validated.outcomes[0]
        assert outcome.error is None
        assert outcome.news_summary is None
        assert outcome.filings == ()
        assert outcome.verdict is not None
        assert outcome.verdict.recommendation == "proceed"


class TestSymbolCoverage:
    def test_a_symbol_absent_from_the_input_is_an_error_outcome(self, write_documents):
        validated = _validated(write_documents, symbols=[symbol_payload(symbol="TSLA")])

        outcome = validated.for_symbol("TSLA")
        assert outcome is not None
        assert outcome.error == "symbol is absent from analysis_input.json"

    def test_a_symbol_absent_from_the_result_has_no_outcome(self, write_documents):
        validated = _validated(write_documents, symbols=[])

        assert validated.for_symbol("AAPL") is None


class TestResolvedMetadata:
    def test_filing_type_and_date_come_from_the_input_not_the_analysis(
        self,
        write_documents,
    ):
        validated = _validated(write_documents)

        filing = validated.outcomes[0].filings[0]
        assert filing.form_type == "10-Q"
        assert filing.filed_at == date(2027, 2, 20)

    def test_source_urls_come_from_the_input_document(self, write_documents):
        validated = _validated(write_documents)

        # Calendar events are citable by every symbol, so their URLs must be
        # resolvable too -- otherwise a legitimately cited macro event renders
        # as a bare source ID instead of a link.
        assert validated.source_urls == {
            NEWS_ID: "https://example.com/news",
            FILING_ID: "https://example.com/filing",
            CALENDAR_ID: "https://fred.stlouisfed.org/release?rid=1",
        }

    def test_a_fact_citing_a_calendar_event_resolves_to_its_url(self, write_documents):
        payload = symbol_payload(
            news_summary={
                "facts": [
                    {
                        "text": "雇用統計が as_of 直後に予定されている。",
                        "source_ids": [CALENDAR_ID],
                    }
                ],
                "interpretation": [],
                "risk_flags": [],
            }
        )

        validated = _validated(write_documents, symbols=[payload])

        assert validated.outcomes[0].error is None
        assert (
            validated.source_urls[CALENDAR_ID]
            == "https://fred.stlouisfed.org/release?rid=1"
        )


class TestInputLoading:
    def test_a_malformed_input_document_is_a_hard_failure(self, write_documents):
        input_path, _result_path = write_documents(input_payload(as_of="not-a-date"))

        with pytest.raises(AnalysisIngestError, match="failed schema validation"):
            load_analysis_input(input_path)
