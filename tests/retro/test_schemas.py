"""P8-31/P8-32: the `retro-input-v1` and `retro-result-v1` strict schemas.

The retrospective's two documents are the second machine-checked boundary in
this repository, so they are held to the same rules as the `analysis-*` pair:
unknown fields fail loudly, the version is a constant rather than a free
string, and the input's digest binds it to its own contents.
"""

from __future__ import annotations

import json
from copy import deepcopy
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


def _input_coverage() -> dict[str, Any]:
    """Issue #157's coverage block, as an export wrote it before Issue #267."""
    return {
        "filing_count": 3,
        "truncated_filing_count": 1,
        "exhibit_truncated_filing_count": 0,
        "fallback_filing_count": 1,
        "omitted_filing_count": 0,
        "severe_miss_symbol_count_with_gap": 1,
        "severe_miss_symbol_count_without_gap": 0,
        "severe_miss_symbol_count_unknown": 0,
    }


def _input_coverage_before_issue_157() -> dict[str, Any]:
    """The coverage block as an export wrote it before Issue #157."""
    block = _input_coverage()
    del block["exhibit_truncated_filing_count"]
    return block


def _input_coverage_since_issue_267() -> dict[str, Any]:
    """The coverage block an export writes today, both counts present."""
    return {**_input_coverage(), "starved_filing_count": 0}


