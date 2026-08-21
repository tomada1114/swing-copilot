"""Symbol-level trade-plan checks for the public analysis path.

The public product does not know a reader's account equity or holdings. This
module therefore evaluates only facts intrinsic to one symbol: the reference
close, planned limit and stop, stop distance (1R), earnings timing, and the
market-wide exposure label.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from swing_copilot.backtest.entries import entry_limit_price
from swing_copilot.risk.earnings import business_days_since, evaluate_earnings_proximity

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

    from swing_copilot.config import Settings
    from swing_copilot.data.earnings import EarningsLookup
    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.risk.circuit_breaker import CircuitBreakerResult
    from swing_copilot.screening.base import Candidate

_MISSING_DATA_REASON = "missing candidate price/ATR data"
_INVALID_STOP_REASON = (
    "ATR-based stop distance is not usable (ATR is zero or unavailable)"
)

RISK_WARNING_WIDE_STOP = "WIDE_STOP"
REGIME_CASH_PRIORITY_REASON = "REGIME_CASH_PRIORITY"
EARNINGS_PROXIMITY_BLOCK_REASON = "EARNINGS_PROXIMITY_BLOCK"
EARNINGS_PROXIMITY_WARN_WARNING = "EARNINGS_PROXIMITY_WARN"
EARNINGS_DATE_UNKNOWN_WARNING = "EARNINGS_DATE_UNKNOWN"
EARNINGS_RECENTLY_REPORTED_WARNING = "EARNINGS_RECENTLY_REPORTED"
CIRCUIT_BREAKER_REASON_PREFIX = "CIRCUIT_BREAKER_"

# A stored earnings-calendar row within this many business days before `as_of`
# is recent enough to flag. The configurable windows classify upcoming events.
_RECENTLY_REPORTED_BUSINESS_DAYS = 3

BindingConstraint = Literal["regime", "earnings", "not_calculable"]


@dataclass(frozen=True, slots=True)
class EarningsGuardInput:
    """Pre-fetched lookup data supplied to the deterministic risk core."""

    is_enabled: bool
    lookups_by_symbol: Mapping[str, EarningsLookup]


@dataclass(frozen=True, slots=True)
class RiskRunContext:
    """Run-wide controls supplied to the deterministic checker.

    `circuit_breaker` remains only as a temporary simulator compatibility seam;
    the production daily path never supplies it. Issue #349 owns removal from
    the backtest policy.
    """

    earnings_guard: EarningsGuardInput | None = None
    circuit_breaker: CircuitBreakerResult | None = None


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """One candidate's account-independent trade-plan evaluation."""

    symbol: str
    status: str  # "approved" | "rejected" | "not_calculable"
    # Reference close from the run day. Tracking deliberately uses this value
    # as its virtual-ledger entry until the entry basis is revisited in #327.
    entry_price: float | None
    # Maximum planned fill price. A planned limit is not proof of a fill.
    limit_price: float | None
    stop_price: float | None
    atr14: float | None
    # One unit of initial risk as a fraction of the planned limit price.
    stop_distance_pct: float | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    # Only a blocking reason is recorded. Approved rows have no constraint.
    binding_constraint: BindingConstraint | None = None


def _binding_constraint_after(
    assessment: RiskAssessment, candidate_constraint: BindingConstraint
) -> BindingConstraint:
    """Preserve the first block when a later guard also fires."""
    if assessment.status == "rejected" and assessment.binding_constraint is not None:
        return assessment.binding_constraint
    return candidate_constraint


