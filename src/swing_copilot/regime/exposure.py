"""Pure Exposure Ceiling decisions derived from a regime snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from swing_copilot.regime.distribution import DataQuality, DistributionLevel
from swing_copilot.regime.gate import GateVerdict, RegimeSnapshot


class ExposureVerdict(StrEnum):
    """Maximum permitted stance for prospective positions."""

    NEW_ENTRY_ALLOWED = "NEW_ENTRY_ALLOWED"
    REDUCE_ONLY = "REDUCE_ONLY"
    CASH_PRIORITY = "CASH_PRIORITY"


@dataclass(frozen=True, slots=True)
class ExposureDecision:
    """Code-owned exposure decision and its auditable input rationale."""

    verdict: ExposureVerdict
    gate: GateVerdict
    dd_level: DistributionLevel
    data_quality: DataQuality
    is_conservatively_downgraded: bool
    reduce_only_risk_multiplier: float = 0.5


def determine_exposure(
    snapshot: RegimeSnapshot, *, reduce_only_risk_multiplier: float = 0.5
) -> ExposureDecision:
    """Map a snapshot to the strictest safe new-entry policy.

    Unknown inputs cannot loosen a policy. When only one input is unknown,
    the known input supplies the baseline and the decision moves one level
    stricter; when both are unknown the ceiling is cash priority.
    """
    gate, dd_level = snapshot.gate.verdict, snapshot.dd_level
    has_unknown_gate = gate is GateVerdict.UNKNOWN
    has_unknown_dd = dd_level is DistributionLevel.UNKNOWN
    if has_unknown_gate and has_unknown_dd:
        verdict = ExposureVerdict.CASH_PRIORITY
    else:
        baseline = _base_exposure(gate, dd_level)
        verdict = (
            _stricter(baseline) if has_unknown_gate or has_unknown_dd else baseline
        )
    return ExposureDecision(
        verdict=verdict,
        gate=gate,
        dd_level=dd_level,
        data_quality=snapshot.data_quality,
        is_conservatively_downgraded=has_unknown_gate or has_unknown_dd,
        reduce_only_risk_multiplier=reduce_only_risk_multiplier,
    )


def _base_exposure(gate: GateVerdict, dd_level: DistributionLevel) -> ExposureVerdict:
    if gate is GateVerdict.BEAR or dd_level is DistributionLevel.SEVERE:
        return ExposureVerdict.CASH_PRIORITY
    if gate is GateVerdict.NEUTRAL or dd_level is DistributionLevel.HIGH:
        return ExposureVerdict.REDUCE_ONLY
    return ExposureVerdict.NEW_ENTRY_ALLOWED


def _stricter(verdict: ExposureVerdict) -> ExposureVerdict:
    return {
        ExposureVerdict.NEW_ENTRY_ALLOWED: ExposureVerdict.REDUCE_ONLY,
        ExposureVerdict.REDUCE_ONLY: ExposureVerdict.CASH_PRIORITY,
        ExposureVerdict.CASH_PRIORITY: ExposureVerdict.CASH_PRIORITY,
    }[verdict]
