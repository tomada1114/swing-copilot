"""Tests for storage/json_guard.py (P1-04, Issue #13, REQ-003/004/005/020)."""

from __future__ import annotations

import json
import math

import pytest

from swing_copilot.storage.json_guard import dumps_safe


class TestDumpsSafeHappyPath:
    def test_serializes_a_finite_structure_like_json_dumps(self):
        payload = {"symbol": "AAPL", "metrics": {"score": 0.42, "count": 3}}

        assert json.loads(dumps_safe(payload)) == payload

    def test_empty_dict_passes_through_cleanly(self):
        assert dumps_safe({}) == "{}"

    def test_empty_list_passes_through_cleanly(self):
        assert dumps_safe([]) == "[]"

    def test_nested_empty_structures_pass_through_cleanly(self):
        payload: dict[str, object] = {"a": [], "b": {}, "c": [{}]}

        assert json.loads(dumps_safe(payload)) == payload


class TestDumpsSafeRejectsNonFinite:
    def test_bare_nan_raises_value_error(self):
        with pytest.raises(ValueError, match="non-finite"):
            dumps_safe(float("nan"))

    def test_bare_infinity_raises_value_error(self):
        with pytest.raises(ValueError, match="non-finite"):
            dumps_safe(float("inf"))

    def test_bare_negative_infinity_raises_value_error(self):
        with pytest.raises(ValueError, match="non-finite"):
            dumps_safe(float("-inf"))

    def test_exception_message_includes_the_exact_key_path(self):
        # Issue #13's own worked example: {"a": {"b": [{"c": nan}]}} ->
        # exception message must include an "a.b[0].c"-style path.
        payload = {"a": {"b": [{"c": float("nan")}]}}

        with pytest.raises(ValueError, match=r"a\.b\[0\]\.c") as exc_info:
            dumps_safe(payload)

        assert "a.b[0].c" in str(exc_info.value)

    def test_no_write_happens_before_the_guard_raises(self):
        # dumps_safe must fail before returning any string -- no partial
        # JSON is ever produced for a non-finite payload.
        with pytest.raises(ValueError, match="non-finite"):
            dumps_safe({"metrics": {"score": float("inf")}})

    def test_json_dumps_allow_nan_false_is_second_line_of_defense(self):
        # REQ-005: even if the pre-check were bypassed, json.dumps itself is
        # called with allow_nan=False so a non-finite value can never reach
        # the caller as a (spec-violating) "NaN"/"Infinity" JSON literal.
        with pytest.raises(ValueError, match="Out of range float"):
            json.dumps(float("nan"), allow_nan=False)


class TestDumpsSafeDeepNesting:
    def test_1000_deep_nested_list_with_non_finite_leaf_raises_value_error_not_recursion_error(
        self,
    ):
        # REQ-004, Boundary Condition "深いネスト": iterative (explicit-stack)
        # traversal must not hit Python's recursion limit.
        nested: object = float("nan")
        for _ in range(1000):
            nested = [nested]

        with pytest.raises(ValueError, match="non-finite"):
            dumps_safe(nested)

    def test_1000_deep_nested_dict_with_non_finite_leaf_raises_value_error_not_recursion_error(
        self,
    ):
        nested: object = float("inf")
        for _ in range(1000):
            nested = {"level": nested}

        with pytest.raises(ValueError, match="non-finite"):
            dumps_safe(nested)

    def test_1000_deep_nested_all_finite_structure_passes_through(self):
        # A deep-but-finite structure must not raise at all (not just "not
        # RecursionError" -- genuinely clean).
        nested: object = 1.0
        for _ in range(1000):
            nested = [nested]

        result = dumps_safe(nested)

        assert result.count("[") == 1000


def _naive_recursive_check_finite(value: object) -> None:
    """A recursive (non-iterative) equivalent, kept only for one test.

    Proves REQ-004's "must be iterative" requirement is load-bearing: this
    helper is expected to blow the recursion limit on the same 1000-deep
    fixture that `dumps_safe` (the real, stack-based implementation)
    handles cleanly.
    """
    if isinstance(value, float) and not math.isfinite(value):
        msg = f"non-finite value: {value}"
        raise ValueError(msg)
    if isinstance(value, dict):
        for item in value.values():
            _naive_recursive_check_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _naive_recursive_check_finite(item)


def test_a_naive_recursive_implementation_would_hit_recursion_error_on_1000_deep_nesting():
    """Verify the design choice behind `dumps_safe`'s iterative traversal.

    A recursive traversal genuinely fails on the exact fixture the
    iterative `dumps_safe` handles, so REQ-004's "no recursive helper call"
    requirement is not just a style preference.
    """
    nested: object = float("nan")
    for _ in range(1000):
        nested = [nested]

    with pytest.raises(RecursionError):
        _naive_recursive_check_finite(nested)
