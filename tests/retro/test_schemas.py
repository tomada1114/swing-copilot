"""P8-31/P8-32: the `retro-input-v1` and `retro-result-v1` strict schemas.

The retrospective's two documents are the second machine-checked boundary in
this repository, so they are held to the same rules as the `analysis-*` pair:
unknown fields fail loudly, the version is a constant rather than a free
string, and the input's digest binds it to its own contents.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.schemas import canonical_json_digest
from swing_copilot.retro.schemas import (
    RETRO_INPUT_SCHEMA_VERSION,
    RETRO_RESULT_SCHEMA_VERSION,
    RetroInput,
    RetroResult,
    retro_input_digest,
)
from tests.retro.conftest import (
    narration_payload,
    proposal_payload,
    retro_result_payload,
)
from tests.retro.conftest import (
    retro_input_payload as _payload,
)
from tests.retro.conftest import (
    retro_input_unsigned_payload as _unsigned_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _news_supply_aggregate() -> dict[str, Any]:
    """Issue #154's supply cross-tab, as an export would write it."""
    return {
        "metric_id": "metric:news_supply",
        "sufficient_threshold": 5,
        "verdict_count": 2,
        "recorded_verdict_count": 1,
        "unrecorded_verdict_count": 1,
        "cells": [
            {
                "cell_id": "metric:news_supply:sparse:proceed",
                "level": "sparse",
                "recommendation": "proceed",
                "verdict_count": 1,
                "min_symbol_mention_items": 3,
                "max_symbol_mention_items": 3,
                "mean_symbol_mention_items": 3.0,
            },
            {
                "cell_id": "metric:news_supply:unrecorded:skip",
                "level": "unrecorded",
                "recommendation": "skip",
                "verdict_count": 1,
                "min_symbol_mention_items": None,
                "max_symbol_mention_items": None,
                "mean_symbol_mention_items": None,
            },
        ],
    }


