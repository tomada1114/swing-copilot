"""Production entry gates, wrapped as an injectable backtest port (Issue #184).

Between a ranked candidate and an actual position, `pipeline/daily.py` puts
six gates: the regime Exposure Ceiling (`CASH_PRIORITY` blocks outright,
`REDUCE_ONLY` halves the per-trade risk budget), portfolio heat, the earnings
proximity block, the realized-P&L circuit breaker, and the sector cap. The
simulator used to apply none of them, so `risk.reduce_only_risk_multiplier`,
`risk.max_portfolio_heat_pct`, `risk.earnings_block_business_days` and
`risk.circuit_*` could not move a backtest number by construction — and "did
the regime gate improve the results?" had no answer anywhere in the repository.

This module closes that hole **by wrapping `risk/checks.py::RiskChecker`, not
by reimplementing it**. A second copy of the gates inside the engine would
measure a second system and reintroduce exactly the divergence being fixed, so
`RiskCheckerEntryPolicy` builds the production checker once per simulated
decision day and translates its `RiskAssessment`s into engine-level verdicts.
Arms that exercise fewer gates (`EntryPolicyArm.REGIME`) configure the other
ceilings out of the way rather than branching around the checker.

Two things the policy deliberately does *not* decide:

- **The share count.** `RiskChecker` sizes from the signal day's close, while
  the engine fills at the next open plus adverse slippage. The policy returns
  the *effective* `max_trade_risk_pct` (already halved under `REDUCE_ONLY`)
  and the engine performs the single sizing call at the real fill price, so
  the two never disagree about what was bought.
- **The as-of date.** `EntryPolicyRequest.as_of` is the *signal* day, never
  the fill day: at tomorrow's open, today's close is the newest observable
  fact, so evaluating the regime on the fill day's own bar would be exactly
  the look-ahead the simulator exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast
from uuid import NAMESPACE_URL, uuid5

import pandas as pd

from swing_copilot.backtest.metrics import (
    ENTRY_BLOCK_CIRCUIT_BREAKER,
    ENTRY_BLOCK_EARNINGS,
    ENTRY_BLOCK_NOT_CALCULABLE,
    ENTRY_BLOCK_PORTFOLIO_HEAT,
    ENTRY_BLOCK_REGIME,
    ENTRY_BLOCK_SECTOR,
)
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.models import Position
from swing_copilot.regime.distribution import DistributionThresholds
from swing_copilot.regime.exposure import determine_exposure
from swing_copilot.regime.gate import (
    GateThresholds,
    RegimeThresholds,
    calculate_regime_snapshot,
)
from swing_copilot.risk.checks import (
    CIRCUIT_BREAKER_REASON_PREFIX,
    EARNINGS_PROXIMITY_BLOCK_REASON,
    PORTFOLIO_HEAT_EXCEEDED_REASON,
    PORTFOLIO_HEAT_NOT_CALCULABLE_REASON,
    REGIME_CASH_PRIORITY_REASON,
    EarningsGuardInput,
    RiskChecker,
    RiskRunContext,
)
from swing_copilot.risk.circuit_breaker import (
    CircuitThresholds,
    RealizedTrade,
    evaluate_circuit_breaker,
    evaluation_time_for_as_of,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import date

    from swing_copilot.config import RegimeConfig, Settings
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.risk.circuit_breaker import CircuitBreakerResult
    from swing_copilot.screening.base import Candidate
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.universe import UniverseMember

#: Index/volatility symbols `calculate_regime_snapshot` needs. Narrower than
#: `report/daily_brief.MARKET_STRIP_SYMBOLS` on purpose: `^TNX` is header
#: decoration and never reaches the gate.
REGIME_SYMBOLS = ("SPY", "QQQ", "^VIX")

#: Deterministic namespace for the synthetic `Position.position_id`s handed to
#: `RiskChecker`. A random UUID would make two runs of the same backtest differ
#: in a field nothing reads; `uuid5` keeps reproducibility exact.
_POSITION_NAMESPACE = uuid5(NAMESPACE_URL, "swing_copilot/backtest/policy")

#: Heat ceiling used by the regime-only arm. `max_portfolio_heat_pct` has no
#: "disabled" value, so the arm sets one no realistic portfolio can reach
#: instead of branching around the production checker.
_UNBOUNDED_HEAT_PCT = 1e12

#: Sector ceiling used by the regime-only arm. Deliberately above the config
#: field's own `le=1.0` bound, which `model_copy(update=...)` does not
#: re-validate: the sector check compares *cost basis* against *current*
#: equity, so a plain 1.0 would start blocking inside a drawdown and quietly
#: put a second gate into the regime-only arm.
_UNBOUNDED_SECTOR_PCT = 1e12


class EntryPolicyError(SwingCopilotError):
    """Raised when an entry policy cannot be built from the supplied inputs."""


class EntryPolicyArm(StrEnum):
    """Which production gates one backtest arm applies."""

    #: Deterministic screening/sizing only — the pre-Issue-#184 simulator.
    NONE = "none"
    #: Regime Exposure Ceiling only (`CASH_PRIORITY` block, `REDUCE_ONLY`
    #: risk halving). Heat and sector ceilings are configured out of the way;
    #: the earnings guard and circuit breaker stay off.
    REGIME = "regime"
    #: Every gate the simulator can supply inputs for: regime, portfolio heat,
    #: sector cap, and the realized-P&L circuit breaker fed from the run's own
    #: closed trades.
    REGIME_RISK = "regime+risk"


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """One candidate's gate verdict for one simulated decision day."""

    is_allowed: bool
    #: Effective per-trade risk budget the engine must size with, already
    #: reduced by the `REDUCE_ONLY` multiplier. `None` leaves the engine on
    #: its configured default.
    max_trade_risk_pct: float | None = None
    #: One of `metrics.ENTRY_BLOCK_REASONS`; `None` while `is_allowed`.
    reject_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EntryPolicyRequest:
    """Everything a policy may look at, resolved as of the signal day."""

    #: The signal day (the candidates' own `as_of`), never the fill day.
    as_of: date
    candidates: tuple[Candidate, ...]
    open_positions: tuple[Position, ...]
    #: Account equity as of `as_of`: simulated cash plus marked positions.
    equity: float
    #: `(exit_date, realized pnl)` for every trade already closed, feeding the
    #: circuit breaker the same way `pipeline/daily.py` feeds it closed paper
    #: positions.
    realized_pnl_history: tuple[tuple[date, float], ...] = ()


