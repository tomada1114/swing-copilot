"""Ingest verification rules for skill output (`analysis/validate.py`)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pytest

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.analysis.validate import (
    AnalysisIngestError,
    load_analysis_input,
    load_analysis_result,
    validate_analysis,
)
from tests.analysis.conftest import (
    CALENDAR_ID,
    FILING_ID,
    FILING_QUOTE,
    NEWS_ID,
    NEWS_QUOTE,
    input_payload,
    result_payload,
    symbol_payload,
)


def _validated(write_documents, *, analysis_input=None, **result_overrides):
    """Load and verify a result built from `result_payload(**overrides)`."""
    input_path, result_path = write_documents(
        analysis_input, result_payload(**result_overrides)
    )
    return validate_analysis(
        load_analysis_input(input_path), load_analysis_result(result_path)
    )


class TestHardFailures:
    def test_a_missing_result_file_is_a_hard_failure(self, tmp_path):
        with pytest.raises(AnalysisIngestError, match="could not be read"):
            load_analysis_result(tmp_path / "nope.json")

    @pytest.mark.parametrize(
        ("load", "filename"),
        [
            pytest.param(load_analysis_input, ANALYSIS_INPUT_FILENAME, id="input"),
            pytest.param(load_analysis_result, ANALYSIS_RESULT_FILENAME, id="result"),
        ],
    )
    def test_a_wrongly_encoded_document_is_a_hard_failure(
        self, tmp_path, load, filename
    ):
        """A non-UTF-8 artifact must arrive as `AnalysisIngestError` (Issue #153).

        `UnicodeDecodeError` is a `ValueError`, so the read step used to let it
        escape uncaught and the callers that tell "broken artifact" from
        "unexpected fault" by exception type saw the wrong kind of failure.
        """
        path = tmp_path / filename
        path.write_bytes(b'{"as_of": "\xff\xfe"}')

        with pytest.raises(AnalysisIngestError, match="could not be read"):
            load(path)

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
        other_candidate: dict[str, Any] = {
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
        other_result = symbol_payload(
            symbol="MSFT",
            news_summary=None,
            filing_analyses=[],
            verdict={"recommendation": "proceed", "reasons": []},
        )

        input_path, result_path = write_documents(
            custom_input,
            result_payload(
                input_digest=custom_input["input_digest"],
                symbols=[payload, other_result],
            ),
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


class TestEvidenceQuotes:
    """The 2026-07-30 failure: correct IDs, text written from another slice."""

    def test_a_quote_that_occurs_in_the_cited_body_is_accepted(self, write_documents):
        validated = _validated(write_documents)

        assert validated.outcomes[0].error is None

    def test_a_quote_absent_from_the_cited_body_withholds_the_symbol(
        self, write_documents
    ):
        # The mechanism of the incident: a correctly-cited HUM filing carrying a
        # sentence that only exists in UDR's. IDs alone cannot see this.
        payload = symbol_payload(
            filing_analyses=[
                _filing(
                    facts=[
                        {
                            "text": "Occupancy improved across the portfolio.",
                            "source_ids": [FILING_ID],
                            "evidence_quote": "same-store occupancy improved",
                        }
                    ]
                )
            ]
        )

        validated = _validated(write_documents, symbols=[payload])

        error = validated.outcomes[0].error
        assert error is not None
        assert "same-store occupancy improved" in error

    def test_a_fact_without_a_quote_withholds_the_symbol(self, write_documents):
        payload = symbol_payload(
            news_summary=_news(
                facts=[{"text": "Unevidenced claim.", "source_ids": [NEWS_ID]}]
            )
        )

        validated = _validated(write_documents, symbols=[payload])

        error = validated.outcomes[0].error
        assert error is not None
        assert "no evidence_quote" in error

    def test_a_quote_must_occur_in_a_body_the_same_fact_cites(self, write_documents):
        # The quote is verbatim from the news item, but the fact cites only the
        # filing. Admitting it would let any supplied body vouch for any claim.
        payload = symbol_payload(
            filing_analyses=[
                _filing(
                    facts=[
                        {
                            "text": "Cross-cited claim.",
                            "source_ids": [FILING_ID],
                            "evidence_quote": NEWS_QUOTE,
                        }
                    ]
                )
            ]
        )

        validated = _validated(write_documents, symbols=[payload])

        error = validated.outcomes[0].error
        assert error is not None
        assert NEWS_QUOTE in error

    def test_a_quote_matching_any_one_cited_body_is_enough(self, write_documents):
        payload = symbol_payload(
            filing_analyses=[
                _filing(
                    facts=[
                        {
                            "text": "Corroborated by both sources.",
                            "source_ids": [FILING_ID, CALENDAR_ID],
                            "evidence_quote": "Employment Situation",
                        }
                    ]
                )
            ]
        )

        validated = _validated(write_documents, symbols=[payload])

        assert validated.outcomes[0].error is None

    def test_presentation_differences_do_not_withhold_an_honest_quote(
        self, write_documents
    ):
        payload = symbol_payload(
            filing_analyses=[
                _filing(
                    facts=[
                        {
                            "text": "Quarterly results were filed.",
                            "source_ids": [FILING_ID],
                            "evidence_quote": "  QUARTERLY\n  REPORT  ",
                        }
                    ]
                )
            ]
        )

        validated = _validated(write_documents, symbols=[payload])

        assert validated.outcomes[0].error is None

    def test_one_symbols_unsupported_quote_leaves_its_sibling_intact(
        self, write_documents
    ):
        base_candidate = input_payload()["candidates"][0]
        sibling = {**base_candidate, "symbol": "MSFT", "news": [], "filings": []}
        custom_input = input_payload(candidates=[base_candidate, sibling])
        failing = symbol_payload(
            news_summary=_news(
                facts=[
                    {
                        "text": "Written from elsewhere.",
                        "source_ids": [NEWS_ID],
                        "evidence_quote": "a sentence from another filing",
                    }
                ]
            )
        )
        healthy = symbol_payload(
            symbol="MSFT",
            news_summary=None,
            filing_analyses=[],
            verdict={"recommendation": "proceed", "reasons": []},
        )

        input_path, result_path = write_documents(
            custom_input,
            result_payload(
                input_digest=custom_input["input_digest"],
                symbols=[failing, healthy],
            ),
        )
        validated = validate_analysis(
            load_analysis_input(input_path), load_analysis_result(result_path)
        )

        assert validated.outcomes[0].error is not None
        assert validated.outcomes[1].error is None

    def test_an_unverifiable_quote_is_logged_without_retry(
        self, write_documents, caplog
    ):
        payload = symbol_payload(
            news_summary=_news(
                facts=[
                    {
                        "text": "Written from elsewhere.",
                        "source_ids": [NEWS_ID],
                        "evidence_quote": "a sentence from another filing",
                    }
                ]
            )
        )

        with caplog.at_level(logging.WARNING):
            _validated(write_documents, symbols=[payload])

        withheld = [record for record in caplog.records if "withheld" in record.message]
        assert len(withheld) == 1


class TestResultSchemaVersion:
    def test_an_archived_v2_result_cannot_be_ingested(self, write_documents):
        # Still parseable for P8 collect, but it carries no quotes to verify,
        # so accepting it at ingest would reopen the hole.
        legacy = result_payload(schema_version="analysis-result-v2")
        input_path, result_path = write_documents(None, legacy)

        with pytest.raises(AnalysisIngestError, match="analysis-result-v3 is required"):
            validate_analysis(
                load_analysis_input(input_path), load_analysis_result(result_path)
            )

    def test_an_archived_v2_result_still_parses(self, write_documents):
        _input_path, result_path = write_documents(
            None, result_payload(schema_version="analysis-result-v2")
        )

        assert load_analysis_result(result_path).schema_version == "analysis-result-v2"


class TestNumericConsistencyWarnings:
    """A verbatim quote restated with the wrong digits (Issue #131).

    The quote is real, its source ID is real, and CON-03 has nothing to say --
    only the converted figure is wrong, so this check warns rather than
    withholds and the symbol still reaches the report.
    """

    def test_a_misconverted_figure_is_warned_about(self, write_documents, caplog):
        with caplog.at_level(logging.WARNING):
            validated = _validated(
                write_documents,
                **_statement_documents("連結営業収益は35億9,530万ドルだった。"),
            )

        outcome = validated.outcomes[0]
        assert outcome.error is None
        assert "35億9,530万" in caplog.text
        assert "AAPL" in caplog.text

    def test_a_warned_symbol_is_still_rendered(self, write_documents):
        validated = _validated(
            write_documents,
            **_statement_documents("連結営業収益は35億9,530万ドルだった。"),
        )

        outcome = validated.outcomes[0]
        assert outcome.filings[0].analysis.facts[0].text.endswith("ドルだった。")
        assert outcome.verdict is not None

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(
                "連結営業収益は34億9,530万ドルだった。", id="the-corrected-figure"
            ),
            pytest.param(
                "前年同期の連結営業収益は29億2,818万ドルだった。",
                id="the-prior-year-figure",
            ),
            pytest.param(
                "連結営業収益は前年同期比19.4%増だった。", id="a-derived-percentage"
            ),
        ],
    )
    def test_a_faithful_restatement_is_not_warned_about(
        self, write_documents, caplog, text
    ):
        with caplog.at_level(logging.WARNING):
            validated = _validated(write_documents, **_statement_documents(text))

        assert validated.outcomes[0].error is None
        assert caplog.records == []

    def test_a_withheld_symbol_is_not_also_warned_about_its_figures(
        self, write_documents, caplog
    ):
        documents = _statement_documents(
            "連結営業収益は35億9,530万ドル。今すぐ買うべき。"
        )

        with caplog.at_level(logging.WARNING):
            validated = _validated(write_documents, **documents)

        assert validated.outcomes[0].error is not None
        assert "35億9,530万" not in caplog.text


#: The 2026-08-11 JBHT statement line Issue #131 was found against: the figure
#: is in thousands, so every faithful restatement of it converts a power of ten.
_STATEMENT_LINE = (
    "Condensed Consolidated Statements of Earnings (in thousands) "
    "Total operating revenues 3,495,296 2,928,181"
)
_STATEMENT_QUOTE = "Total operating revenues 3,495,296 2,928,181"


def _statement_documents(fact_text: str) -> dict[str, Any]:
    """Build `_validated` overrides whose only filing body is `_STATEMENT_LINE`."""
    candidate = input_payload()["candidates"][0]
    filing = {
        **candidate["filings"][0],
        "text": _STATEMENT_LINE,
        "coverage": {
            "original_chars": len(_STATEMENT_LINE),
            "exported_chars": len(_STATEMENT_LINE),
            "is_truncated": False,
            "selection_mode": "full",
            "sections": [],
        },
    }
    custom_input = input_payload(candidates=[{**candidate, "filings": [filing]}])
    symbol = symbol_payload(
        news_summary=None,
        filing_analyses=[
            _filing(
                facts=[
                    {
                        "text": fact_text,
                        "source_ids": [FILING_ID],
                        "evidence_quote": _STATEMENT_QUOTE,
                    }
                ]
            )
        ],
    )
    return {
        "analysis_input": custom_input,
        "input_digest": custom_input["input_digest"],
        "symbols": [symbol],
    }


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
                        facts=[
                            {
                                "text": _VIOLATION,
                                "source_ids": [NEWS_ID],
                                "evidence_quote": NEWS_QUOTE,
                            }
                        ]
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
                        _filing(
                            facts=[
                                {
                                    "text": _VIOLATION,
                                    "source_ids": [FILING_ID],
                                    "evidence_quote": FILING_QUOTE,
                                }
                            ]
                        )
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
            symbol="MSFT",
            news_summary=None,
            filing_analyses=[],
            screening_assessment={
                "summary": "今すぐ買う",
                "strengths": [],
                "concerns": [],
            },
            verdict={"recommendation": "proceed", "reasons": []},
        )
        other_candidate: dict[str, Any] = {
            "symbol": "MSFT",
            "score_breakdown": "<score_breakdown>\n</score_breakdown>\n",
            "risk_constraints": "<risk_constraints>\n</risk_constraints>\n",
            "decision_history": None,
            "news": [],
            "filings": [],
        }
        custom_input = input_payload(
            candidates=[input_payload()["candidates"][0], other_candidate]
        )
        input_path, result_path = write_documents(
            custom_input,
            result_payload(
                input_digest=custom_input["input_digest"], symbols=[clean, dirty]
            ),
        )
        # A per-symbol failure must never take its complete-result sibling down.
        validated = validate_analysis(
            load_analysis_input(input_path), load_analysis_result(result_path)
        )

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
    def test_a_symbol_absent_from_the_input_is_a_hard_failure(self, write_documents):
        input_path, result_path = write_documents(
            None, result_payload(symbols=[symbol_payload(symbol="TSLA")])
        )

        with pytest.raises(AnalysisIngestError, match=r"unexpected.*TSLA"):
            validate_analysis(
                load_analysis_input(input_path), load_analysis_result(result_path)
            )

    def test_a_symbol_absent_from_the_result_is_a_hard_failure(self, write_documents):
        input_path, result_path = write_documents(None, result_payload(symbols=[]))

        with pytest.raises(AnalysisIngestError, match=r"missing.*AAPL"):
            validate_analysis(
                load_analysis_input(input_path), load_analysis_result(result_path)
            )


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
                        "evidence_quote": "Employment Situation",
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

    @pytest.mark.parametrize(
        ("url", "is_linked"),
        [
            pytest.param("https://example.com/news", True, id="https"),
            pytest.param("http://example.com/news", True, id="http"),
            pytest.param("", False, id="empty"),
            pytest.param("javascript:alert(1)", False, id="javascript"),
            pytest.param("data:text/html,unsafe", False, id="data"),
            pytest.param("file:///tmp/unsafe", False, id="file"),
        ],
    )
    def test_only_http_and_https_input_urls_are_linkable(
        self, write_documents, url, is_linked
    ):
        candidate = input_payload()["candidates"][0]
        candidate["news"][0]["url"] = url
        custom_input = input_payload(candidates=[candidate])
        input_path, result_path = write_documents(
            custom_input,
            result_payload(input_digest=custom_input["input_digest"]),
        )

        validated = validate_analysis(
            load_analysis_input(input_path), load_analysis_result(result_path)
        )

        assert (NEWS_ID in validated.source_urls) is is_linked


class TestInputLoading:
    def test_a_malformed_input_document_is_a_hard_failure(self, write_documents):
        input_path, _result_path = write_documents(input_payload(as_of="not-a-date"))

        with pytest.raises(AnalysisIngestError, match="failed schema validation"):
            load_analysis_input(input_path)
