"""The `analysis_work/` fragment contract (`analysis/fragment.py`, Issue #132)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.fragment import (
    AnalysisFragment,
    as_symbol_analysis,
    fragment_filename_error,
    verify_fragment,
)
from swing_copilot.analysis.schemas import (
    AnalysisInput,
    AnalysisResult,
    filing_body_digest,
)
from swing_copilot.analysis.validate import validate_analysis
from tests.analysis.conftest import (
    CALENDAR_ID,
    FILING_ID,
    FILING_QUOTE,
    NEWS_ID,
    NEWS_QUOTE,
    exported_filing_digests,
    fragment_payload,
    input_payload,
    result_payload,
    symbol_payload,
)

#: A fact whose quote is present in the news body, used as the clean baseline
#: the violating variants below are derived from.
_CLEAN_FACT = {
    "text": "A new product line was announced.",
    "source_ids": [NEWS_ID],
    "evidence_quote": NEWS_QUOTE,
}
#: "you should buy" in full-width Latin letters, written as escapes so the
#: source stays free of lookalike glyphs. NFKC folds it back to ASCII, so
#: the CON-03 check sees an imperative a literal scan of the file cannot.
_FULLWIDTH_IMPERATIVE = (
    "\uff59\uff4f\uff55 \uff53\uff48\uff4f\uff55\uff4c\uff44 \uff42\uff55\uff59"
)


def _analysis_input() -> AnalysisInput:
    return AnalysisInput.model_validate(input_payload())


def _fragment_error(news_summary: dict[str, Any] | None) -> str | None:
    """Verify one news payload the way an expert would before writing it out."""
    fragment = AnalysisFragment.model_validate(
        fragment_payload(news_summary=news_summary)
    )
    return verify_fragment(_analysis_input(), fragment)


def _ingest_error(news_summary: dict[str, Any] | None) -> str | None:
    """Verify the same payload the way `copilot-ingest-analysis` would.

    Everything the fragment does not carry is set to the empty stand-in
    `as_symbol_analysis` supplies, so the only difference between the two paths
    is the document the payload arrived in.
    """
    result = AnalysisResult.model_validate(
        result_payload(
            symbols=[
                symbol_payload(
                    news_summary=news_summary,
                    filing_analyses=[],
                    screening_assessment={"summary": ""},
                    verdict={"recommendation": "skip", "reasons": []},
                )
            ]
        )
    )
    return validate_analysis(_analysis_input(), result).outcomes[0].error


class TestFragmentEnvelope:
    def test_a_contract_satisfying_fragment_reports_no_violation(self):
        fragment = AnalysisFragment.model_validate(fragment_payload())

        assert verify_fragment(_analysis_input(), fragment) is None

    @pytest.mark.parametrize(
        "kind",
        [pytest.param(kind, id=kind) for kind in ("news", "filings", "screening")],
    )
    def test_each_expert_kind_is_derived_from_its_payload_key(self, kind):
        fragment = AnalysisFragment.model_validate(fragment_payload(kind))

        assert fragment.kind == kind
        assert verify_fragment(_analysis_input(), fragment) is None

    def test_a_fragment_without_any_payload_key_is_rejected(self):
        payload = fragment_payload()
        del payload["news_summary"]

        with pytest.raises(ValidationError, match="exactly one payload key"):
            AnalysisFragment.model_validate(payload)

    def test_a_fragment_carrying_two_experts_answers_is_rejected(self):
        payload = fragment_payload(filing_analyses=[])

        with pytest.raises(ValidationError, match="exactly one payload key"):
            AnalysisFragment.model_validate(payload)

    def test_an_invented_field_is_rejected_like_the_result_schema(self):
        payload = fragment_payload(sentiment="positive")

        with pytest.raises(ValidationError, match="sentiment"):
            AnalysisFragment.model_validate(payload)

    def test_a_null_news_summary_is_an_analyzed_but_empty_answer(self):
        fragment = AnalysisFragment.model_validate(fragment_payload(news_summary=None))

        assert fragment.kind == "news"
        assert verify_fragment(_analysis_input(), fragment) is None

    def test_a_null_screening_assessment_is_rejected(self):
        payload = fragment_payload("screening", screening_assessment=None)

        with pytest.raises(ValidationError, match="must not be null"):
            AnalysisFragment.model_validate(payload)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("ac_check", "   ", id="blank-ac-check"),
            pytest.param("symbol", "", id="empty-symbol"),
            pytest.param("input_digest", "abc", id="short-digest"),
            pytest.param("run_id", "not-a-uuid", id="malformed-run-id"),
            pytest.param("as_of", "2027-13-40", id="impossible-date"),
        ],
    )
    def test_a_malformed_envelope_field_is_rejected(self, field, value):
        with pytest.raises(ValidationError, match=field):
            AnalysisFragment.model_validate(fragment_payload(**{field: value}))

    def test_a_fragment_without_an_ac_check_is_rejected(self):
        payload = fragment_payload()
        del payload["ac_check"]

        with pytest.raises(ValidationError, match="ac_check"):
            AnalysisFragment.model_validate(payload)


class TestFragmentIdentity:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param(
                "run_id", "99999999-9999-4999-8999-999999999999", id="another-run"
            ),
            pytest.param("as_of", "2027-02-28", id="another-day"),
            pytest.param("input_digest", "f" * 64, id="another-input"),
        ],
    )
    def test_a_fragment_bound_to_another_run_names_the_mismatch(self, field, value):
        fragment = AnalysisFragment.model_validate(fragment_payload(**{field: value}))

        error = verify_fragment(_analysis_input(), fragment)

        assert error is not None
        assert error.startswith(f"{field} {value} does not match analysis_input.json")

    def test_identity_is_reported_before_the_content_checks(self):
        """A stale fragment must not be blamed for a provenance failure."""
        fragment = AnalysisFragment.model_validate(
            fragment_payload(
                input_digest="f" * 64,
                news_summary={
                    "facts": [{**_CLEAN_FACT, "source_ids": ["news-invented"]}],
                    "interpretation": [],
                    "risk_flags": [],
                },
            )
        )

        error = verify_fragment(_analysis_input(), fragment)

        assert error is not None
        assert error.startswith("input_digest")

    @pytest.mark.parametrize(
        "kind",
        [pytest.param(kind, id=kind) for kind in ("news", "filings", "screening")],
    )
    def test_a_symbol_the_input_never_offered_is_withheld(self, kind):
        """Named as an absent symbol, not as a body or identity mismatch.

        The filings key is compared against a candidate that does not exist, so
        the reuse check has to defer here rather than blame the digests.
        """
        fragment = AnalysisFragment.model_validate(
            fragment_payload(kind, symbol="MSFT")
        )

        assert (
            verify_fragment(_analysis_input(), fragment)
            == "symbol is absent from analysis_input.json"
        )


#: A run other than the one `input_payload()` describes. Reusing a fragment
#: across days means exactly this: its envelope names a run that has ended.
_ANOTHER_RUN = {
    "run_id": "99999999-9999-4999-8999-999999999999",
    "as_of": "2027-02-28",
    "input_digest": "f" * 64,
}


def _input_with_filing(**filing_overrides: Any) -> dict[str, Any]:
    """`input_payload()` with its one filing altered."""
    payload = input_payload()
    candidate = payload["candidates"][0]
    return input_payload(
        candidates=[
            {**candidate, "filings": [{**candidate["filings"][0], **filing_overrides}]}
        ]
    )


class TestFilingFragmentReuse:
    """Issue #261: a filing reading is keyed on its bodies, not on its run."""

    def test_an_unchanged_filing_body_makes_yesterdays_reading_reusable(self):
        fragment = AnalysisFragment.model_validate(
            fragment_payload("filings", **_ANOTHER_RUN)
        )

        assert verify_fragment(_analysis_input(), fragment) is None

    def test_a_changed_filing_body_forces_a_re_analysis(self):
        """The same accession, re-exported with more of its text, is new work."""
        analysis_input = AnalysisInput.model_validate(
            _input_with_filing(
                text="Quarterly report body. Revenue rose year over year.",
                coverage={
                    "original_chars": 51,
                    "exported_chars": 51,
                    "is_truncated": False,
                    "selection_mode": "full",
                    "exhibit_truncated": False,
                    "sections": [],
                },
            )
        )
        fragment = AnalysisFragment.model_validate(fragment_payload("filings"))

        error = verify_fragment(analysis_input, fragment)

        assert error is not None
        assert error.startswith("filing_body_digests do not match")
        assert f"changed=['{FILING_ID}']" in error

    def test_a_filing_the_reading_never_saw_forces_a_re_analysis(self):
        """An 8-K filed overnight must not be covered by yesterday's reading."""
        payload = input_payload()
        candidate = payload["candidates"][0]
        analysis_input = AnalysisInput.model_validate(
            input_payload(
                candidates=[
                    {
                        **candidate,
                        "filings": [
                            *candidate["filings"],
                            {
                                **candidate["filings"][0],
                                "source_id": "edgar:0000320193-27-000002",
                                "form_type": "8-K",
                            },
                        ],
                    }
                ]
            )
        )
        fragment = AnalysisFragment.model_validate(fragment_payload("filings"))

        error = verify_fragment(analysis_input, fragment)

        assert error is not None
        assert "newly_exported=['edgar:0000320193-27-000002']" in error

    def test_a_filing_the_input_no_longer_exports_forces_a_re_analysis(self):
        fragment = AnalysisFragment.model_validate(
            fragment_payload(
                "filings",
                filing_body_digests={
                    **exported_filing_digests(),
                    "edgar:0000320193-27-000009": "a" * 64,
                },
            )
        )

        error = verify_fragment(_analysis_input(), fragment)

        assert error is not None
        assert "no_longer_exported=['edgar:0000320193-27-000009']" in error

    def test_a_filings_fragment_without_the_digests_is_rejected(self):
        payload = fragment_payload("filings")
        del payload["filing_body_digests"]

        with pytest.raises(ValidationError, match="must declare filing_body_digests"):
            AnalysisFragment.model_validate(payload)

    def test_the_digests_must_cover_every_analyzed_filing(self):
        """A reading may skip a filing; it may not read one it never declared."""
        payload = fragment_payload(
            "filings", filing_body_digests={"edgar:other": "b" * 64}
        )

        with pytest.raises(ValidationError, match="must cover every analyzed filing"):
            AnalysisFragment.model_validate(payload)

    def test_a_reading_covering_a_filing_it_wrote_nothing_about_is_allowed(self):
        """A read filing with nothing to say stays distinct from an unread one."""
        fragment = AnalysisFragment.model_validate(
            fragment_payload("filings", **_ANOTHER_RUN, filing_analyses=[])
        )

        assert verify_fragment(_analysis_input(), fragment) is None

    @pytest.mark.parametrize(
        "kind",
        [pytest.param(kind, id=kind) for kind in ("news", "screening")],
    )
    def test_the_as_of_dependent_readings_are_never_reusable_across_runs(self, kind):
        """`news_summary` and `screening_assessment` keep the three-value key.

        One reads the day's articles and the other the day's deterministic
        score, so an unchanged filing body says nothing about whether either is
        still current.
        """
        fragment = AnalysisFragment.model_validate(
            fragment_payload(kind, **_ANOTHER_RUN)
        )

        error = verify_fragment(_analysis_input(), fragment)

        assert error is not None
        assert error.startswith("run_id 99999999-9999-4999-8999-999999999999")

    @pytest.mark.parametrize(
        "kind",
        [pytest.param(kind, id=kind) for kind in ("news", "screening")],
    )
    def test_an_as_of_dependent_fragment_may_not_claim_body_digests(self, kind):
        payload = fragment_payload(kind, filing_body_digests=exported_filing_digests())

        with pytest.raises(ValidationError, match="filings fragment alone"):
            AnalysisFragment.model_validate(payload)

    def test_a_wrongly_reused_reading_still_fails_the_verbatim_quote_check(self):
        """The fail-closed net that makes the relaxed key safe (Issue #261).

        The digests are forged to match, so the reuse key is defeated
        outright -- exactly the state a bug in the key would produce. The
        reading is still withheld, because its `evidence_quote` was taken from
        the body the *earlier* run exported and no longer occurs in this one.
        """
        analysis_input = AnalysisInput.model_validate(
            _input_with_filing(
                text="Annual report body.",
                coverage={
                    "original_chars": 19,
                    "exported_chars": 19,
                    "is_truncated": False,
                    "selection_mode": "full",
                    "exhibit_truncated": False,
                    "sections": [],
                },
            )
        )
        forged = {
            item.source_id: filing_body_digest(item.text)
            for item in analysis_input.candidates[0].filings
        }
        fragment = AnalysisFragment.model_validate(
            fragment_payload("filings", **_ANOTHER_RUN, filing_body_digests=forged)
        )

        error = verify_fragment(analysis_input, fragment)

        assert error is not None
        assert error.startswith("evidence_quote is absent from every cited source body")
        assert FILING_QUOTE in error


