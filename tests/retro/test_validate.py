"""P8-32: `retro_result.json` is verified before any of it is rendered.

The rules mirror `analysis/validate.py`'s: an identity mismatch is a hard
failure for the whole retrospective, while a fabricated evidence reference, a
CON-03 violation, or an unjustified re-proposal withholds exactly one item and
leaves its siblings intact -- fail-closed, with no retry.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.analysis.validate import WITHHELD_MESSAGE
from swing_copilot.retro.schemas import RetroInput, RetroResult
from swing_copilot.retro.validate import (
    RetroIngestError,
    load_retro_input,
    load_retro_result,
    validate_retro_identity,
    validate_retro_result,
)
from tests.retro.conftest import (
    CITED_SOURCE_ID,
    SEPARATION_METRIC_ID,
    SURPRISE_ID,
    narration_payload,
    proposal_payload,
    retro_input_payload,
    retro_input_unsigned_payload,
    retro_result_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: A phrase `analysis/safety.py` forbids in any user-visible field (CON-03).
FORBIDDEN_TEXT = "この銘柄は今すぐ買うべき"


def _aggregates_with_news_supply() -> dict[str, Any]:
    """The fixture's aggregates plus Issue #154's supply cross-tab."""
    aggregates = retro_input_unsigned_payload()["aggregates"]
    return {
        **aggregates,
        "news_supply": {
            "metric_id": "metric:news_supply",
            "sufficient_threshold": 5,
            "verdict_count": 1,
            "recorded_verdict_count": 1,
            "unrecorded_verdict_count": 0,
            "cells": [
                {
                    "cell_id": "metric:news_supply:sparse:proceed",
                    "level": "sparse",
                    "recommendation": "proceed",
                    "verdict_count": 1,
                    "min_symbol_mention_items": 3,
                    "max_symbol_mention_items": 3,
                    "mean_symbol_mention_items": 3.0,
                }
            ],
        },
    }


def _input(**overrides: Any) -> RetroInput:
    return RetroInput.model_validate(retro_input_payload(**overrides))


def _result(**overrides: Any) -> RetroResult:
    return RetroResult.model_validate(retro_result_payload(**overrides))


def _validated(result: RetroResult, closed: frozenset[str] = frozenset()) -> Any:
    return validate_retro_result(_input(), result, closed)


class TestArtifactIdentity:
    def test_accepts_a_result_answering_the_exported_dossier(self) -> None:
        validate_retro_identity(_input(), _result())

    def test_rejects_a_result_for_another_as_of(self) -> None:
        result = _result(as_of="2027-04-30")

        with pytest.raises(RetroIngestError, match="as_of"):
            validate_retro_identity(_input(), result)

    def test_rejects_a_result_copying_a_foreign_input_digest(self) -> None:
        result = _result(input_digest="a" * 64)

        with pytest.raises(RetroIngestError, match="input_digest"):
            validate_retro_identity(_input(), result)


class TestEvidenceReferences:
    def test_accepts_every_identifier_the_dossier_supplied(self) -> None:
        validated = _validated(
            _result(
                proposals=[
                    proposal_payload(
                        evidence_refs=[
                            SEPARATION_METRIC_ID,
                            "metric:human_alignment:followed:proceed:5d",
                            "metric:source_contribution:news:finnhub",
                            SURPRISE_ID,
                            CITED_SOURCE_ID,
                            "finnhub:9",
                        ]
                    )
                ]
            )
        )

        assert validated.withheld == ()
        assert len(validated.proposals) == 1

    def test_accepts_the_verdict_mix_metric_id(self) -> None:
        # `verdict_mix` is the aggregate that stays measurable when a
        # zero-proceed window silences the rate metrics, so a proposal about
        # that very window has nothing else to cite. Its ID is the bare
        # `verdict_mix` the dossier carries -- copied, never constructed.
        validated = _validated(
            _result(proposals=[proposal_payload(evidence_refs=["verdict_mix"])])
        )

        assert validated.withheld == ()
        assert len(validated.proposals) == 1

    def test_accepts_the_supply_cross_tab_ids_when_the_dossier_carries_them(
        self,
    ) -> None:
        # Issue #154: a proposal about the `sufficient` threshold has to be
        # able to cite the evidence for it, both the whole cross-tab and one
        # `(level, recommendation)` cell.
        retro_input = _input(aggregates=_aggregates_with_news_supply())
        result = _result(
            proposals=[
                proposal_payload(
                    evidence_refs=[
                        "metric:news_supply",
                        "metric:news_supply:sparse:proceed",
                    ]
                )
            ],
        )

        validated = validate_retro_result(retro_input, result, frozenset())

        assert validated.withheld == ()

    def test_withholds_a_supply_reference_a_dossier_without_the_cross_tab(
        self,
    ) -> None:
        # The evidence space is closed: an older dossier that never measured
        # the supply cannot have a proposal argue from it.
        validated = _validated(
            _result(
                proposals=[proposal_payload(evidence_refs=["metric:news_supply"])],
            )
        )

        assert len(validated.withheld) == 1
        assert "metric:news_supply" in validated.withheld[0].reason

    def test_withholds_only_the_proposal_citing_an_invented_reference(self) -> None:
        validated = _validated(
            _result(
                proposals=[
                    proposal_payload(evidence_refs=["metric:invented:9d"]),
                    proposal_payload(
                        proposal_key="config:analysis.max_news_items", title="健全な案"
                    ),
                ]
            )
        )

        assert [item.title for item in validated.proposals] == ["健全な案"]
        assert len(validated.withheld) == 1
        assert validated.withheld[0].identifier == (
            "config:postmortem.severe_threshold_pct"
        )
        assert "metric:invented:9d" in validated.withheld[0].reason

    def test_withholds_a_narration_about_a_surprise_that_was_never_exported(
        self,
    ) -> None:
        validated = _validated(
            _result(narrations=[narration_payload(surprise_id="surprise:unknown:ZZZ")])
        )

        assert validated.narrations == ()
        assert validated.withheld[0].kind == "narration"
        assert "surprise:unknown:ZZZ" in validated.withheld[0].reason

    def test_withholds_a_narration_citing_an_invented_reference(self) -> None:
        validated = _validated(
            _result(
                narrations=[
                    narration_payload(evidence_refs=[SURPRISE_ID, "finnhub:invented"])
                ]
            )
        )

        assert validated.narrations == ()
        assert "finnhub:invented" in validated.withheld[0].reason


class TestCon03:
    @pytest.mark.parametrize(
        "field",
        ["title", "claim", "expected_effect", "verification_plan", "target"],
    )
    def test_withholds_a_proposal_whose_visible_text_violates_con03(
        self, field: str
    ) -> None:
        validated = _validated(
            _result(proposals=[proposal_payload(**{field: FORBIDDEN_TEXT})])
        )

        assert validated.proposals == ()
        assert "CON-03" in validated.withheld[0].reason

    def test_withholds_a_proposal_whose_risks_violate_con03(self) -> None:
        validated = _validated(
            _result(proposals=[proposal_payload(risks=["問題なし", FORBIDDEN_TEXT])])
        )

        assert validated.proposals == ()
        assert "CON-03" in validated.withheld[0].reason

    def test_does_not_echo_the_identifier_of_a_con03_violating_item(self) -> None:
        validated = _validated(
            _result(proposals=[proposal_payload(proposal_key=FORBIDDEN_TEXT)])
        )

        assert validated.withheld[0].identifier is None
        assert FORBIDDEN_TEXT not in validated.withheld[0].reason

    def test_withholds_only_the_violating_item(self) -> None:
        validated = _validated(
            _result(
                narrations=[narration_payload(narrative=FORBIDDEN_TEXT)],
                proposals=[proposal_payload()],
            )
        )

        assert validated.narrations == ()
        assert len(validated.proposals) == 1
        assert [item.kind for item in validated.withheld] == ["narration"]

    def test_replaces_a_violating_structural_review_note(self) -> None:
        validated = _validated(_result(structural_review_note=FORBIDDEN_TEXT))

        assert validated.structural_review_note == WITHHELD_MESSAGE
        assert validated.withheld[0].kind == "structural_review_note"
        assert len(validated.proposals) == 1

    def test_checks_con03_before_evidence_so_a_violation_is_never_echoed(self) -> None:
        validated = _validated(
            _result(
                proposals=[
                    proposal_payload(
                        proposal_key=FORBIDDEN_TEXT, evidence_refs=["metric:invented"]
                    )
                ]
            )
        )

        assert validated.withheld[0].identifier is None
        assert "CON-03" in validated.withheld[0].reason


class TestReproposalGuard:
    def test_withholds_a_proposal_closed_by_the_ledger(self) -> None:
        validated = _validated(
            _result(), frozenset({"config:postmortem.severe_threshold_pct"})
        )

        assert validated.proposals == ()
        assert "reopen_justification" in validated.withheld[0].reason

    def test_accepts_a_reopened_proposal_that_justifies_itself(self) -> None:
        validated = _validated(
            _result(
                proposals=[
                    proposal_payload(reopen_justification="20 日側の新しい証拠が出た")
                ]
            ),
            frozenset({"config:postmortem.severe_threshold_pct"}),
        )

        assert validated.withheld == ()
        assert len(validated.proposals) == 1

    def test_leaves_an_unrelated_proposal_alone(self) -> None:
        validated = _validated(_result(), frozenset({"config:some.other_key"}))

        assert len(validated.proposals) == 1


class TestLoading:
    def test_reads_both_documents_from_disk(self, tmp_path: Path) -> None:
        input_path = tmp_path / "retro_input.json"
        result_path = tmp_path / "retro_result.json"
        input_path.write_text(json.dumps(retro_input_payload()), encoding="utf-8")
        result_path.write_text(json.dumps(retro_result_payload()), encoding="utf-8")

        assert (
            load_retro_input(input_path).as_of == load_retro_result(result_path).as_of
        )

    def test_rejects_a_missing_document(self, tmp_path: Path) -> None:
        with pytest.raises(RetroIngestError, match="could not be read"):
            load_retro_result(tmp_path / "absent.json")

    @pytest.mark.parametrize(
        ("load", "filename"),
        [
            pytest.param(load_retro_input, "retro_input.json", id="input"),
            pytest.param(load_retro_result, "retro_result.json", id="result"),
        ],
    )
    def test_rejects_a_wrongly_encoded_document(
        self,
        tmp_path: Path,
        load: Callable[[Path], RetroInput | RetroResult],
        filename: str,
    ) -> None:
        """A non-UTF-8 document must arrive as `RetroIngestError` (Issue #164).

        `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so the read
        step used to let it escape uncaught and callers that tell "broken
        artifact" from "unexpected fault" by exception type saw the wrong kind
        of failure.
        """
        path = tmp_path / filename
        path.write_bytes(b'{"as_of": "\xff\xfe"}')

        with pytest.raises(RetroIngestError, match="could not be read"):
            load(path)

    def test_rejects_a_document_that_is_not_json(self, tmp_path: Path) -> None:
        path = tmp_path / "retro_result.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(RetroIngestError, match="not valid JSON"):
            load_retro_result(path)

    def test_rejects_a_document_with_an_unknown_field(self, tmp_path: Path) -> None:
        path = tmp_path / "retro_result.json"
        path.write_text(
            json.dumps(retro_result_payload(unexpected="x")), encoding="utf-8"
        )

        with pytest.raises(RetroIngestError, match="schema validation"):
            load_retro_result(path)
