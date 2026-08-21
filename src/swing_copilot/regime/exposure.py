"""Pure Exposure Ceiling decisions derived from a regime snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from swing_copilot.regime.distribution import DataQuality, DistributionLevel
from swing_copilot.regime.ftd import FtdState
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
    # Kept as a nullable-schema compatibility field until the account-rule
    # cleanup in Issue #342 removes it. REDUCE_ONLY is a label, so the live
    # decision always carries 1.0 and never halves position risk.
    reduce_only_risk_multiplier: float = 1.0
    spy_sma200: float | None = None
    vix_close: float | None = None
    spy_ftd_state: str | None = None
    is_ftd_active: bool = False


def determine_exposure(snapshot: RegimeSnapshot) -> ExposureDecision:
    """Map a snapshot to the strictest safe new-entry policy.

    Unknown inputs cannot loosen a policy. When only one input is unknown,
    the known input supplies the baseline and the decision moves one level
    stricter; when both are unknown the ceiling is cash priority.
    """
    gate, dd_level = snapshot.gate.verdict, snapshot.dd_level
    has_unknown_gate = gate is GateVerdict.UNKNOWN
    has_unknown_dd = dd_level is DistributionLevel.UNKNOWN
    spy_ftd = snapshot.ftd.spy if snapshot.ftd is not None else None
    is_ftd_active = (
        gate in (GateVerdict.BEAR, GateVerdict.NEUTRAL)
        and spy_ftd is not None
        and spy_ftd.state is FtdState.FTD_CONFIRMED
    )
    has_unknown_ftd = (
        spy_ftd is not None and spy_ftd.data_quality is DataQuality.INSUFFICIENT
    )
    if has_unknown_gate and has_unknown_dd:
        verdict = ExposureVerdict.CASH_PRIORITY
    else:
        baseline = _base_exposure(
            gate,
            dd_level,
            is_ftd_active=is_ftd_active,
            is_panic=snapshot.gate.is_panic,
        )
        verdict = (
            _stricter(baseline) if has_unknown_gate or has_unknown_dd else baseline
        )
    return ExposureDecision(
        verdict=verdict,
        gate=gate,
        dd_level=dd_level,
        data_quality=snapshot.data_quality,
        is_conservatively_downgraded=(
            has_unknown_gate
            or has_unknown_dd
            or (has_unknown_ftd and gate is GateVerdict.BEAR)
        ),
        reduce_only_risk_multiplier=1.0,
        spy_sma200=snapshot.gate.spy_sma200,
        vix_close=snapshot.gate.vix_close,
        spy_ftd_state=spy_ftd.state.value if spy_ftd is not None else None,
        is_ftd_active=is_ftd_active,
    )


def _base_exposure(
    gate: GateVerdict,
    dd_level: DistributionLevel,
    *,
    is_ftd_active: bool,
    is_panic: bool,
) -> ExposureVerdict:
    if is_panic:
        return ExposureVerdict.CASH_PRIORITY
    if gate is GateVerdict.BEAR:
        return (
            ExposureVerdict.REDUCE_ONLY
            if is_ftd_active
            else ExposureVerdict.CASH_PRIORITY
        )
    if gate is GateVerdict.NEUTRAL or dd_level is DistributionLevel.SEVERE:
        return ExposureVerdict.REDUCE_ONLY
    return ExposureVerdict.NEW_ENTRY_ALLOWED


def _stricter(verdict: ExposureVerdict) -> ExposureVerdict:
    return {
        ExposureVerdict.NEW_ENTRY_ALLOWED: ExposureVerdict.REDUCE_ONLY,
        ExposureVerdict.REDUCE_ONLY: ExposureVerdict.CASH_PRIORITY,
        ExposureVerdict.CASH_PRIORITY: ExposureVerdict.CASH_PRIORITY,
    }[verdict]
