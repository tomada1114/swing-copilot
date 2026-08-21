"""Tests for the backtest's production market-state gate port (Issue #184).

The contract under test is "the simulator applies the *production* gates, on
*point-in-time* inputs" — so every case here pins either a gate that must fire
(and the reason it is reported under) or an as-of boundary that must hold.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from swing_copilot.backtest.metrics import (
    ENTRY_BLOCK_EARNINGS,
    ENTRY_BLOCK_NOT_CALCULABLE,
    ENTRY_BLOCK_REGIME,
)
from swing_copilot.backtest.policy import (
    EntryPolicy,
    EntryPolicyArm,
    EntryPolicyError,
    EntryPolicyRequest,
    build_entry_policy,
    parse_policy_arms,
)
from swing_copilot.data.earnings import EarningsEvent, EarningsLookup
from swing_copilot.risk.checks import EarningsGuardInput
from swing_copilot.screening.base import Candidate
from tests.backtest.conftest import bar_row, bars_frame, flat_bars

if TYPE_CHECKING:
    from collections.abc import Callable

    from swing_copilot.config import Settings

# Long enough for the 25-day Distribution Day window (26 prices) and for the
# production gate's SMA200. A shorter fixture would silently test the UNKNOWN
# path rather than the policy's actual exposure branches.
_DAYS = [date(2027, 1, 1) + timedelta(days=index) for index in range(260)]


def _rising(
    symbol: str, days: list[date], start_price: float
) -> list[dict[str, object]]:
    return [
        bar_row(
            symbol,
            day,
            (
                start_price + index,
                start_price + index + 1,
                start_price + index - 1,
                start_price + index,
            ),
        )
        for index, day in enumerate(days)
    ]


def _neutral(
    symbol: str, days: list[date], start_price: float
) -> list[dict[str, object]]:
    """Drift down gently enough to remain inside the SMA200 buffer."""
    return [
        bar_row(
            symbol,
            day,
            (
                start_price - index * 0.1,
                start_price - index * 0.1 + 1,
                start_price - index * 0.1 - 1,
                start_price - index * 0.1,
            ),
        )
        for index, day in enumerate(days)
    ]


def _market_bars(
    *, vix: float, is_rising: bool = False, days: list[date] | None = None
) -> list[dict[str, object]]:
    """SPY/QQQ/^VIX rows that pin one deterministic Exposure Ceiling verdict.

    A gentle decline keeps SPY inside the SMA200 buffer (-> REDUCE_ONLY);
    rising ones clear the SMA200 (-> NEW_ENTRY_ALLOWED while VIX is calm).
    Constant volume means no bar can ever be a Distribution Day, so the DD
    level is a known NORMAL rather than an UNKNOWN that would conservatively
    downgrade the verdict.
    """
    days = days or _DAYS
    index_rows = (
        [*_rising("SPY", days, 400.0), *_rising("QQQ", days, 350.0)]
        if is_rising
        else [*_neutral("SPY", days, 400.0), *_neutral("QQQ", days, 350.0)]
    )
    return [*index_rows, *flat_bars("^VIX", days, vix)]


def _candidate(symbol: str = "AAA", *, close: float = 100.0) -> Candidate:
    return Candidate(
        symbol=symbol,
        as_of=_DAYS[-1],
        signal_names=("trend_sma",),
        metrics={"close": close, "atr14": 2.0},
        rank=1,
    )


def _request(
    *,
    as_of: date | None = None,
    candidates: tuple[Candidate, ...] = (),
) -> EntryPolicyRequest:
    return EntryPolicyRequest(
        as_of=as_of or _DAYS[-1],
        candidates=candidates or (_candidate(),),
    )


def _policy(
    settings: Settings,
    arm: EntryPolicyArm,
    rows: list[dict[str, object]],
    earnings_guard_fn: Callable[[date, tuple[str, ...]], EarningsGuardInput]
    | None = None,
) -> EntryPolicy:
    policy = build_entry_policy(
        arm,
        settings,
        bars_frame(rows),
        earnings_guard_fn=earnings_guard_fn,
    )
    assert policy is not None
    return policy


class TestArmSelection:
    def test_none_arm_builds_no_policy(self, settings):
        assert (
            build_entry_policy(
                EntryPolicyArm.NONE,
                settings,
                bars_frame(_market_bars(vix=12.0)),
            )
            is None
        )

    def test_missing_regime_bars_fail_fast_instead_of_blocking_silently(self, settings):
        rows = flat_bars("AAA", _DAYS, 100.0)

        with pytest.raises(EntryPolicyError, match=r"QQQ"):
            build_entry_policy(EntryPolicyArm.REGIME, settings, bars_frame(rows))

    def test_empty_bars_frame_fails_fast(self, settings):
        with pytest.raises(EntryPolicyError, match=r"SPY"):
            build_entry_policy(EntryPolicyArm.REGIME, settings, bars_frame([]))


class TestRegimeGate:
    def test_cash_priority_blocks_every_candidate_with_the_regime_reason(
        self, settings
    ):
        policy = _policy(settings, EntryPolicyArm.REGIME, _market_bars(vix=45.0))

        decision = policy.decide(_request())["AAA"]

        assert decision.is_allowed is False
        assert decision.reject_reason == ENTRY_BLOCK_REGIME

    def test_reduce_only_is_a_label_and_does_not_override_simulator_sizing(
        self, settings
    ):
        policy = _policy(settings, EntryPolicyArm.REGIME, _market_bars(vix=15.0))

        decision = policy.decide(_request())["AAA"]

        assert decision.is_allowed is True

    def test_new_entry_allowed_does_not_override_simulator_sizing(self, settings):
        policy = _policy(
            settings,
            EntryPolicyArm.REGIME,
            [*_market_bars(vix=12.0, is_rising=True), *flat_bars("AAA", _DAYS, 100.0)],
        )

        decision = policy.decide(_request())["AAA"]

        assert decision.is_allowed is True

    def test_a_candidate_the_checker_cannot_size_is_withheld_fail_closed(
        self, settings
    ):
        policy = _policy(
            settings,
            EntryPolicyArm.REGIME,
            [*_market_bars(vix=12.0, is_rising=True), *flat_bars("AAA", _DAYS, 100.0)],
        )
        no_price = Candidate(
            symbol="AAA",
            as_of=_DAYS[-1],
            signal_names=("trend_sma",),
            metrics={},
            rank=1,
        )

        decision = policy.decide(_request(candidates=(no_price,)))["AAA"]

        assert decision.is_allowed is False
        assert decision.reject_reason == ENTRY_BLOCK_NOT_CALCULABLE


class TestAsOfDiscipline:
    """The gate must read the signal day's close and nothing newer."""

    @pytest.fixture
    def spiking_policy(self, settings):
        # VIX is calm through _DAYS[-2] and panics from _DAYS[-1] onward, so
        # the verdict flips exactly at the cutoff day.
        calm_days, panic_days = _DAYS[:-1], _DAYS[-1:]
        rows = [
            *_rising("SPY", _DAYS, 400.0),
            *_rising("QQQ", _DAYS, 350.0),
            *flat_bars("^VIX", calm_days, 12.0),
            *flat_bars("^VIX", panic_days, 45.0),
            *flat_bars("AAA", _DAYS, 100.0),
        ]
        return _policy(settings, EntryPolicyArm.REGIME, rows)

    def test_bar_immediately_before_the_cutoff_leaves_entries_allowed(
        self, spiking_policy
    ):
        decision = spiking_policy.decide(_request(as_of=_DAYS[-2]))["AAA"]

        assert decision.is_allowed is True

    def test_bar_exactly_at_the_cutoff_is_included_and_blocks(self, spiking_policy):
        decision = spiking_policy.decide(_request(as_of=_DAYS[-1]))["AAA"]

        assert decision.reject_reason == ENTRY_BLOCK_REGIME

    def test_bar_after_the_cutoff_cannot_reach_back_and_block_an_earlier_day(
        self, spiking_policy
    ):
        # Same frame, same panic bar, but evaluated two days earlier: a
        # future bar must not change a past decision.
        decision = spiking_policy.decide(_request(as_of=_DAYS[-3]))["AAA"]

        assert decision.is_allowed is True