class EntryPolicy(Protocol):
    """The single port through which the engine consults production gates."""

    def decide(self, request: EntryPolicyRequest) -> Mapping[str, EntryDecision]:
        """Return one decision per candidate symbol in `request`."""
        ...  # pragma: no cover


def build_entry_policy(
    arm: EntryPolicyArm,
    settings: Settings,
    universe: tuple[UniverseMember, ...],
    bars: pd.DataFrame,
    *,
    earnings_guard_fn: Callable[[date, tuple[str, ...]], EarningsGuardInput]
    | None = None,
) -> EntryPolicy | None:
    """Build the policy for one A/B arm.

    Args:
        arm: Which gates this arm applies.
        settings: Loaded application settings (`risk.*`, `regime.*`).
        universe: Current universe, supplying the symbol -> GICS sector map.
        bars: The backtest's whole tidy OHLCV frame. It must carry
            `REGIME_SYMBOLS`; the policy trims every read at its own `as_of`.
        earnings_guard_fn: Point-in-time earnings lookups for `as_of` and the
            symbols under consideration. Omitted, the earnings block is
            inert — the simulator has no historical earnings calendar of its
            own, and inventing one would be worse than reporting zero.

    Returns:
        The policy, or `None` for `EntryPolicyArm.NONE` (the engine then
        applies no gate at all).

    Raises:
        EntryPolicyError: `bars` lacks one of `REGIME_SYMBOLS`, so the regime
            could only ever evaluate to `UNKNOWN` and, fail-closed, block
            every entry for the whole window.
    """
    if arm is EntryPolicyArm.NONE:
        return None
    _require_regime_bars(bars)
    is_full = arm is EntryPolicyArm.REGIME_RISK
    return RiskCheckerEntryPolicy(
        settings if is_full else _regime_only_settings(settings),
        universe,
        bars,
        is_circuit_breaker_enabled=is_full,
        earnings_guard_fn=earnings_guard_fn if is_full else None,
    )


