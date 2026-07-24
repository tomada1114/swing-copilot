"""Common NaN/Inf write-boundary guard for `storage/`'s JSON columns (P1-04).

Every `json.dumps(...)` call site under `storage/` must go through
`dumps_safe` instead: a finite-value check runs before serialization, and
`json.dumps(..., allow_nan=False)` is the second line of defense so a
non-finite float can never reach a DuckDB `JSON` column as a spec-violating
`NaN`/`Infinity` literal (Issue #13, REQ-003/005/020).
"""

from __future__ import annotations

import json
import math


def dumps_safe(value: object) -> str:
    """Serialize `value` to JSON, rejecting any embedded NaN/Inf/-Inf.

    Args:
        value: A JSON-serializable structure (dict/list/tuple of primitives).

    Returns:
        The JSON string, equivalent to `json.dumps(value)` for finite input.

    Raises:
        ValueError: `value` contains a non-finite float anywhere in its
            structure. The message includes the offending key/index path
            (e.g. `"a.b[0].c"`), and is raised before any serialization or
            caller-side write happens.
    """
    _check_finite(value)
    return json.dumps(value, allow_nan=False)


def _check_finite(value: object) -> None:
    """Iteratively walk `value` for non-finite floats (REQ-004: no recursion).

    An explicit stack (rather than a recursive helper call) keeps stack
    depth constant regardless of input nesting depth, so a deeply nested
    dict/list cannot trigger `RecursionError`.
    """
    stack: list[tuple[object, str]] = [(value, "")]
    while stack:
        current, path = stack.pop()
        if isinstance(current, float) and not math.isfinite(current):
            msg = f"non-finite value at {path or '<root>'}: {current}"
            raise ValueError(msg)
        if isinstance(current, dict):
            for key, item in current.items():
                child_path = f"{path}.{key}" if path else str(key)
                stack.append((item, child_path))
        elif isinstance(current, (list, tuple)):
            for index, item in enumerate(current):
                stack.append((item, f"{path}[{index}]"))
