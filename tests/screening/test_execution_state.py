"""P5-23 execution-state boundaries and safe bucket ordering."""

from __future__ import annotations

from swing_copilot.screening.execution import execution_bucket
from swing_copilot.screening.pipeline import _execution_state, _state_sort_key


def test_execution_state_boundaries_are_inclusive_on_the_upper_bucket():
    assert _execution_state(-3.01) == "DAMAGED"
    assert _execution_state(-3.0) == "PULLBACK_ZONE"
    assert _execution_state(0.0) == "FAIR"
    assert _execution_state(2.0) == "EXTENDED"
    assert _execution_state(4.0) == "OVEREXTENDED"


def test_unknown_is_safe_side_pass_bucket():
    assert _execution_state(None) == "UNKNOWN"
    assert execution_bucket("UNKNOWN") == "見送り"


def test_cash_priority_overrides_execution_state_for_display_bucket():
    assert execution_bucket("FAIR", risk_reasons=("REGIME_CASH_PRIORITY",)) == (
        "見送り（地合い）"
    )


def test_state_cap_places_high_scoring_pass_candidate_after_other_buckets():
    rows = [
        ("OVER", 0.95, "OVEREXTENDED"),
        ("WATCH", 0.10, "EXTENDED"),
        ("READY", 0.01, "FAIR"),
    ]

    ordered = sorted(rows, key=lambda row: _state_sort_key(row[2], row[1], row[0]))

    assert [row[0] for row in ordered] == ["READY", "WATCH", "OVER"]