def _require_regime_bars(bars: pd.DataFrame) -> None:
    present = set(bars["symbol"].unique()) if not bars.empty else set()
    missing = [symbol for symbol in REGIME_SYMBOLS if symbol not in present]
    if missing:
        msg = (
            f"レジームゲートに必要なバーがありません: {', '.join(missing)}。"
            "SPY/QQQ/^VIX を価格履歴へ取り込んでから --policy を指定してください。"
        )
        raise EntryPolicyError(msg)


def _regime_only_settings(settings: Settings) -> Settings:
    """Configure the non-regime ceilings out of the way, without branching.

    The alternative — skipping parts of `RiskChecker` — would put a second
    implementation of "which gates apply" inside the backtest, which is the
    duplication this module exists to prevent.
    """
    return settings.model_copy(
        update={
            "risk": settings.risk.model_copy(
                update={
                    "max_portfolio_heat_pct": _UNBOUNDED_HEAT_PCT,
                    "max_sector_pct": _UNBOUNDED_SECTOR_PCT,
                }
            )
        }
    )


def _regime_thresholds(config: RegimeConfig) -> RegimeThresholds:
    """Mirror `pipeline/daily.py::_calculate_regime_snapshot`'s threshold wiring."""
    return RegimeThresholds(
        gate=GateThresholds(
            ema_period=config.ema_period,
            bull_vix_max=config.bull_vix_max,
            bear_spy_ema_ratio=config.bear_spy_ema_ratio,
            bear_vix_min=config.bear_vix_min,
        ),
        distribution=DistributionThresholds(
            window_days=config.distribution_window_days,
            dd_decline_pct=config.dd_decline_pct,
            stall_abs_change_pct=config.stall_abs_change_pct,
            recovery_pct=config.recovery_pct,
            severe_d25=config.dd_severe_d25,
            severe_d15=config.dd_severe_d15,
            high_d25=config.dd_high_d25,
            high_d15=config.dd_high_d15,
            high_d5=config.dd_high_d5,
            caution_d25=config.dd_caution_d25,
        ),
    )


def as_position(
    symbol: str, entry_date: date, entry_price: float, shares: int, stop_price: float
) -> Position:
    """Represent one open simulated holding the way `RiskChecker` expects it.

    Args:
        symbol: Held ticker.
        entry_date: Fill date.
        entry_price: Executed entry price, slippage included.
        shares: Share count.
        stop_price: Current stop, which portfolio heat requires to be set.

    Returns:
        An `open`, paper `Position` with a deterministic identifier.
    """
    return Position(
        position_id=uuid5(_POSITION_NAMESPACE, f"{symbol}:{entry_date.isoformat()}"),
        symbol=symbol,
        is_paper=True,
        entry_date=entry_date,
        entry_price=entry_price,
        shares=shares,
        status="open",
        stop_price=stop_price,
    )