class TestRetroInput:
    def test_accepts_and_round_trips_a_complete_document(self) -> None:
        document = RetroInput.model_validate(_payload())

        reloaded = RetroInput.model_validate(
            json.loads(document.model_dump_json(by_alias=False))
        )

        assert reloaded == document
        assert reloaded.surprises.items[0].symbol == "AAPL"
        assert reloaded.surprises.dropped_count == 1

    def test_pins_the_schema_version_to_the_constant(self) -> None:
        assert RETRO_INPUT_SCHEMA_VERSION == "retro-input-v1"
        with pytest.raises(ValidationError, match="schema_version"):
            RetroInput.model_validate(
                {**_payload(), "schema_version": "retro-input-v2"}
            )

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(
                lambda payload: payload.update({"unexpected": 1}), id="document"
            ),
            pytest.param(
                lambda payload: payload["evaluation"].update({"unexpected": 1}),
                id="evaluation",
            ),
            pytest.param(
                lambda payload: payload["aggregates"]["separation"][0].update(
                    {"unexpected": 1}
                ),
                id="metric",
            ),
            pytest.param(
                lambda payload: payload["surprises"]["items"][0].update(
                    {"unexpected": 1}
                ),
                id="surprise",
            ),
            pytest.param(
                lambda payload: payload["surprises"]["items"][0]["freshness"].update(
                    {"unexpected": 1}
                ),
                id="freshness",
            ),
            pytest.param(
                lambda payload: payload["proposals_ledger"].update({"unexpected": 1}),
                id="ledger",
            ),
        ],
    )
    def test_rejects_an_unknown_field_anywhere_in_the_document(
        self, mutate: Callable[[dict[str, Any]], None]
    ) -> None:
        payload = _payload()
        mutate(payload)

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RetroInput.model_validate(payload)

    def test_rejects_a_document_whose_digest_does_not_match_its_body(self) -> None:
        tampered = {**_payload(), "as_of": "2027-04-30"}

        with pytest.raises(ValidationError, match="input_digest"):
            RetroInput.model_validate(tampered)

    def test_rejects_a_blank_metric_id(self) -> None:
        payload = _unsigned_payload()
        payload["aggregates"]["separation"][0]["metric_id"] = "   "

        with pytest.raises(ValidationError):
            RetroInput.model_validate(
                {
                    **payload,
                    "input_digest": canonical_json_digest(
                        payload, excluded_field="input_digest"
                    ),
                }
            )

    def test_rejects_a_negative_sample_size(self) -> None:
        payload = _unsigned_payload()
        payload["aggregates"]["separation"][0]["sample_size"] = -1

        with pytest.raises(ValidationError):
            RetroInput.model_validate(
                {
                    **payload,
                    "input_digest": canonical_json_digest(
                        payload, excluded_field="input_digest"
                    ),
                }
            )

    def test_a_dossier_archived_before_the_supply_cross_tab_still_verifies(
        self,
    ) -> None:
        # Issue #154 added `aggregates.news_supply` and the per-surprise
        # `news_supply`. Both default to `None`, and the digest must ignore
        # that default, or every dossier written before the change would stop
        # verifying the day it landed.
        document = RetroInput.model_validate(_payload())

        assert document.aggregates.news_supply is None
        assert document.surprises.items[0].news_supply is None

    def test_a_dossier_carrying_the_supply_cross_tab_verifies_its_digest(
        self,
    ) -> None:
        payload = _unsigned_payload()
        payload["aggregates"]["news_supply"] = _news_supply_aggregate()

        document = RetroInput.model_validate(
            {**payload, "input_digest": retro_input_digest(payload)}
        )

        supply = document.aggregates.news_supply
        assert supply is not None
        assert supply.cells[0].cell_id == "metric:news_supply:sparse:proceed"

    def test_rejects_an_unknown_field_inside_a_supply_cell(self) -> None:
        payload = _unsigned_payload()
        aggregate = _news_supply_aggregate()
        aggregate["cells"][0]["unexpected"] = 1
        payload["aggregates"]["news_supply"] = aggregate

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RetroInput.model_validate(
                {**payload, "input_digest": retro_input_digest(payload)}
            )

    def test_accepts_an_empty_window_with_no_metrics_or_surprises(self) -> None:
        payload = _unsigned_payload()
        payload["aggregates"] = {
            "separation": [],
            "proceed_severe_miss_rate": [],
            "skip_hit_rate": [],
            "verdict_mix": {
                "metric_id": "verdict_mix",
                "run_count": 0,
                "verdict_count": 0,
                "proceed_count": 0,
                "skip_count": 0,
                "proceed_ratio": None,
                "is_flagged": False,
            },
        }
        payload["signal_performance"] = []
        payload["human_alignment"] = []
        payload["source_contribution"] = []
        payload["surprises"] = {"max_surprises": 5, "dropped_count": 0, "items": []}
        payload["notes"] = []

        document = RetroInput.model_validate(
            {
                **payload,
                "input_digest": canonical_json_digest(
                    payload, excluded_field="input_digest"
                ),
            }
        )

        assert document.surprises.items == []


