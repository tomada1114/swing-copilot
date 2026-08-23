"""Market-state entry gates, wrapped as an injectable backtest port (Issue #184).

The public daily path supplies symbol-level trade plans plus regime and earnings
gates. It does not know a reader's account or holdings. The simulator reuses
those point-in-time gates; nominal-money sizing remains inside the engine.

This module closes that hole **by wrapping `risk/checks.py::RiskChecker`, not
by reimplementing it**. A second copy of the gates inside the engine would
measure a second system and reintroduce exactly the divergence being fixed, so
`RiskCheckerEntryPolicy` builds the production checker once per simulated
decision day and translates its `RiskAssessment`s into engine-level verdicts.
The `regime` arm applies only market state; `regime+earnings` adds the
point-in-time earnings gate.

The adapter now owns only the point-in-time market-state and earnings gates.
Nominal sizing is configured by `backtest.*` and executed by the engine; the
production `RiskChecker` remains the source of symbol-level trade-plan values.

Two things the policy deliberately does *not* decide:

- **The share count.** The engine alone performs nominal-money sizing, against
  the worst-case planned limit price rather than the fill price.
  `REDUCE_ONLY` is intentionally not converted into an account-specific risk
  multiplier.
- **The as-of date.** `EntryPolicyRequest.as_of` is the *signal* day, never
  the fill day: at tomorrow's open, today's close is the newest observable
  fact, so evaluating the regime on the fill day's own bar would be exactly
  the look-ahead the simulator exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from swing_copilot.backtest.metrics import (
    ENTRY_BLOCK_EARNINGS,
    ENTRY_BLOCK_NOT_CALCULABLE,
    ENTRY_BLOCK_REGIME,
)
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.regime.distribution import DistributionThresholds
from swing_copilot.regime.exposure import determine_exposure
from swing_copilot.regime.ftd import FtdThresholds
from swing_copilot.regime.gate import (
    GateThresholds,
    RegimeThresholds,
    calculate_regime_snapshot,
)
from swing_copilot.risk.checks import (
    EARNINGS_PROXIMITY_BLOCK_REASON,
    REGIME_CASH_PRIORITY_REASON,
    EarningsGuardInput,
    RiskChecker,
    RiskRunContext,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import date

    import pandas as pd

    from swing_copilot.config import RegimeConfig, Settings
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate

#: Index/volatility symbols `calculate_regime_snapshot` needs. Narrower than
#: `report/daily_brief.MARKET_STRIP_SYMBOLS` on purpose: `^TNX` is header
#: decoration and never reaches the gate.
REGIME_SYMBOLS = ("SPY", "QQQ", "^VIX")


class EntryPolicyError(SwingCopilotError):
    """Raised when an entry policy cannot be built from the supplied inputs."""


class EntryPolicyArm(StrEnum):
    """Which point-in-time gates one backtest arm applies."""

    #: Deterministic screening/sizing only — the pre-Issue-#184 simulator.
    NONE = "none"
    #: Regime Exposure Ceiling only (`CASH_PRIORITY` block, `REDUCE_ONLY`
    #: label; it does not halve risk). The earnings gate stays off.
    REGIME = "regime"
    #: Regime plus the point-in-time earnings gate.
    REGIME_EARNINGS = "regime+earnings"


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """One candidate's gate verdict for one simulated decision day."""

    is_allowed: bool
    #: One of `metrics.ENTRY_BLOCK_REASONS`; `None` while `is_allowed`.
    reject_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EntryPolicyRequest:
    """Everything a policy may look at, resolved as of the signal day."""

    #: The signal day (the candidates' own `as_of`), never the fill day.
    as_of: date
    candidates: tuple[Candidate, ...]


class EntryPolicy(Protocol):
    """The single port through which the engine consults production gates."""

    def decide(self, request: EntryPolicyRequest) -> Mapping[str, EntryDecision]:
        """Return one decision per candidate symbol in `request`."""
        ...  # pragma: no cover