class _FrameBarReader:
    """`MarketStore.read_bars`-shaped reader over the simulator's own frame.

    `RiskChecker` reads bars for exactly one thing: the correlation *warning*,
    which never blocks a candidate. Serving it from the frame the engine
    already holds keeps the policy offline and free of a second storage
    handle, and lets the as-of cutoff be tested directly. Rows are pre-grouped
    by symbol so the per-candidate lookback never scans the whole frame.
    """

    def __init__(self, bars: pd.DataFrame) -> None:
        self._columns = list(bars.columns)
        self._by_symbol: dict[str, pd.DataFrame] = {
            str(symbol): frame.sort_values("date")
            for symbol, frame in bars.groupby("symbol")
        }

    def read_bars(
        self, symbols: list[str], start: date, end: date, as_of: date
    ) -> pd.DataFrame:
        """Return `symbols`' bars over `[start, end]`, never past `as_of`.

        Args:
            symbols: Ticker symbols to read.
            start: Inclusive range start.
            end: Inclusive range end.
            as_of: Point-in-time guard; the boundary row itself is included.

        Returns:
            Tidy bars, ordered by symbol then date.
        """
        # `date` objects throughout: `RiskChecker` derives its window start as
        # `as_of - pd.Timedelta(...)`, which stays a plain `date`, and the
        # frame's `date` column is object-dtype `date` (as `engine._bar`
        # already assumes).
        effective_end = min(end, as_of)
        frames = [
            frame.loc[(frame["date"] >= start) & (frame["date"] <= effective_end)]
            for symbol in sorted(symbols)
            if (frame := self._by_symbol.get(symbol)) is not None
        ]
        if not frames:
            return pd.DataFrame(columns=self._columns)
        return pd.concat(frames, ignore_index=True)


class RiskCheckerEntryPolicy:
    """`EntryPolicy` implemented by running the production `RiskChecker`."""

    def __init__(
        self,
        settings: Settings,
        universe: tuple[UniverseMember, ...],
        bars: pd.DataFrame,
        *,
        is_circuit_breaker_enabled: bool = False,
        earnings_guard_fn: Callable[[date, tuple[str, ...]], EarningsGuardInput]
        | None = None,
    ) -> None:
        """Create the policy.

        Args:
            settings: Settings the wrapped checker runs under; an arm that
                exercises fewer gates supplies a copy with the other ceilings
                configured out of the way.
            universe: Current universe, for the sector map.
            bars: The backtest's tidy OHLCV frame, covering `REGIME_SYMBOLS`.
            is_circuit_breaker_enabled: Whether the run's own realized P&L
                feeds `evaluate_circuit_breaker`.
            earnings_guard_fn: Point-in-time earnings lookups, or `None` to
                leave the earnings block inert.
        """
        self._settings = settings
        self._universe = universe
        # Sliced once: `decide` runs per simulated day and must not re-scan
        # the whole multi-year frame for the index strip every time.
        self._regime_bars = {
            symbol: bars.loc[bars["symbol"] == symbol] for symbol in REGIME_SYMBOLS
        }
        self._bar_reader = _FrameBarReader(bars)
        self._thresholds = _regime_thresholds(settings.regime)
        self._is_circuit_breaker_enabled = is_circuit_breaker_enabled
        self._earnings_guard_fn = earnings_guard_fn

    def decide(self, request: EntryPolicyRequest) -> Mapping[str, EntryDecision]:
        """Assess every candidate through the production gates.

        Args:
            request: The signal day's candidates, holdings, and equity.

        Returns:
            `{symbol: decision}` for exactly the candidates supplied.
        """
        snapshot = calculate_regime_snapshot(
            self._regime_bars["SPY"],
            self._regime_bars["QQQ"],
            self._regime_bars["^VIX"],
            request.as_of,
            thresholds=self._thresholds,
        )
        exposure = determine_exposure(
            snapshot,
            reduce_only_risk_multiplier=self._settings.regime.reduce_only_risk_multiplier,
        )
        checker = RiskChecker(
            self._settings,
            self._universe,
            # `RiskChecker` touches the store only through `read_bars`, for
            # the correlation warning. Widening its annotation belongs to the
            # checks-registry refactor (Issue #193), not here, so the seam is
            # spelled out as one documented cast instead.
            cast("MarketStore", self._bar_reader),
            RiskRunContext(
                earnings_guard=self._earnings_guard(request),
                circuit_breaker=self._circuit_breaker(request),
            ),
        )
        assessments = checker.check(
            list(request.candidates),
            list(request.open_positions),
            request.equity,
            exposure,
        )
        return {
            assessment.symbol: _to_decision(assessment) for assessment in assessments
        }

    def _earnings_guard(self, request: EntryPolicyRequest) -> EarningsGuardInput | None:
        if self._earnings_guard_fn is None:
            return None
        symbols = tuple(candidate.symbol for candidate in request.candidates)
        return self._earnings_guard_fn(request.as_of, symbols)

    def _circuit_breaker(
        self, request: EntryPolicyRequest
    ) -> CircuitBreakerResult | None:
        if not self._is_circuit_breaker_enabled:
            return None
        config = self._settings.risk
        return evaluate_circuit_breaker(
            [
                RealizedTrade(evaluation_time_for_as_of(exit_date), pnl)
                for exit_date, pnl in request.realized_pnl_history
            ],
            request.equity,
            request.as_of,
            evaluation_time_for_as_of(request.as_of),
            CircuitThresholds(
                config.circuit_daily_loss_pct,
                config.circuit_weekly_loss_pct,
                config.circuit_monthly_loss_pct,
                config.circuit_consecutive_losses,
                config.circuit_cooldown_hours,
            ),
        )


