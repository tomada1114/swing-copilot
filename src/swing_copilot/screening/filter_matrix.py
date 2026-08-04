"""Per-check independent pass rates and their overlap, for one strategy.

The rejection ledger (`screening_rejections`) records only the *first* check a
symbol failed, in `strategies.yaml` order, so it cannot answer the questions a
parameter-tuning session actually asks: how much does each check reject on its
own, how much of that rejection is shared with another check, and which checks
are somebody's only blocker.

This module answers them by applying every configured Filter and Signal
*independently* to the whole universe -- the real registered components via
`build_strategy_components`, never a re-implementation -- and by splitting each
non-pass into a genuine threshold miss (`FAILED`) and a data gap (`NO_DATA`)
using the existing rejection classifier's own `data_quality` stage.

Pure and point-in-time: everything is derived from the caller's
`ScreeningInput`, so the visibility cutoff is whatever `as_of` the repositories
already enforced. Nothing here reads the clock, the network, or storage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from typing import TYPE_CHECKING

from swing_copilot.screening.base import RejectionStage
from swing_copilot.screening.pipeline import build_strategy_components
from swing_copilot.screening.rejection_classifier import (
    classify_filter_rejection,
    classify_signal_rejection,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

    from swing_copilot.config import Settings, StrategySpec
    from swing_copilot.screening.base import ScreeningInput


class CheckKind(Enum):
    """Which screening stage a check belongs to."""

    FILTER = "filter"
    SIGNAL = "signal"


class CheckOutcome(Enum):
    """One symbol's result for one check, applied on its own.

    `NO_DATA` is not a softer `FAILED`: a symbol without enough bars or filings
    tells you nothing about whether the threshold is too tight, so it is
    counted as its own category everywhere (matching the ledger's
    `data_quality` rejection stage).
    """

    PASSED = "passed"
    FAILED = "failed"
    NO_DATA = "no_data"


@dataclass(frozen=True, slots=True)
class StrategySelection:
    """The one `strategies.yaml` entry a diagnostic run measures."""

    key: str
    spec: StrategySpec


@dataclass(frozen=True, slots=True)
class CheckStats:
    """One check's independent result over the whole universe."""

    name: str
    kind: CheckKind
    pass_count: int
    fail_count: int
    no_data_count: int
    #: Symbols whose *only* non-pass check is this one: dropping this check
    #: alone would let exactly this many more symbols through.
    sole_blocker_count: int

    @property
    def blocked_count(self) -> int:
        """Symbols this check does not pass, for any reason."""
        return self.fail_count + self.no_data_count

    @property
    def pass_rate(self) -> float | None:
        """Passing share of the universe, or `None` for an empty universe."""
        total = self.pass_count + self.blocked_count
        return None if total == 0 else self.pass_count / total


@dataclass(frozen=True, slots=True)
class FilterMatrixResult:
    """Independent pass rates, blocked-count spread, and the overlap matrix."""

    as_of: date
    strategy_key: str
    universe_size: int
    checks: tuple[CheckStats, ...]
    #: `(number of blocked checks, symbol count)`, ascending. `0` is the
    #: candidate-equivalent bucket (before ranking and `candidate_limit`).
    blocked_count_distribution: tuple[tuple[int, int], ...]
    #: `(first check, second check) -> symbols blocked by both`, keyed in
    #: configured order so the pair appears exactly once.
    co_blocked_counts: Mapping[tuple[str, str], int]
    #: Symbols passing every configured check.
    unblocked_symbols: tuple[str, ...]


