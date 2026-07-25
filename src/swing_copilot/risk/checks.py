"""Position sizing, sector concentration, and correlation checks (FR-06).

The stop-distance multiple (`entry_price - exit_atr_multiple * ATR14`) reuses
`settings.backtest.exit_atr_multiple` rather than a second, redundant
risk-specific multiplier — `settings.yaml`'s `risk.*` section has no such
key, and reusing the strategy's own stop rule keeps the risk preview
consistent with the backtest per `docs/04_detailed_design.md` 2.1 #5 ("reuse
the same logic").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pandas as pd

from swing_copilot.risk.position_sizing import PositionSizeResult, calc_position_size

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.config import Settings
    from swing_copilot.models import Position
    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.screening.base import Candidate
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.universe import UniverseMember

_MISSING_DATA_REASON = "missing candidate price/ATR data"
_MISSING_EQUITY_REASON = "account_equity is not set"
_INVALID_STOP_REASON = (
    "ATR-based stop distance is not usable (ATR is zero or unavailable)"
)

# P1-03 (REQ-020): below this risk budget (account_equity * max_trade_risk_pct,
# in USD), commissions/slippage dominate the trade regardless of the share
# count, so the risk-% calculation itself stops being meaningful. The issue
# text gives no exact figure ("極小リスク額") for this half of REQ-020; $1 is
# a deliberately conservative judgment call, documented here and pinned by a
# dedicated test (要検証).
_MIN_MEANINGFUL_RISK_BUDGET_USD = 1.0

SIZING_WARNING_WIDE_STOP = "WIDE_STOP"
SIZING_WARNING_SMALL_ACCOUNT_FRICTION = "SMALL_ACCOUNT_FRICTION"
SIZING_WARNING_REGIME_REDUCE_ONLY = "REGIME_REDUCE_ONLY_RISK_HALVED"
REGIME_CASH_PRIORITY_REASON = "REGIME_CASH_PRIORITY"
PORTFOLIO_HEAT_EXCEEDED_REASON = "PORTFOLIO_HEAT_EXCEEDED"
PORTFOLIO_HEAT_NOT_CALCULABLE_REASON = "PORTFOLIO_HEAT_NOT_CALCULABLE"

# REQ-004: the constraint that determined the final share count.
BindingConstraint = str  # "trade_risk" | "position_cap" | "sector" | "correlation" | "regime" | "portfolio_heat" | "not_calculable"


@dataclass(frozen=True, slots=True)
class PortfolioHeatResult:
    """Account-level stop risk for open holdings and approved candidates."""

    status: str  # "calculated" | "not_calculable"
    heat_pct: float | None
    missing_stop_symbols: tuple[str, ...] = ()
    reason: str | None = None


def calculate_portfolio_heat(
    positions: list[Position], account_equity: float | None
) -> PortfolioHeatResult:
    """Calculate total open stop risk as a percentage of account equity.

    Args:
        positions: Holdings or approved candidates represented as positions.
        account_equity: Positive account equity in USD.

    Returns:
        A calculated percentage, or an explicit non-calculable result. Missing
        stops are never treated as zero risk.
    """
    missing_stops = tuple(
        sorted(position.symbol for position in positions if position.stop_price is None)
    )
    if missing_stops:
        return PortfolioHeatResult(
            "not_calculable",
            None,
            missing_stops,
            "one or more open positions have no recorded stop",
        )
    if account_equity is None or account_equity <= 0:
        return PortfolioHeatResult(
            "not_calculable", None, reason="account_equity is not set or positive"
        )
    total_risk = sum(
        (position.entry_price - position.stop_price) * position.shares
        for position in positions
        if position.stop_price is not None
    )
    return PortfolioHeatResult("calculated", total_risk / account_equity * 100)


@dataclass(frozen=True, slots=True)
class CorrelationWarning:
    """A warning (never a block) about correlation with a held position."""

    correlated_symbol: str
    correlation: float
    warning_type: str = "high_correlation"  # or "data_quality"


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """One candidate's risk evaluation."""

    symbol: str
    status: str  # "approved" | "rejected" | "not_calculable"
    max_shares: int | None
    entry_price: float | None
    stop_price: float | None
    reasons: tuple[str, ...]
    warnings: tuple[CorrelationWarning, ...] = ()
    # P1-03 sizing breakdown.
    shares_by_risk: int | None = None
    shares_by_position_cap: int | None = None
    binding_constraint: BindingConstraint = "not_calculable"
    sizing_warnings: tuple[str, ...] = ()
    max_trade_risk_pct: float | None = None
    portfolio_heat_pct: float | None = None


