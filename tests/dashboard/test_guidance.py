"""The reading hints must stay true to the code that produces the values."""

from __future__ import annotations

import pytest

from swing_copilot.dashboard import guidance
from swing_copilot.retro.evaluate import (
    HIT,
    MISS_MILD,
    MISS_SEVERE,
    NEUTRAL,
    classify_verdict_outcome,
)

_NEUTRAL_PCT = 1.0
_SEVERE_PCT = 2.0


def classify(recommendation: str, forward_return_pct: float) -> str:
    return classify_verdict_outcome(
        recommendation,
        forward_return_pct,
        neutral_threshold_pct=_NEUTRAL_PCT,
        severe_threshold_pct=_SEVERE_PCT,
    )


class TestOutcomeHintMatchesTheClassifier:
    """Pin the claims the caption makes against `retro/evaluate.py` itself.

    The caption is the only place a reader learns that HIT means "the call
    was right" on *both* sides. If the classifier's direction ever changed,
    the caption would become actively misleading, so it is asserted rather
    than trusted.
    """

    def test_a_proceed_that_avoided_a_decline_is_a_hit(self) -> None:
        assert classify("proceed", 0.5) == HIT

    def test_a_skip_whose_symbol_declined_is_also_a_hit(self) -> None:
        assert classify("skip", -3.0) == HIT

    def test_a_skip_whose_symbol_rose_is_an_opportunity_cost_miss(self) -> None:
        assert classify("skip", 1.5) == MISS_MILD
        assert classify("skip", 2.0) == MISS_SEVERE

    def test_a_proceed_that_fell_is_a_miss(self) -> None:
        assert classify("proceed", -1.5) == MISS_MILD
        assert classify("proceed", -2.0) == MISS_SEVERE

    def test_only_skip_can_be_neutral(self) -> None:
        assert classify("skip", 0.5) == NEUTRAL
        assert classify("proceed", 0.0) == HIT

    @pytest.mark.parametrize(
        ("recommendation", "expected"),
        [
            pytest.param("proceed", MISS_MILD, id="proceed-noise-edge-is-a-miss"),
            pytest.param("skip", HIT, id="skip-noise-edge-is-a-hit"),
        ],
    )
    def test_the_noise_band_edge_flips_with_the_verdict(
        self, recommendation: str, expected: str
    ) -> None:
        # The caption states this reversal explicitly; keep it honest.
        assert classify(recommendation, -_NEUTRAL_PCT) == expected


class TestHintContent:
    def test_the_outcome_hint_names_every_classification(self) -> None:
        text = guidance.OUTCOME.summary + " ".join(guidance.OUTCOME.details)
        for label in (HIT, MISS_MILD, MISS_SEVERE, NEUTRAL):
            assert label in text

    def test_thresholds_are_named_by_config_key_not_hardcoded(self) -> None:
        # The dashboard never reads settings.yaml, so a quoted number would
        # go stale silently.
        details = " ".join(guidance.OUTCOME.details)
        assert "postmortem.neutral_threshold_pct" in details
        assert "postmortem.severe_threshold_pct" in details

    def test_the_facet_hint_reuses_the_outcome_definitions(self) -> None:
        assert guidance.CLASSIFICATION_FACETS.details == guidance.OUTCOME.details

    def test_the_ledger_hint_says_skip_is_a_control_group(self) -> None:
        assert "対照群" in guidance.LEDGER.summary

    def test_the_pending_banner_explains_the_verdict_ingestion_lag(self) -> None:
        assert "retro collect" in guidance.ANALYSIS_PENDING