def evaluate_filter_matrix(
    data: ScreeningInput, settings: Settings, strategy: StrategySelection
) -> FilterMatrixResult:
    """Apply every configured check independently and summarize the overlap.

    Args:
        data: Point-in-time screening input; its `universe` is the population
            every count below is measured against.
        settings: Loaded application settings, passed to every component.
        strategy: The `strategies.yaml` entry to measure.

    Returns:
        Per-check counts, the blocked-count distribution, the pairwise
        co-blocked matrix, and the symbols nothing blocks.

    Raises:
        KeyError: A configured filter/signal key is not registered.
        NotImplementedError: A configured key has no mirrored classification
            in `rejection_classifier`, so a data gap cannot be told apart
            from a threshold miss for it.
    """
    filters, signals = build_strategy_components(strategy.spec, settings)
    symbols = sorted({member.symbol for member in data.universe})
    universe = set(symbols)

    outcomes: dict[str, dict[str, CheckOutcome]] = {}
    kinds: dict[str, CheckKind] = {}
    order: list[str] = []

    for filter_ in filters:
        passing = filter_.apply(data) & universe
        outcomes[filter_.name] = {
            symbol: CheckOutcome.PASSED
            if symbol in passing
            else _filter_outcome(symbol, data, settings, filter_.name)
            for symbol in symbols
        }
        kinds[filter_.name] = CheckKind.FILTER
        order.append(filter_.name)

    for signal in signals:
        # Evaluated against the *whole* universe, not the filtered subset the
        # pipeline would hand it: an independent pass rate is only meaningful
        # against the same population every other check is measured on.
        passing = {hit.symbol for hit in signal.evaluate(data, universe)}
        outcomes[signal.name] = {
            symbol: CheckOutcome.PASSED
            if symbol in passing
            else _signal_outcome(symbol, data, settings, signal.name)
            for symbol in symbols
        }
        kinds[signal.name] = CheckKind.SIGNAL
        order.append(signal.name)

    blocked_by_symbol = {
        symbol: tuple(
            name for name in order if outcomes[name][symbol] is not CheckOutcome.PASSED
        )
        for symbol in symbols
    }

    pair_counts: Counter[tuple[str, str]] = Counter()
    for blocked in blocked_by_symbol.values():
        pair_counts.update(combinations(blocked, 2))

    return FilterMatrixResult(
        as_of=data.as_of,
        strategy_key=strategy.key,
        universe_size=len(symbols),
        checks=tuple(
            _check_stats(name, kinds[name], outcomes[name], blocked_by_symbol)
            for name in order
        ),
        blocked_count_distribution=tuple(
            sorted(
                Counter(len(blocked) for blocked in blocked_by_symbol.values()).items()
            )
        ),
        co_blocked_counts=dict(pair_counts),
        unblocked_symbols=tuple(
            symbol for symbol in symbols if not blocked_by_symbol[symbol]
        ),
    )


def _check_stats(
    name: str,
    kind: CheckKind,
    outcomes: Mapping[str, CheckOutcome],
    blocked_by_symbol: Mapping[str, tuple[str, ...]],
) -> CheckStats:
    counts = Counter(outcomes.values())
    return CheckStats(
        name=name,
        kind=kind,
        pass_count=counts[CheckOutcome.PASSED],
        fail_count=counts[CheckOutcome.FAILED],
        no_data_count=counts[CheckOutcome.NO_DATA],
        sole_blocker_count=sum(
            1 for blocked in blocked_by_symbol.values() if blocked == (name,)
        ),
    )


def _filter_outcome(
    symbol: str, data: ScreeningInput, settings: Settings, filter_name: str
) -> CheckOutcome:
    record = classify_filter_rejection(symbol, data, settings, filter_name)
    is_data_gap = record is not None and record.stage is RejectionStage.DATA_QUALITY
    return CheckOutcome.NO_DATA if is_data_gap else CheckOutcome.FAILED


def _signal_outcome(
    symbol: str, data: ScreeningInput, settings: Settings, signal_name: str
) -> CheckOutcome:
    record = classify_signal_rejection(symbol, data, settings, signal_name)
    is_data_gap = record.stage is RejectionStage.DATA_QUALITY
    return CheckOutcome.NO_DATA if is_data_gap else CheckOutcome.FAILED