class TestFragmentFilename:
    def test_the_contracted_filename_agrees_with_the_payload(self):
        fragment = AnalysisFragment.model_validate(fragment_payload())

        assert fragment_filename_error(Path("news-AAPL.json"), fragment) is None

    def test_a_filename_naming_another_symbol_is_reported(self):
        fragment = AnalysisFragment.model_validate(fragment_payload())

        error = fragment_filename_error(Path("news-MSFT.json"), fragment)

        assert error == (
            "filename declares symbol 'MSFT' but the payload declares 'AAPL'"
        )

    def test_a_filename_naming_another_expert_is_reported(self):
        fragment = AnalysisFragment.model_validate(fragment_payload())

        error = fragment_filename_error(Path("filings-AAPL.json"), fragment)

        assert error == (
            "filename declares the filings expert but the payload is news_summary"
        )

    def test_a_name_outside_the_convention_is_not_judged(self):
        fragment = AnalysisFragment.model_validate(fragment_payload())

        assert fragment_filename_error(Path("draft.json"), fragment) is None


class TestSharedCheckMatchesIngest:
    """The pre-flight check must be the ingest check, not a lookalike."""

    @pytest.mark.parametrize(
        "news_summary",
        [
            pytest.param(
                {
                    "facts": [{**_CLEAN_FACT, "source_ids": ["news-invented"]}],
                    "interpretation": [],
                    "risk_flags": [],
                },
                id="source-id-absent-from-the-input",
            ),
            pytest.param(
                {
                    "facts": [
                        {**_CLEAN_FACT, "evidence_quote": "recalled every unit sold"}
                    ],
                    "interpretation": [],
                    "risk_flags": [],
                },
                id="quote-absent-from-the-cited-body",
            ),
            pytest.param(
                {
                    "facts": [{**_CLEAN_FACT, "source_ids": [FILING_ID]}],
                    "interpretation": [],
                    "risk_flags": [],
                },
                id="quote-from-another-source-of-the-same-symbol",
            ),
            pytest.param(
                {
                    "facts": [_CLEAN_FACT],
                    "interpretation": ["この銘柄は買うべきである。"],
                    "risk_flags": [],
                },
                id="con03-imperative",
            ),
            pytest.param(
                {
                    "facts": [_CLEAN_FACT],
                    "interpretation": [_FULLWIDTH_IMPERATIVE],
                    "risk_flags": [],
                },
                id="con03-imperative-hidden-behind-full-width-glyphs",
            ),
            pytest.param(
                {
                    "facts": [_CLEAN_FACT],
                    "interpretation": [],
                    "risk_flags": ["投資家心理が悪化している。"],
                },
                id="con03-unevidenced-behavioral-claim",
            ),
        ],
    )
    def test_a_violating_payload_fails_identically_in_both_paths(self, news_summary):
        fragment_error = _fragment_error(news_summary)

        assert fragment_error is not None
        assert fragment_error == _ingest_error(news_summary)

    @pytest.mark.parametrize(
        "news_summary",
        [
            pytest.param(None, id="analyzed-but-empty"),
            pytest.param(
                {
                    "facts": [
                        {
                            **_CLEAN_FACT,
                            # Case, a non-breaking space, and doubled spacing:
                            # presentation only, which normalization folds away.
                            "evidence_quote": "ANNOUNCED\u00a0A NEW  PRODUCT LINE",
                        }
                    ],
                    "interpretation": ["需要を支える可能性がある。"],
                    "risk_flags": [],
                },
                id="quote-differing-only-in-presentation",
            ),
            pytest.param(
                {
                    "facts": [
                        {
                            "text": "An employment release is scheduled.",
                            "source_ids": [CALENDAR_ID],
                            "evidence_quote": "Employment Situation",
                        }
                    ],
                    "interpretation": [],
                    "risk_flags": [],
                },
                id="run-wide-calendar-citation",
            ),
        ],
    )
    def test_a_conforming_payload_passes_identically_in_both_paths(self, news_summary):
        assert _fragment_error(news_summary) is None
        assert _ingest_error(news_summary) is None


class TestSymbolAnalysisLifting:
    def test_the_stand_in_sections_contribute_nothing_checkable(self):
        """The filled-in sections must not add a citation or displayable text."""
        analysis = as_symbol_analysis(
            AnalysisFragment.model_validate(fragment_payload())
        )

        assert analysis.screening_assessment.summary == ""
        assert analysis.screening_assessment.strengths == []
        assert analysis.screening_assessment.concerns == []
        assert analysis.verdict.reasons == []
        assert analysis.filing_analyses == []

    def test_a_filings_fragment_keeps_its_own_payload_and_no_news(self):
        analysis = as_symbol_analysis(
            AnalysisFragment.model_validate(fragment_payload("filings"))
        )

        assert analysis.news_summary is None
        assert [item.source_id for item in analysis.filing_analyses] == [FILING_ID]