class TestRetroResult:
    def test_accepts_and_round_trips_a_complete_document(self) -> None:
        document = RetroResult.model_validate(retro_result_payload())

        reloaded = RetroResult.model_validate(
            json.loads(document.model_dump_json(by_alias=False))
        )

        assert reloaded == document
        assert reloaded.proposals[0].level == "L1"
        assert reloaded.narrations[0].failure_class == "information_absent"

    def test_pins_the_schema_version_to_the_constant(self) -> None:
        assert RETRO_RESULT_SCHEMA_VERSION == "retro-result-v1"
        with pytest.raises(ValidationError, match="schema_version"):
            RetroResult.model_validate(
                retro_result_payload(schema_version="retro-result-v2")
            )

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(
                lambda payload: payload.update({"unexpected": 1}), id="document"
            ),
            pytest.param(
                lambda payload: payload["proposals"][0].update({"unexpected": 1}),
                id="proposal",
            ),
            pytest.param(
                lambda payload: payload["narrations"][0].update({"unexpected": 1}),
                id="narration",
            ),
        ],
    )
    def test_rejects_an_unknown_field_anywhere_in_the_document(
        self, mutate: Callable[[dict[str, Any]], None]
    ) -> None:
        payload = retro_result_payload()
        mutate(payload)

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RetroResult.model_validate(payload)

    def test_rejects_a_failure_class_outside_the_closed_enum(self) -> None:
        payload = retro_result_payload(
            narrations=[narration_payload(failure_class="bad_luck")]
        )

        with pytest.raises(ValidationError, match="failure_class"):
            RetroResult.model_validate(payload)

    @pytest.mark.parametrize(
        "failure_class",
        [
            "information_absent",
            "information_present_missed",
            "interpretation_error",
            "exogenous",
            "threshold_artifact",
        ],
    )
    def test_accepts_every_documented_failure_class(self, failure_class: str) -> None:
        document = RetroResult.model_validate(
            retro_result_payload(
                narrations=[narration_payload(failure_class=failure_class)]
            )
        )

        assert document.narrations[0].failure_class == failure_class

    def test_requires_a_narration_to_name_exactly_one_failure_class(self) -> None:
        payload = retro_result_payload(narrations=[narration_payload()])
        del payload["narrations"][0]["failure_class"]

        with pytest.raises(ValidationError, match="failure_class"):
            RetroResult.model_validate(payload)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("proposal_key", "  ", id="blank-key"),
            pytest.param("title", "", id="blank-title"),
            pytest.param("level", "L4", id="unknown-level"),
            pytest.param("evidence_basis", "vibes", id="unknown-basis"),
            pytest.param("evidence_refs", [], id="no-evidence"),
            pytest.param("risks", [], id="no-risks"),
        ],
    )
    def test_rejects_a_proposal_missing_a_mandatory_value(
        self, field: str, value: object
    ) -> None:
        payload = retro_result_payload(proposals=[proposal_payload(**{field: value})])

        with pytest.raises(ValidationError, match=field):
            RetroResult.model_validate(payload)

    def test_rejects_a_narration_citing_no_evidence(self) -> None:
        payload = retro_result_payload(narrations=[narration_payload(evidence_refs=[])])

        with pytest.raises(ValidationError, match="evidence_refs"):
            RetroResult.model_validate(payload)

    @pytest.mark.parametrize("level", ["L1", "L2"])
    def test_requires_a_verification_plan_for_an_applied_level(
        self, level: str
    ) -> None:
        payload = retro_result_payload(
            proposals=[proposal_payload(level=level, verification_plan=None)]
        )

        with pytest.raises(ValidationError, match="verification_plan"):
            RetroResult.model_validate(payload)

    def test_allows_a_design_review_proposal_without_a_verification_plan(self) -> None:
        document = RetroResult.model_validate(
            retro_result_payload(
                proposals=[proposal_payload(level="L3", verification_plan=None)]
            )
        )

        assert document.proposals[0].verification_plan is None

    def test_rejects_two_proposals_sharing_one_proposal_key(self) -> None:
        payload = retro_result_payload(
            proposals=[proposal_payload(), proposal_payload(title="別案")]
        )

        with pytest.raises(ValidationError, match="proposal_key"):
            RetroResult.model_validate(payload)

    def test_rejects_two_narrations_for_one_surprise(self) -> None:
        payload = retro_result_payload(
            narrations=[narration_payload(), narration_payload(narrative="別解釈")]
        )

        with pytest.raises(ValidationError, match="surprise_id"):
            RetroResult.model_validate(payload)

    def test_accepts_a_retrospective_that_proposes_nothing(self) -> None:
        document = RetroResult.model_validate(
            retro_result_payload(narrations=[], proposals=[])
        )

        assert document.proposals == []
        assert document.structural_review_note

    def test_requires_the_structural_review_note(self) -> None:
        payload = retro_result_payload()
        del payload["structural_review_note"]

        with pytest.raises(ValidationError, match="structural_review_note"):
            RetroResult.model_validate(payload)