class TestEarningsGuard:
    @staticmethod
    def _guard(as_of: date, symbols: tuple[str, ...]) -> EarningsGuardInput:
        event = EarningsEvent(
            symbol=symbols[0],
            earnings_date=as_of + timedelta(days=1),
            session="amc",
            fetched_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        return EarningsGuardInput(
            is_enabled=True,
            lookups_by_symbol={symbols[0]: EarningsLookup("found", event, None)},
        )

    def test_injected_lookup_blocks_with_the_earnings_reason(self, settings):
        policy = _policy(
            settings,
            EntryPolicyArm.REGIME_EARNINGS,
            [*_market_bars(vix=12.0, is_rising=True), *flat_bars("AAA", _DAYS, 100.0)],
            earnings_guard_fn=self._guard,
        )

        decision = policy.decide(_request())["AAA"]

        assert decision.reject_reason == ENTRY_BLOCK_EARNINGS

    def test_without_a_lookup_source_the_earnings_gate_stays_inert(self, settings):
        policy = _policy(
            settings,
            EntryPolicyArm.REGIME_EARNINGS,
            [*_market_bars(vix=12.0, is_rising=True), *flat_bars("AAA", _DAYS, 100.0)],
        )

        decision = policy.decide(_request())["AAA"]

        assert decision.is_allowed is True


class TestParsePolicyArms:
    def test_comma_separated_list_keeps_the_given_order(self):
        assert parse_policy_arms("regime+earnings,none") == (
            EntryPolicyArm.REGIME_EARNINGS,
            EntryPolicyArm.NONE,
        )

    def test_whitespace_is_tolerated(self):
        assert parse_policy_arms(" none , regime ") == (
            EntryPolicyArm.NONE,
            EntryPolicyArm.REGIME,
        )

    def test_unknown_arm_is_rejected_with_the_available_values(self):
        with pytest.raises(
            EntryPolicyError, match=r"利用可能: none, regime, regime\+earnings"
        ):
            parse_policy_arms("regime,bogus")

    def test_duplicate_arm_is_rejected(self):
        with pytest.raises(EntryPolicyError, match=r"重複"):
            parse_policy_arms("none,none")

    def test_empty_value_is_rejected(self):
        with pytest.raises(EntryPolicyError, match=r"少なくとも1つ"):
            parse_policy_arms(" , ")