def _daily_returns(bars: pd.DataFrame, lookback_days: int) -> pd.Series | None:
    if bars.empty:
        return None
    closes = (
        bars.sort_values("date")
        .drop_duplicates(subset="date", keep="last")
        .set_index("date")["close"]
    )
    if len(closes) < lookback_days + 1:
        return None
    returns = closes.tail(lookback_days + 1).pct_change().dropna()
    return returns if len(returns) == lookback_days else None


class RiskChecker:
    """Position sizing, sector concentration, and correlation checks (FR-06)."""

    def __init__(
        self,
        settings: Settings,
        universe: tuple[UniverseMember, ...],
        market_store: MarketStore,
    ) -> None:
        """Create the checker.

        Args:
            settings: Loaded application settings.
            universe: Current universe, for the symbol -> GICS sector map.
            market_store: Store used for correlation lookups.
        """
        self._risk_config = settings.risk
        self._stop_atr_multiple = settings.backtest.exit_atr_multiple
        self._sector_by_symbol = {
            member.symbol: member.gics_sector for member in universe
        }
        self._market_store = market_store

    def check(
        self,
        candidates: list[Candidate],
        portfolio: list[Position],
        account_equity: float | None,
        exposure: ExposureDecision | None = None,
    ) -> list[RiskAssessment]:
        """Assess every candidate: sizing, sector concentration, correlation.

        Args:
            candidates: Ranked screening candidates.
            portfolio: Currently open positions (paper or live).
            account_equity: Total account equity in USD, or `None` if unset.
            exposure: Code-owned market exposure ceiling, if available.

        Returns:
            One `RiskAssessment` per candidate, same order as `candidates`.
        """
        base_heat = calculate_portfolio_heat(portfolio, account_equity)
        current_heat_pct = base_heat.heat_pct
        assessments: list[RiskAssessment] = []
        for candidate in candidates:
            assessment = self._assess(candidate, portfolio, account_equity, exposure)
            if base_heat.status == "not_calculable":
                if assessment.status == "approved":
                    assessment = replace(
                        assessment,
                        status="not_calculable",
                        reasons=(
                            *assessment.reasons,
                            PORTFOLIO_HEAT_NOT_CALCULABLE_REASON,
                        ),
                        binding_constraint="not_calculable",
                    )
            elif assessment.status == "approved":
                additional_heat_pct = self._assessment_heat_pct(
                    assessment, account_equity
                )
                if current_heat_pct is None or additional_heat_pct is None:
                    assessment = replace(
                        assessment,
                        status="not_calculable",
                        reasons=(
                            *assessment.reasons,
                            PORTFOLIO_HEAT_NOT_CALCULABLE_REASON,
                        ),
                        binding_constraint="not_calculable",
                    )
                    assessments.append(assessment)
                    continue
                proposed_heat_pct = current_heat_pct + additional_heat_pct
                if proposed_heat_pct > self._risk_config.max_portfolio_heat_pct:
                    assessment = replace(
                        assessment,
                        status="rejected",
                        reasons=(
                            *assessment.reasons,
                            PORTFOLIO_HEAT_EXCEEDED_REASON,
                        ),
                        binding_constraint="portfolio_heat",
                        portfolio_heat_pct=current_heat_pct,
                    )
                else:
                    current_heat_pct = proposed_heat_pct
                    assessment = replace(
                        assessment, portfolio_heat_pct=current_heat_pct
                    )
            else:
                assessment = replace(assessment, portfolio_heat_pct=current_heat_pct)
            assessments.append(assessment)
        return assessments

    @staticmethod
    def _assessment_heat_pct(
        assessment: RiskAssessment, account_equity: float | None
    ) -> float | None:
        """Return one approved candidate's heat, guarding inconsistent inputs."""
        if (
            account_equity is None
            or account_equity <= 0
            or assessment.entry_price is None
            or assessment.stop_price is None
            or assessment.max_shares is None
        ):
            return None
        return (
            (assessment.entry_price - assessment.stop_price)
            * assessment.max_shares
            / account_equity
            * 100
        )

    def _assess(
        self,
        candidate: Candidate,
        portfolio: list[Position],
        account_equity: float | None,
        exposure: ExposureDecision | None,
    ) -> RiskAssessment:
        entry_price = candidate.metrics.get("close")
        atr14 = candidate.metrics.get("atr14")
        warnings = tuple(
            self.check_correlation(
                candidate.symbol, portfolio, self._market_store, candidate.as_of
            )
        )

        if exposure is not None and exposure.verdict.value == "CASH_PRIORITY":
            return RiskAssessment(
                symbol=candidate.symbol,
                status="rejected",
                max_shares=0,
                entry_price=entry_price,
                stop_price=None,
                reasons=(REGIME_CASH_PRIORITY_REASON,),
                warnings=warnings,
                binding_constraint="regime",
                max_trade_risk_pct=0.0,
            )

        if entry_price is None or atr14 is None:
            return RiskAssessment(
                symbol=candidate.symbol,
                status="not_calculable",
                max_shares=None,
                entry_price=entry_price,
                stop_price=None,
                reasons=(_MISSING_DATA_REASON,),
                warnings=warnings,
                binding_constraint="not_calculable",
            )
        if account_equity is None:
            return RiskAssessment(
                symbol=candidate.symbol,
                status="not_calculable",
                max_shares=None,
                entry_price=entry_price,
                stop_price=None,
                reasons=(_MISSING_EQUITY_REASON,),
                warnings=warnings,
                binding_constraint="not_calculable",
            )

        effective_risk_pct = self._risk_config.max_trade_risk_pct
        is_reduce_only = (
            exposure is not None and exposure.verdict.value == "REDUCE_ONLY"
        )
        if is_reduce_only:
            multiplier = exposure.reduce_only_risk_multiplier if exposure else 1.0
            effective_risk_pct *= multiplier

        stop_price = entry_price - self._stop_atr_multiple * atr14
        try:
            sizing = calc_position_size(
                account_equity,
                entry_price,
                stop_price,
                self._risk_config.max_position_pct,
                effective_risk_pct,
            )
        except ValueError:
            return RiskAssessment(
                symbol=candidate.symbol,
                status="not_calculable",
                max_shares=None,
                entry_price=entry_price,
                stop_price=stop_price,
                reasons=(_INVALID_STOP_REASON,),
                warnings=warnings,
                binding_constraint="not_calculable",
            )

        sizing_warnings = self._sizing_warnings(
            entry_price, stop_price, account_equity, sizing, effective_risk_pct
        )
        if is_reduce_only:
            sizing_warnings += (SIZING_WARNING_REGIME_REDUCE_ONLY,)

        reasons: list[str] = []
        status = "approved"
        # REQ-004 tie-break: equal intermediate values favor trade_risk.
        binding_constraint: BindingConstraint = (
            "trade_risk"
            if sizing.shares_by_risk <= sizing.shares_by_position_cap
            else "position_cap"
        )
        sector = self._sector_by_symbol.get(candidate.symbol)
        if sector is not None and account_equity > 0:
            existing_exposure = sum(
                position.shares * position.entry_price
                for position in portfolio
                if self._sector_by_symbol.get(position.symbol) == sector
            )
            new_exposure = sizing.shares * entry_price
            if (
                existing_exposure + new_exposure
            ) / account_equity > self._risk_config.max_sector_pct:
                status = "rejected"
                # The sector cap is the actual reason the trade is blocked,
                # regardless of which of trade_risk/position_cap was tighter.
                binding_constraint = "sector"
                reasons.append(
                    f"sector concentration limit exceeded for sector {sector!r}"
                )

        return RiskAssessment(
            symbol=candidate.symbol,
            status=status,
            max_shares=sizing.shares,
            entry_price=entry_price,
            stop_price=stop_price,
            reasons=tuple(reasons),
            warnings=warnings,
            shares_by_risk=sizing.shares_by_risk,
            shares_by_position_cap=sizing.shares_by_position_cap,
            binding_constraint=binding_constraint,
            sizing_warnings=sizing_warnings,
            max_trade_risk_pct=effective_risk_pct,
        )

    def _sizing_warnings(
        self,
        entry_price: float,
        stop_price: float,
        account_equity: float,
        sizing: PositionSizeResult,
        max_trade_risk_pct: float,
    ) -> tuple[str, ...]:
        """REQ-030/REQ-020: friction warnings that never block approval."""
        warnings: list[str] = []
        stop_distance_pct = (entry_price - stop_price) / entry_price * 100
        if stop_distance_pct > self._risk_config.wide_stop_threshold_pct:
            warnings.append(SIZING_WARNING_WIDE_STOP)

        risk_budget = account_equity * max_trade_risk_pct
        if sizing.shares < 1 or risk_budget < _MIN_MEANINGFUL_RISK_BUDGET_USD:
            warnings.append(SIZING_WARNING_SMALL_ACCOUNT_FRICTION)
        return tuple(warnings)

    def check_correlation(
        self,
        candidate_symbol: str,
        portfolio: list[Position],
        market_store: MarketStore,
        as_of: date,
    ) -> list[CorrelationWarning]:
        """Warn (never block) on high correlation with held positions.

        Args:
            candidate_symbol: The candidate being evaluated.
            portfolio: Currently open positions.
            market_store: Store to read historical daily bars from.
            as_of: Point-in-time cutoff for the correlation lookback window.

        Returns:
            One warning per held symbol that is either highly correlated
            with `candidate_symbol` or lacks enough history to tell
            (`warning_type="data_quality"` — never silently skipped).
        """
        held_symbols = sorted({position.symbol for position in portfolio})
        if not held_symbols:
            return []

        lookback = self._risk_config.correlation_lookback_days
        start = as_of - pd.Timedelta(days=lookback * 3)

        candidate_bars = market_store.read_bars([candidate_symbol], start, as_of, as_of)
        candidate_returns = _daily_returns(candidate_bars, lookback)

        warnings: list[CorrelationWarning] = []
        for symbol in held_symbols:
            if candidate_returns is None:
                warnings.append(
                    CorrelationWarning(symbol, float("nan"), "data_quality")
                )
                continue

            position_bars = market_store.read_bars([symbol], start, as_of, as_of)
            position_returns = _daily_returns(position_bars, lookback)
            if position_returns is None:
                warnings.append(
                    CorrelationWarning(symbol, float("nan"), "data_quality")
                )
                continue

            aligned = pd.concat(
                [
                    candidate_returns.rename("candidate"),
                    position_returns.rename("position"),
                ],
                axis=1,
                join="inner",
            ).dropna()
            if len(aligned) < lookback or (aligned.nunique() <= 1).any():
                warnings.append(
                    CorrelationWarning(symbol, float("nan"), "data_quality")
                )
                continue

            correlation = aligned["candidate"].corr(aligned["position"])
            if math.isnan(correlation):
                warnings.append(
                    CorrelationWarning(symbol, float("nan"), "data_quality")
                )
            elif correlation > self._risk_config.max_correlation:
                warnings.append(
                    CorrelationWarning(symbol, float(correlation), "high_correlation")
                )
        return warnings