def build_entry_policy(
    arm: EntryPolicyArm,
    settings: Settings,
    bars: pd.DataFrame,
    *,
    earnings_guard_fn: Callable[[date, tuple[str, ...]], EarningsGuardInput]
    | None = None,
) -> EntryPolicy | None:
    """Build the policy for one A/B arm.

    Args:
        arm: Which gates this arm applies.
        settings: Loaded application settings (`risk.*`, `regime.*`).
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
    is_earnings = arm is EntryPolicyArm.REGIME_EARNINGS
    return RiskCheckerEntryPolicy(
        settings,
        bars,
        earnings_guard_fn=earnings_guard_fn if is_earnings else None,
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


def _regime_thresholds(config: RegimeConfig) -> RegimeThresholds:
    """Mirror the daily regime snapshot's threshold wiring exactly."""
    return RegimeThresholds(
        gate=GateThresholds(
            sma_period=config.sma_period,
            bear_spy_sma_ratio=config.bear_spy_sma_ratio,
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
        ftd=FtdThresholds(
            correction_decline_pct=config.ftd_correction_decline_pct,
            correction_down_days=config.ftd_correction_down_days,
            ftd_gain_pct=config.ftd_gain_pct,
        ),
    )


class RiskCheckerEntryPolicy:
    """`EntryPolicy` implemented by running the production `RiskChecker`."""

    def __init__(
        self,
        settings: Settings,
        bars: pd.DataFrame,
        *,
        earnings_guard_fn: Callable[[date, tuple[str, ...]], EarningsGuardInput]
        | None = None,
    ) -> None:
        """Create the policy.

        Args:
            settings: Settings the wrapped checker runs under.
            bars: The backtest's tidy OHLCV frame, covering `REGIME_SYMBOLS`.
            earnings_guard_fn: Point-in-time earnings lookups, or `None` to
                leave the earnings block inert.
        """
        self._settings = settings
        # Sliced once: `decide` runs per simulated day and must not re-scan
        # the whole multi-year frame for the index strip every time.
        self._regime_bars = {
            symbol: bars.loc[bars["symbol"] == symbol] for symbol in REGIME_SYMBOLS
        }
        self._thresholds = _regime_thresholds(settings.regime)
        self._earnings_guard_fn = earnings_guard_fn

    def decide(self, request: EntryPolicyRequest) -> Mapping[str, EntryDecision]:
        """Assess every candidate through the production gates.

        Args:
            request: The signal day's candidates.

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
        exposure = determine_exposure(snapshot)
        checker = RiskChecker(
            self._settings,
            RiskRunContext(
                earnings_guard=self._earnings_guard(request),
            ),
        )
        assessments = checker.check(list(request.candidates), exposure)
        return {
            assessment.symbol: _to_decision(assessment) for assessment in assessments
        }

    def _earnings_guard(self, request: EntryPolicyRequest) -> EarningsGuardInput | None:
        if self._earnings_guard_fn is None:
            return None
        symbols = tuple(candidate.symbol for candidate in request.candidates)
        return self._earnings_guard_fn(request.as_of, symbols)


def _to_decision(assessment: RiskAssessment) -> EntryDecision:
    if assessment.status == "approved":
        return EntryDecision(is_allowed=True)
    return EntryDecision(is_allowed=False, reject_reason=_block_reason(assessment))


def _block_reason(assessment: RiskAssessment) -> str:
    """Pick the one gate reported as "why this entry was not taken".

    Several gates can fire on the same candidate, so the order below is the
    contract: market state, earnings, then an invalid
    symbol-level trade plan.
    """
    reasons = assessment.reasons
    if REGIME_CASH_PRIORITY_REASON in reasons:
        return ENTRY_BLOCK_REGIME
    if EARNINGS_PROXIMITY_BLOCK_REASON in reasons:
        return ENTRY_BLOCK_EARNINGS
    return ENTRY_BLOCK_NOT_CALCULABLE


def parse_policy_arms(raw: str) -> tuple[EntryPolicyArm, ...]:
    """Parse a comma-separated `--policy` value into distinct arms, in order.

    Args:
        raw: e.g. `"none,regime+earnings"`.

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
