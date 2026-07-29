"""P8-31: the `retro-input-v1` strict schema (E31.2).

The retrospective's dossier is the second machine-checked boundary in this
repository, so it is held to the same rules as `analysis-input-v2`: unknown
fields fail loudly, the version is a constant rather than a free string, and
the document's digest binds it to its own contents.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.schemas import canonical_json_digest
from swing_copilot.retro.schemas import (
    RETRO_INPUT_SCHEMA_VERSION,
    RetroInput,
)

if TYPE_CHECKING:
    from collections.abc import Callable

RUN_ID = "11111111-1111-1111-1111-111111111111"


def _unsigned_payload() -> dict[str, Any]:
    """A complete document body, digest excluded."""
    return {
        "schema_version": RETRO_INPUT_SCHEMA_VERSION,
        "as_of": "2027-03-29",
        "generated_at": "2027-03-29T00:00:00Z",
        "window_start": "2026-12-29",
        "evaluation": {
            "horizon_5d_weight": 0.6,
            "horizon_20d_weight": 0.4,
            "neutral_threshold_pct": 0.5,
            "severe_threshold_pct": 2.0,
            "preliminary_sample_threshold": 20,
            "lookback_window_days": 90,
            "proceed_severe_miss_watch_rate": 0.15,
        },
        "aggregates": {
            "separation": [
                {
                    "metric_id": "metric:separation:5d",
                    "horizon_days": 5,
                    "value": -0.9,
                    "sample_size": 3,
                    "is_preliminary": True,
                }
            ],
            "proceed_severe_miss_rate": [
                {
                    "metric_id": "metric:proceed_severe_miss_rate:5d",
                    "horizon_days": 5,
                    "value": 0.5,
                    "baseline_value": 0.33,
                    "is_flagged": True,
                    "sample_size": 2,
                    "is_preliminary": True,
                }
            ],
            "skip_hit_rate": [
                {
                    "metric_id": "metric:skip_hit_rate:composed",
                    "horizon_days": None,
                    "value": None,
                    "baseline_value": None,
                    "is_flagged": False,
                    "sample_size": 0,
                    "is_preliminary": True,
                }
            ],
        },
        "signal_performance": [
            {
                "signal_name": "rsi_pullback",
                "true_positive_count": 2,
                "false_positive_count": 1,
                "neutral_count": 0,
                "hit_rate": 0.6,
                "n": 3,
                "is_preliminary": True,
            }
        ],
        "human_alignment": [
            {
                "cell_id": "metric:human_alignment:followed:proceed:5d",
                "decision": "followed",
                "recommendation": "proceed",
                "horizon_days": 5,
                "count": 2,
                "mean_forward_return_pct": 1.25,
                "hit_count": 1,
                "severe_miss_count": 1,
            }
        ],
        "source_contribution": [
            {
                "contribution_id": "metric:source_contribution:news:finnhub",
                "source_type": "news",
                "provider": "finnhub",
                "citation_count": 3,
                "hit_citation_count": 2,
                "miss_citation_count": 1,
                "neutral_citation_count": 0,
                "hit_citation_ratio": 0.6666666666666666,
            }
        ],
        "surprises": {
            "max_surprises": 5,
            "dropped_count": 1,
            "items": [
                {
                    "surprise_id": f"surprise:{RUN_ID}:AAPL",
                    "run_id": RUN_ID,
                    "symbol": "AAPL",
                    "run_as_of": "2027-03-01",
                    "strategy_key": "default",
                    "recommendation": "proceed",
                    "no_trade": False,
                    "reasons": [
                        {"text": "受注は堅調に見える", "source_ids": ["finnhub:1"]}
                    ],
                    "cited_source_ids": ["finnhub:1"],
                    "outcomes": [
                        {
                            "horizon_days": 5,
                            "maturity_as_of": "2027-03-08",
                            "forward_return_pct": -8.0,
                            "classification": "MISS_SEVERE",
                        }
                    ],
                    "max_adverse_return_pct": -9.5,
                    "freshness": {
                        "news": [
                            {
                                "source_id": "finnhub:9",
                                "published_at": "2027-03-05T00:00:00Z",
                                "headline": "見出し",
                                "summary": "本文",
                                "url": "https://example.test/9",
                                "provider": "finnhub",
                            }
                        ],
                        "filings": [],
                        "fetch_failed": False,
                    },
                }
            ],
        },
        "config_snapshot": {
            "sections": {"retro": {"max_surprises": 5, "approval_mode": "auto"}},
            "config_hash": "0" * 64,
        },
        "proposals_ledger": {
            "path": "docs/retro/proposals.md",
            "exists": False,
            "rejected_proposal_ids": [],
        },
        "notes": ["AAPL: 鮮度開示を取得できなかったため空欄"],
    }


def _payload() -> dict[str, Any]:
    unsigned = _unsigned_payload()
    return {
        **unsigned,
        "input_digest": canonical_json_digest(unsigned, excluded_field="input_digest"),
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

    def test_accepts_an_empty_window_with_no_metrics_or_surprises(self) -> None:
        payload = _unsigned_payload()
        payload["aggregates"] = {
            "separation": [],
            "proceed_severe_miss_rate": [],
            "skip_hit_rate": [],
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
