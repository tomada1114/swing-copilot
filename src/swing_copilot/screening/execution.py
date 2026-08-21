"""Shared execution-state classification and report bucket mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Collection

EXECUTION_BUCKETS: Final = ("即検討可", "様子見", "見送り")
EXECUTION_CASH_PRIORITY_BUCKET: Final = "見送り（地合い）"
REGIME_CASH_PRIORITY_REASON: Final = "REGIME_CASH_PRIORITY"


def execution_bucket(state: str, *, risk_reasons: Collection[str] = ()) -> str:
    """Map a code-owned execution state to its user-facing bucket.

    Args:
        state: P5-23 execution state, such as ``FAIR`` or ``EXTENDED``.
        risk_reasons: Code-owned risk reasons that can override the state for
            display. ``REGIME_CASH_PRIORITY`` keeps the candidate visible but
            places it in the market-condition stand-down bucket.

    Returns:
        The stable Japanese bucket label used by ranking and reports.
    """
    if REGIME_CASH_PRIORITY_REASON in risk_reasons:
        return EXECUTION_CASH_PRIORITY_BUCKET
    if state in {"PULLBACK_ZONE", "FAIR"}:
        return EXECUTION_BUCKETS[0]
    if state == "EXTENDED":
        return EXECUTION_BUCKETS[1]
    return EXECUTION_BUCKETS[2]
