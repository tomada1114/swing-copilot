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
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from swing_copilot.risk.position_sizing import calc_position_size

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.config import Settings
    from swing_copilot.models import Position
    from swing_copilot.screening.base import Candidate
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.universe import UniverseMember

_MISSING_DATA_REASON = "missing candidate price/ATR data"
_MISSING_EQUITY_REASON = "account_equity is not set"
_INVALID_STOP_REASON = (
    "ATR-based stop distance is not usable (ATR is zero or unavailable)"
)


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


def _daily_returns(bars: pd.DataFrame, lookback_days: int) -> pd.Series | None:
    if bars.empty:
        return None
    closes = bars.sort_values("date")["close"]
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
    ) -> list[RiskAssessment]:
        """Assess every candidate: sizing, sector concentration, correlation.

        Args:
            candidates: Ranked screening candidates.
            portfolio: Currently open positions (paper or live).
            account_equity: Total account equity in USD, or `None` if unset.

        Returns:
            One `RiskAssessment` per candidate, same order as `candidates`.
        """
        return [
            self._assess(candidate, portfolio, account_equity)
            for candidate in candidates
        ]

    def _assess(
        self,
        candidate: Candidate,
        portfolio: list[Position],
        account_equity: float | None,
    ) -> RiskAssessment:
        entry_price = candidate.metrics.get("close")
        atr14 = candidate.metrics.get("atr14")
        warnings = tuple(
            self.check_correlation(
                candidate.symbol, portfolio, self._market_store, candidate.as_of
            )
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
            )

        stop_price = entry_price - self._stop_atr_multiple * atr14
        try:
            max_shares = calc_position_size(
                account_equity,
                entry_price,
                stop_price,
                self._risk_config.max_position_pct,
                self._risk_config.max_trade_risk_pct,
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
            )

        reasons: list[str] = []
        status = "approved"
        sector = self._sector_by_symbol.get(candidate.symbol)
        if sector is not None and account_equity > 0:
            existing_exposure = sum(
                position.shares * position.entry_price
                for position in portfolio
                if self._sector_by_symbol.get(position.symbol) == sector
            )
            new_exposure = max_shares * entry_price
            if (
                existing_exposure + new_exposure
            ) / account_equity > self._risk_config.max_sector_pct:
                status = "rejected"
                reasons.append(
                    f"sector concentration limit exceeded for sector {sector!r}"
                )

        return RiskAssessment(
            symbol=candidate.symbol,
            status=status,
            max_shares=max_shares,
            entry_price=entry_price,
            stop_price=stop_price,
            reasons=tuple(reasons),
            warnings=warnings,
        )

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

            correlation = candidate_returns.corr(position_returns)
            if math.isnan(correlation):
                warnings.append(
                    CorrelationWarning(symbol, float("nan"), "data_quality")
                )
            elif correlation > self._risk_config.max_correlation:
                warnings.append(
                    CorrelationWarning(symbol, float(correlation), "high_correlation")
                )
        return warnings