class RiskChecker:
    """Evaluate symbol-level prices, earnings timing, and market state."""

    def __init__(
        self,
        settings: Settings,
        run_context: RiskRunContext | None = None,
    ) -> None:
        """Create the checker from validated settings and prefetched controls."""
        self._risk_config = settings.risk
        self._stop_atr_multiple = settings.backtest.exit_atr_multiple
        self._entry_limit_atr_multiple = settings.backtest.entry_limit_atr_multiple
        context = run_context or RiskRunContext()
        self._earnings_guard = context.earnings_guard or EarningsGuardInput(False, {})
        self._circuit_breaker = context.circuit_breaker

    def check(
        self,
        candidates: list[Candidate],
        exposure: ExposureDecision | None = None,
    ) -> list[RiskAssessment]:
        """Assess candidates without requiring account or holding state."""
        assessments: list[RiskAssessment] = []
        for candidate in candidates:
            assessment = self._assess(candidate, exposure)
            assessment = self._apply_earnings_guard(
                assessment,
                candidate.as_of,
                self._earnings_guard.lookups_by_symbol.get(candidate.symbol),
                self._earnings_guard.is_enabled,
            )
            assessments.append(self._apply_circuit_breaker(assessment))
        return assessments

    def _assess(
        self,
        candidate: Candidate,
        exposure: ExposureDecision | None,
    ) -> RiskAssessment:
        entry_price = candidate.metrics.get("close")
        atr14 = candidate.metrics.get("atr14")
        is_cash_priority = (
            exposure is not None and exposure.verdict.value == "CASH_PRIORITY"
        )
        if entry_price is None or atr14 is None:
            return RiskAssessment(
                symbol=candidate.symbol,
                status="rejected" if is_cash_priority else "not_calculable",
                entry_price=entry_price,
                limit_price=None,
                stop_price=None,
                atr14=atr14,
                stop_distance_pct=None,
                reasons=(
                    (REGIME_CASH_PRIORITY_REASON,)
                    if is_cash_priority
                    else (_MISSING_DATA_REASON,)
                ),
                binding_constraint=("regime" if is_cash_priority else "not_calculable"),
            )

        limit_price = entry_limit_price(
            entry_price, atr14, self._entry_limit_atr_multiple
        )
        stop_price = entry_price - self._stop_atr_multiple * atr14
        if (
            not all(math.isfinite(value) for value in (limit_price, stop_price, atr14))
            or limit_price <= 0.0
            or stop_price >= limit_price
        ):
            return RiskAssessment(
                symbol=candidate.symbol,
                status="rejected" if is_cash_priority else "not_calculable",
                entry_price=entry_price,
                limit_price=limit_price,
                stop_price=stop_price,
                atr14=atr14,
                stop_distance_pct=None,
                reasons=(
                    (REGIME_CASH_PRIORITY_REASON,)
                    if is_cash_priority
                    else (_INVALID_STOP_REASON,)
                ),
                binding_constraint=("regime" if is_cash_priority else "not_calculable"),
            )

        stop_distance_pct = (limit_price - stop_price) / limit_price
        warnings = (
            (RISK_WARNING_WIDE_STOP,)
            if stop_distance_pct * 100 > self._risk_config.wide_stop_threshold_pct
            else ()
        )
        if is_cash_priority:
            return RiskAssessment(
                symbol=candidate.symbol,
                status="rejected",
                entry_price=entry_price,
                limit_price=limit_price,
                stop_price=stop_price,
                atr14=atr14,
                stop_distance_pct=stop_distance_pct,
                reasons=(REGIME_CASH_PRIORITY_REASON,),
                warnings=warnings,
                binding_constraint="regime",
            )
        return RiskAssessment(
            symbol=candidate.symbol,
            status="approved",
            entry_price=entry_price,
            limit_price=limit_price,
            stop_price=stop_price,
            atr14=atr14,
            stop_distance_pct=stop_distance_pct,
            reasons=(),
            warnings=warnings,
        )

    def _apply_circuit_breaker(self, assessment: RiskAssessment) -> RiskAssessment:
        result = self._circuit_breaker
        if result is None or result.state.value == "TRADING_ALLOWED":
            return assessment
        return replace(
            assessment,
            status="rejected",
            reasons=(
                *assessment.reasons,
                f"{CIRCUIT_BREAKER_REASON_PREFIX}{result.state.value}",
            ),
            binding_constraint=_binding_constraint_after(assessment, "regime"),
        )

    def _apply_earnings_guard(
        self,
        assessment: RiskAssessment,
        as_of: date,
        lookup: EarningsLookup | None,
        is_enabled: bool,
    ) -> RiskAssessment:
        if not is_enabled:
            return assessment
        event = lookup.event if lookup is not None else None
        proximity = evaluate_earnings_proximity(
            as_of,
            event.earnings_date if event is not None else None,
            block_business_days=self._risk_config.earnings_block_business_days,
            warn_business_days=self._risk_config.earnings_warn_business_days,
        )
        if proximity.status == "block":
            assessment = replace(
                assessment,
                status="rejected",
                reasons=(*assessment.reasons, EARNINGS_PROXIMITY_BLOCK_REASON),
                binding_constraint=_binding_constraint_after(assessment, "earnings"),
            )
        elif proximity.status == "warn" and event is not None:
            warning = (
                f"{EARNINGS_PROXIMITY_WARN_WARNING}: {proximity.business_days} "
                f"business days until {event.earnings_date.isoformat()}"
            )
            assessment = replace(assessment, warnings=(*assessment.warnings, warning))
        elif proximity.status == "unknown" and (
            event is not None
            or (lookup is not None and lookup.status == "fetch_failed")
        ):
            assessment = replace(
                assessment,
                warnings=(*assessment.warnings, EARNINGS_DATE_UNKNOWN_WARNING),
            )
        return self._apply_recently_reported_warning(assessment, as_of, lookup)

    @staticmethod
    def _apply_recently_reported_warning(
        assessment: RiskAssessment,
        as_of: date,
        lookup: EarningsLookup | None,
    ) -> RiskAssessment:
        recent_event = lookup.recent_event if lookup is not None else None
        if recent_event is None or recent_event.earnings_date >= as_of:
            return assessment
        days_since = business_days_since(as_of, recent_event.earnings_date)
        if days_since > _RECENTLY_REPORTED_BUSINESS_DAYS:
            return assessment
        warning = (
            f"{EARNINGS_RECENTLY_REPORTED_WARNING}: {days_since} "
            f"business days since {recent_event.earnings_date.isoformat()}"
        )
        return replace(assessment, warnings=(*assessment.warnings, warning))