def _filing_coverage_before_issue_157() -> dict[str, Any]:
    """One archived filing coverage as it was written before Issue #157.

    `exhibit_truncated` and `sections` are the two keys the field addition
    made appear in a re-read of a document that never carried them.
    """
    return {
        "source_id": "edgar:0000320193-27-000001",
        "coverage": {
            "original_chars": 100,
            "exported_chars": 100,
            "is_truncated": False,
            "selection_mode": "full",
        },
    }


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

    def test_a_dossier_written_before_issue_190_keeps_its_digest(self) -> None:
        # The DoD's regression: re-hashing an archived dossier through the
        # widened schema must reproduce the digest it was written with, or
        # every stored `retro_result.json` stops verifying the day the
        # dispersion fields land.
        payload = _unsigned_payload()
        archived_digest = canonical_json_digest(payload, excluded_field="input_digest")

        rehydrated = RetroInput.model_validate(
            {**payload, "input_digest": archived_digest}
        ).model_dump(mode="json")

        assert rehydrated["aggregates"]["separation"][0]["stderr"] is None
        assert rehydrated["aggregates"]["tracked_performance"] is None
        assert retro_input_digest(rehydrated) == archived_digest

    def test_a_dossier_written_before_issue_189_keeps_its_digest(self) -> None:
        # Same regression, for the two ledger blocks: an archived dossier has
        # no `failure_class_history` and no `aggregates_by_config`, and its
        # absent form must hash exactly as it did before the fields existed.
        payload = _unsigned_payload()
        archived_digest = canonical_json_digest(payload, excluded_field="input_digest")

        rehydrated = RetroInput.model_validate(
            {**payload, "input_digest": archived_digest}
        ).model_dump(mode="json")

        assert rehydrated["failure_class_history"] is None
        assert rehydrated["aggregates_by_config"] == []
        assert retro_input_digest(rehydrated) == archived_digest

    def test_a_dossier_written_before_issue_267_keeps_its_digest(self) -> None:
        # Same regression, for the starved-export count: a dossier archived
        # while `input_coverage` existed but that count did not must still
        # hash to the digest it was written with.
        payload = _unsigned_payload()
        payload["input_coverage"] = _input_coverage()
        archived_digest = canonical_json_digest(payload, excluded_field="input_digest")

        rehydrated = RetroInput.model_validate(
            {**payload, "input_digest": archived_digest}
        ).model_dump(mode="json")

        assert rehydrated["input_coverage"]["starved_filing_count"] == 0
        assert retro_input_digest(rehydrated) == archived_digest

    def test_a_dossier_from_before_verdict_mix_was_required_fails_to_parse(
        self,
    ) -> None:
        # Issue #293: unlike the generation gaps above, this one is not
        # rescued. `aggregates.verdict_mix` (Issue #139) has no default, so
        # the one archived dossier written before it existed --
        # `reports/retro/2026-07-30/retro_input.json` -- cannot be read back,
        # even though #276's `exclude_unset` digest reproduction covers every
        # other generation gap: that mechanism can only absorb fields whose
        # default is the "not measured" form, and `verdict_mix` is required.
        # Pin that the failure is the missing field itself, surfaced before
        # pydantic ever reaches the digest model-validator -- not a digest
        # mismatch. Reading this generation is a decided non-goal (see
        # docs/04_detailed_design.md §3.23.4), not something to patch with a
        # legacy default.
        payload = _unsigned_payload()
        del payload["aggregates"]["verdict_mix"]
        archived_digest = canonical_json_digest(payload, excluded_field="input_digest")

        with pytest.raises(ValidationError) as excinfo:
            RetroInput.model_validate({**payload, "input_digest": archived_digest})

        errors = excinfo.value.errors()
        assert len(errors) == 1
        assert errors[0]["type"] == "missing"
        assert errors[0]["loc"] == ("aggregates", "verdict_mix")
        assert errors[0]["msg"] == "Field required"

    @pytest.mark.parametrize(
        "build_coverage",
        [
            pytest.param(_input_coverage_before_issue_157, id="before-issue-157"),
            pytest.param(_input_coverage, id="issue-157-to-issue-267"),
            pytest.param(_input_coverage_since_issue_267, id="since-issue-267"),
        ],
    )
    def test_every_input_coverage_generation_verifies_its_own_digest(
        self, build_coverage: Callable[[], dict[str, Any]]
    ) -> None:
        # Issue #276: the block gained `exhibit_truncated_filing_count` at
        # Issue #157 and `starved_filing_count` at Issue #267, so three shapes
        # sit in `reports/retro/`. Each was signed by the export of its own
        # day, and re-reading it through today's widened schema has to
        # reproduce that signature -- the pre-#157 generation did not, because
        # a re-read materialized `exhibit_truncated_filing_count: 0` into a
        # document whose stored bytes never carried the key.
        payload = _unsigned_payload()
        payload["input_coverage"] = build_coverage()
        archived_digest = retro_input_digest(payload)

        document = RetroInput.model_validate(
            {**payload, "input_digest": archived_digest}
        )

        assert document.input_coverage is not None
        assert document.input_coverage.exhibit_truncated_filing_count == 0
        assert document.input_coverage.starved_filing_count == 0

    @pytest.mark.parametrize(
        ("build_stored", "build_signed_as"),
        [
            pytest.param(
                _input_coverage_before_issue_157,
                _input_coverage,
                id="pre-157-body-signed-as-157",
            ),
            pytest.param(
                _input_coverage,
                _input_coverage_before_issue_157,
                id="157-body-signed-as-pre-157",
            ),
        ],
    )
    def test_rejects_an_input_coverage_signed_as_another_generation(
        self,
        build_stored: Callable[[], dict[str, Any]],
        build_signed_as: Callable[[], dict[str, Any]],
    ) -> None:
        # The other half of Issue #276: hashing the keys the document itself
        # carries must still tell the generations apart. A body from one
        # generation carrying the neighbouring generation's signature is a
        # document that was edited after it was written.
        payload = _unsigned_payload()
        payload["input_coverage"] = build_stored()
        signed_as = deepcopy(payload)
        signed_as["input_coverage"] = build_signed_as()

        with pytest.raises(ValidationError, match="input_digest"):
            RetroInput.model_validate(
                {**payload, "input_digest": retro_input_digest(signed_as)}
            )

    def test_a_counted_exhibit_truncation_changes_a_fresh_documents_digest(
        self,
    ) -> None:
        # `exhibit_truncated_filing_count` cannot be dropped by value the way
        # the `None`/`[]` defaults are: 0 is a measurement, and a dossier that
        # counted two collection-stage truncations must hash differently from
        # one that counted none -- and from one that never counted at all.
        payload = _unsigned_payload()
        payload["input_coverage"] = {
            **_input_coverage(),
            "exhibit_truncated_filing_count": 2,
        }
        uncounted = deepcopy(payload)
        uncounted["input_coverage"] = _input_coverage()
        before_the_field = deepcopy(payload)
        before_the_field["input_coverage"] = _input_coverage_before_issue_157()

        document = RetroInput.model_validate(
            {**payload, "input_digest": retro_input_digest(payload)}
        )

        assert document.input_coverage is not None
        assert document.input_coverage.exhibit_truncated_filing_count == 2
        assert retro_input_digest(payload) != retro_input_digest(uncounted)
        assert retro_input_digest(uncounted) != retro_input_digest(before_the_field)

    def test_rejects_a_document_that_lost_a_coverage_key_it_was_signed_with(
        self,
    ) -> None:
        # Hashing only the keys the document carries must not become a way to
        # erase a signed field: deleting `exhibit_truncated_filing_count: 2`
        # leaves a document whose stored digest counted it.
        payload = _unsigned_payload()
        payload["input_coverage"] = {
            **_input_coverage(),
            "exhibit_truncated_filing_count": 2,
        }
        archived_digest = retro_input_digest(payload)
        tampered = deepcopy(payload)
        del tampered["input_coverage"]["exhibit_truncated_filing_count"]

        with pytest.raises(ValidationError, match="input_digest"):
            RetroInput.model_validate({**tampered, "input_digest": archived_digest})

    def test_a_surprises_filing_coverage_from_before_issue_157_keeps_its_digest(
        self,
    ) -> None:
        # The live break was not confined to `input_coverage`: Issue #157 put
        # the same field on every archived `FilingCoverage`, and its `false`
        # default is a measurement too. The 2026-08-12 dossier carried both
        # shapes of the loss -- a summary block without
        # `exhibit_truncated_filing_count` and eleven nested coverage blocks
        # without `exhibit_truncated` -- so either alone would have failed it.
        payload = _unsigned_payload()
        payload["surprises"]["items"][0]["input_filing_coverage"] = [
            _filing_coverage_before_issue_157()
        ]
        archived_digest = retro_input_digest(payload)

        document = RetroInput.model_validate(
            {**payload, "input_digest": archived_digest}
        )

        coverage = document.surprises.items[0].input_filing_coverage[0].coverage
        assert coverage.exhibit_truncated is False
        assert coverage.sections == []

    def test_a_counted_starved_export_changes_a_fresh_documents_digest(self) -> None:
        # The other half: the default is dropped only because 0 means "not
        # counted". A dossier that did count a starved filing must hash
        # differently from one that counted none.
        payload = _unsigned_payload()
        payload["input_coverage"] = {**_input_coverage(), "starved_filing_count": 1}
        uncounted = deepcopy(payload)
        uncounted["input_coverage"] = _input_coverage()

        document = RetroInput.model_validate(
            {**payload, "input_digest": retro_input_digest(payload)}
        )

        assert document.input_coverage is not None
        assert document.input_coverage.starved_filing_count == 1
        assert retro_input_digest(payload) != retro_input_digest(uncounted)

    def test_a_recorded_gate_cross_tab_changes_a_fresh_documents_digest(self) -> None:
        payload = _unsigned_payload()
        with_history = deepcopy(payload)
        with_history["failure_class_history"] = {
            "gate_window_sessions": 3,
            "gate_min_count": 5,
            "sessions": ["2027-02-01"],
            "counts": [
                {
                    "count_id": "failure_class_exogenous",
                    "failure_class": "exogenous",
                    "count": 2,
                    "session_count": 1,
                    "meets_l2_gate": False,
                }
            ],
        }

        assert retro_input_digest(with_history) != retro_input_digest(payload)
        assert (
            RetroInput.model_validate(
                {**with_history, "input_digest": retro_input_digest(with_history)}
            ).failure_class_history
            is not None
        )

    def test_a_measured_dispersion_does_change_a_fresh_documents_digest(self) -> None:
        # The other half of the contract: only the *absent* form is ignored.
        # A window that actually produced an interval must hash differently
        # from one that did not, or the digest would stop identifying inputs.
        payload = _unsigned_payload()
        with_spread = deepcopy(payload)
        with_spread["aggregates"]["separation"][0]["stderr"] = 0.4

        assert retro_input_digest(with_spread) != retro_input_digest(payload)

    def test_a_dossier_carrying_the_new_aggregates_verifies_its_digest(self) -> None:
        payload = _unsigned_payload()
        payload["aggregates"]["separation_paired"] = [
            {
                "metric_id": "metric:separation_paired:5d",
                "horizon_days": 5,
                "value": 1.5,
                "sample_size": 4,
                "is_preliminary": True,
                "stderr": 0.5,
                "ci_low": 0.52,
                "ci_high": 2.48,
                "excluded_day_count": 1,
            }
        ]
        payload["aggregates"]["tracked_performance"] = [
            {
                "metric_id": "metric:tracked_performance:proceed",
                "recommendation": "proceed",
                "closed_count": 2,
                "open_count": 1,
                "win_rate": 0.5,
                "profit_factor": 2.0,
                "expectancy_pct": 2.5,
                "avg_r_multiple": 1.0,
                "avg_holding_days": 4.0,
                "exit_reason_counts": [{"reason": "stop", "count": 2}],
            }
        ]

        document = RetroInput.model_validate(
            {**payload, "input_digest": retro_input_digest(payload)}
        )

        assert document.aggregates.separation_paired is not None
        assert document.aggregates.separation_paired[0].excluded_day_count == 1
        assert document.aggregates.tracked_performance is not None
        assert document.aggregates.tracked_performance[0].recommendation == "proceed"

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
