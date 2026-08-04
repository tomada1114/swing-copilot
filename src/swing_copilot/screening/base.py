"""Filter/Signal ABCs, values, and the pluggable registry (NFR-07).

New Filters/Signals are added by writing a class decorated with
`@register_filter`/`@register_signal` and adding its key to
`config/strategies.yaml` — `ScreeningPipeline` never changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import date

    import pandas as pd

    from swing_copilot.config import Settings
    from swing_copilot.universe import UniverseMember


@dataclass(frozen=True, slots=True)
class SignalHit:
    """One symbol's hit on one Signal."""

    symbol: str
    signal_name: str
    direction: str  # "long" only in P1-P2
    strength: float
    metrics: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ScreeningInput:
    """Point-in-time input shared by every Filter and Signal."""

    as_of: date
    universe: tuple[UniverseMember, ...]
    fundamentals: pd.DataFrame
    bars: pd.DataFrame


@dataclass(frozen=True, slots=True)
class Candidate:
    """One symbol's aggregated, ranked screening result."""

    symbol: str
    as_of: date
    signal_names: tuple[str, ...]
    metrics: Mapping[str, float]
    rank: int
    execution_state: str = "UNKNOWN"
    execution_distance: float | None = None


class RejectionStage(Enum):
    """Which screening stage rejected a symbol (P1-02, roadmap §5)."""

    DATA_QUALITY = "data_quality"
    FUNDAMENTAL_FILTER = "fundamental_filter"
    TECHNICAL_SIGNAL = "technical_signal"


class RejectionReasonCode(Enum):
    """Closed enum of rejection reasons (P1-02, roadmap §5).

    Matches Issue #11's enum plus two deliberate additions:

    - `FILTER_LOW_LIQUIDITY`. The issue's enum has no code for the repo's
      actual `volume_min` liquidity filter (`technical_signals.py::
      MinAverageVolumeFilter`, registered as a `Filter`/stage 1 per `Filter`'s
      own docstring). Repo reality wins over the spec document (AGENTS.md
      conflict-resolution rule): recording this divergence here rather than
      silently dropping real liquidity rejections.
    - `DATA_MISSING_NET_INCOME` (P6-25). A `NaN` `net_income` on a quarter
      required by `fundamental_filters.min_profitable_quarters` is a data
      gap (the filing lacked/failed to normalize that concept), not a
      genuinely negative result; conflating the two under
      `FILTER_NEGATIVE_NET_INCOME` misreports a real data-quality problem as
      a business rejection. Stage is `data_quality`, matching
      `DATA_INSUFFICIENT_HISTORY`'s convention.
    """

    FILTER_NEGATIVE_NET_INCOME = "FILTER_NEGATIVE_NET_INCOME"
    FILTER_NEGATIVE_FCF = "FILTER_NEGATIVE_FCF"
    FILTER_LOW_EQUITY_RATIO = "FILTER_LOW_EQUITY_RATIO"
    FILTER_LOW_LIQUIDITY = "FILTER_LOW_LIQUIDITY"  # divergence: see class docstring
    SIGNAL_TREND_NOT_MET = "SIGNAL_TREND_NOT_MET"
    SIGNAL_RSI_NOT_MET = "SIGNAL_RSI_NOT_MET"
    DATA_INSUFFICIENT_HISTORY = "DATA_INSUFFICIENT_HISTORY"
    DATA_MISSING_NET_INCOME = (
        "DATA_MISSING_NET_INCOME"  # divergence: see class docstring
    )


@dataclass(frozen=True, slots=True)
class RejectionRecord:
    """One universe symbol's classified screening rejection (P1-02)."""

    symbol: str
    stage: RejectionStage
    reason_code: RejectionReasonCode
    detail: Mapping[str, float | int | str | None]


@dataclass(frozen=True, slots=True)
class TruncatedCandidate:
    """A symbol that cleared every stage but fell outside `candidate_limit`.

    Nothing rejected it, so it has no `RejectionRecord`: `reason_code` is a
    closed enum backed by a DB CHECK constraint, and inventing a code for
    "ranked 11th" would conflate a configuration cap with a screening
    verdict. Recording it separately is what keeps such a symbol visible at
    all — before this it appeared in neither the candidate list nor the
    rejection ledger.

    `rank` is the symbol's position in the full pre-truncation ranking, so
    `rank > candidate_limit` always holds and the near-misses are obvious.
    """

    symbol: str
    rank: int
    score: float
    score_breakdown: Mapping[str, float]
    execution_state: str
    execution_distance: float | None


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """`ScreeningPipeline.run_with_rejections()`'s full output (P1-02).

    `candidates` matches `run()`'s existing ranked/capped output exactly;
    `rejections` covers every universe symbol that failed a filter or a
    configured signal, classified by `rejection_classifier.py`; `truncated`
    covers the symbols that failed nothing and were cut by `candidate_limit`.
    """

    candidates: list[Candidate]
    rejections: list[RejectionRecord]
    truncated: list[TruncatedCandidate]


class Filter(Protocol):
    """Stage 1: fundamentals/liquidity-based universe narrowing."""

    name: str

    def __init__(self, settings: Settings) -> None:
        """Every Filter takes the full Settings; it plucks its own section."""
        ...  # pragma: no cover

    def apply(self, data: ScreeningInput) -> set[str]:
        """Return the set of symbols satisfying this filter."""
        ...  # pragma: no cover


class Signal(Protocol):
    """Stage 2: technical evaluation over the filtered symbol set."""

    name: str

    def __init__(self, settings: Settings) -> None:
        """Every Signal takes the full Settings; it plucks its own section."""
        ...  # pragma: no cover

    def evaluate(self, data: ScreeningInput, symbols: set[str]) -> list[SignalHit]:
        """Return hits among `symbols` for this signal."""
        ...  # pragma: no cover


FILTER_REGISTRY: dict[str, type[Filter]] = {}
SIGNAL_REGISTRY: dict[str, type[Signal]] = {}


def register_filter(key: str) -> Callable[[type[Filter]], type[Filter]]:
    """Register a `Filter` subclass under `key` in `FILTER_REGISTRY`."""

    def decorator(cls: type[Filter]) -> type[Filter]:
        FILTER_REGISTRY[key] = cls
        return cls

    return decorator


def register_signal(key: str) -> Callable[[type[Signal]], type[Signal]]:
    """Register a `Signal` subclass under `key` in `SIGNAL_REGISTRY`."""

    def decorator(cls: type[Signal]) -> type[Signal]:
        SIGNAL_REGISTRY[key] = cls
        return cls

    return decorator
