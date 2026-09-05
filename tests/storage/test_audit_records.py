"""Direct contracts for the screening-audit persistence helpers."""

from __future__ import annotations

from swing_copilot.screening.base import TruncatedCandidate
from swing_copilot.storage.audit_records import select_persisted_truncations


def _truncated(symbol: str, rank: int) -> TruncatedCandidate:
    return TruncatedCandidate(
        symbol=symbol,
        rank=rank,
        score=0.5,
        score_breakdown={},
        execution_state="READY",
        execution_distance=0.02,
    )


def test_select_persisted_truncations_applies_the_page_cap_at_the_module_boundary() -> (
    None
):
    truncations = tuple(_truncated(f"RANK{rank}", rank) for rank in (7, 4, 6, 5))

    retained = select_persisted_truncations(truncations, candidate_limit=1)

    assert [(item.symbol, item.rank) for item in retained] == [
        ("RANK4", 4),
        ("RANK5", 5),
        ("RANK6", 6),
    ]