def _to_decision(assessment: RiskAssessment) -> EntryDecision:
    if assessment.status == "approved":
        return EntryDecision(
            is_allowed=True, max_trade_risk_pct=assessment.max_trade_risk_pct
        )
    return EntryDecision(is_allowed=False, reject_reason=_block_reason(assessment))


def _block_reason(assessment: RiskAssessment) -> str:
    """Pick the one gate reported as "why this entry was not taken".

    Several gates can fire on the same candidate (a `CASH_PRIORITY` day also
    trips the not-calculable heat path, for instance), so the order below is
    the contract: the earliest match wins, running from the most decisive
    market-wide block down to the per-candidate ones. `binding_constraint` is
    consulted only for the sector cap, whose reason string is free text.
    """
    reasons = assessment.reasons
    if REGIME_CASH_PRIORITY_REASON in reasons:
        return ENTRY_BLOCK_REGIME
    if any(reason.startswith(CIRCUIT_BREAKER_REASON_PREFIX) for reason in reasons):
        return ENTRY_BLOCK_CIRCUIT_BREAKER
    if EARNINGS_PROXIMITY_BLOCK_REASON in reasons:
        return ENTRY_BLOCK_EARNINGS
    if (
        PORTFOLIO_HEAT_EXCEEDED_REASON in reasons
        or PORTFOLIO_HEAT_NOT_CALCULABLE_REASON in reasons
    ):
        return ENTRY_BLOCK_PORTFOLIO_HEAT
    if assessment.binding_constraint == "sector":
        return ENTRY_BLOCK_SECTOR
    return ENTRY_BLOCK_NOT_CALCULABLE


def parse_policy_arms(raw: str) -> tuple[EntryPolicyArm, ...]:
    """Parse a comma-separated `--policy` value into distinct arms, in order.

    Args:
        raw: e.g. `"none,regime+risk"`.

    Returns:
        The arms in the order given, which is the order the report compares.

    Raises:
        EntryPolicyError: An arm is unknown, repeated, or the value is empty.
    """
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        msg = "--policy には少なくとも1つのアームを指定してください。"
        raise EntryPolicyError(msg)
    arms: list[EntryPolicyArm] = []
    available = ", ".join(arm.value for arm in EntryPolicyArm)
    for token in tokens:
        try:
            arm = EntryPolicyArm(token)
        except ValueError as exc:
            msg = f"未知の --policy 値 '{token}' です。利用可能: {available}"
            raise EntryPolicyError(msg) from exc
        if arm in arms:
            msg = f"--policy に '{token}' が重複しています。"
            raise EntryPolicyError(msg)
        arms.append(arm)
    return tuple(arms)
