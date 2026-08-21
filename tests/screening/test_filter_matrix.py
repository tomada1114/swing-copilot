"""Tests for the independent per-check screening diagnostic (`filter_matrix`)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from swing_copilot.config import load_settings, load_strategies
from swing_copilot.screening.base import ScreeningInput
from swing_copilot.screening.filter_matrix import (
    CheckKind,
    CheckStats,
    StrategySelection,
    evaluate_filter_matrix,
)
from swing_copilot.universe import UniverseMember
from tests.screening.conftest import (
    FundamentalsSpec,
    make_bars,
    make_fundamentals_row,
    pinned_settings_path,
    pinned_strategies_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.config import Settings
    from swing_copilot.screening.filter_matrix import FilterMatrixResult

_START = date(2026, 1, 1)
_RISING_SESSIONS = 200
_FALLING_SESSIONS = 10
#: The last bar of every fixture series, and therefore the run's `as_of`.
AS_OF = _START + timedelta(days=_RISING_SESSIONS + _FALLING_SESSIONS - 1)

_QUARTERS = (
    (date(2025, 3, 31), datetime(2025, 4, 15, tzinfo=UTC)),
    (date(2025, 6, 30), datetime(2025, 7, 15, tzinfo=UTC)),
    (date(2025, 9, 30), datetime(2025, 10, 15, tzinfo=UTC)),
    (date(2025, 12, 31), datetime(2026, 1, 15, tzinfo=UTC)),
)

FUNDAMENTALS_CHECK = "profitable_positive_fcf_equity"
VOLUME_CHECK = "volume_min"
TREND_CHECK = "trend_sma"
PULLBACK_CHECK = "pullback_rsi"


def _pullback_closes() -> list[float]:
    """A long uptrend with a shallow final dip.

    Rising 0.5/session for 200 sessions then falling 1.0/session for 10 keeps
    `close > SMA200` and `SMA50 > SMA200` (so `trend_sma` hits) while pulling
    RSI(14) down to ~31 and leaving the close ~0.6% from SMA50 (so
    `pullback_rsi` hits its `rsi_threshold` 45 and fixed 3% compatibility band).
    """
    rising = [100.0 + 0.5 * session for session in range(_RISING_SESSIONS)]
    return rising + [
        rising[-1] - 1.0 * (session + 1) for session in range(_FALLING_SESSIONS)
    ]


def _uptrend_closes() -> list[float]:
    """An unbroken uptrend: `trend_sma` hits, RSI stays far above 45."""
    return [
        100.0 + 0.5 * session for session in range(_RISING_SESSIONS + _FALLING_SESSIONS)
    ]


def _member(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        company_name=symbol,
        gics_sector="Information Technology",
        source_symbol=symbol,
    )


def healthy_fundamentals(symbol: str) -> list[dict[str, object]]:
    """Four profitable quarters with positive FCF and a 50% equity ratio."""
    return [
        make_fundamentals_row(
            symbol,
            FundamentalsSpec(
                accession_no=f"{symbol}-{index}",
                fiscal_period_end=period_end,
                filed_at=filed_at,
                net_income=1_000_000.0,
                fcf=500_000.0,
                equity=5_000_000.0,
                assets=10_000_000.0,
            ),
        )
        for index, (period_end, filed_at) in enumerate(_QUARTERS)
    ]


def _screening_input() -> ScreeningInput:
    """Five symbols engineered to land in every bucket of the matrix.

    * `PASSALL` passes every check.
    * `LOWVOL` is blocked only by `volume_min`, on the threshold.
    * `NOFUND` is blocked only by `profitable_positive_fcf_equity`, for lack
      of any filing (a data gap, not a business rejection).
    * `UPTREND` is blocked only by `pullback_rsi`, on the threshold.
    * `NOBARS` has no price history at all, so all three bar-based checks
      report a data gap and it is the only source of co-blocked pairs.
    """
    bars = pd.concat(
        [
            make_bars("PASSALL", _pullback_closes(), start=_START),
            make_bars("LOWVOL", _pullback_closes(), start=_START, volume=500_000),
            make_bars("NOFUND", _pullback_closes(), start=_START),
            make_bars("UPTREND", _uptrend_closes(), start=_START),
        ],
        ignore_index=True,
    )
    fundamentals = pd.DataFrame(
        [
            row
            for symbol in ("PASSALL", "LOWVOL", "UPTREND", "NOBARS")
            for row in healthy_fundamentals(symbol)
        ]
    )
    return ScreeningInput(
        as_of=AS_OF,
        universe=tuple(
            _member(symbol)
            for symbol in ("PASSALL", "LOWVOL", "NOFUND", "UPTREND", "NOBARS")
        ),
        fundamentals=fundamentals,
        bars=bars,
    )


def _strategy(tmp_path: Path, **overrides: object) -> StrategySelection:
    """The real `default` strategy with its check list pinned (and optionally bent)."""
    strategies = load_strategies(str(pinned_strategies_path(tmp_path, **overrides)))
    return StrategySelection(key="default", spec=strategies.strategies["default"])


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Overrides the repo-wide fixture: these tests hand-calculate thresholds."""
    return load_settings(str(pinned_settings_path(tmp_path)))


@pytest.fixture
def default_strategy(tmp_path: Path) -> StrategySelection:
    return _strategy(tmp_path)


@pytest.fixture
def result(
    settings: Settings, default_strategy: StrategySelection
) -> FilterMatrixResult:
    return evaluate_filter_matrix(_screening_input(), settings, default_strategy)


def _stats(result: FilterMatrixResult, name: str) -> CheckStats:
    return next(check for check in result.checks if check.name == name)


class TestEvaluateFilterMatrix:
    def test_checks_follow_configured_filter_then_signal_order(self, result):
        assert [(check.name, check.kind) for check in result.checks] == [
            (FUNDAMENTALS_CHECK, CheckKind.FILTER),
            (VOLUME_CHECK, CheckKind.FILTER),
            (TREND_CHECK, CheckKind.SIGNAL),
            (PULLBACK_CHECK, CheckKind.SIGNAL),
        ]

    def test_each_check_is_counted_against_the_whole_universe(self, result):
        assert result.universe_size == 5
        assert [
            (check.pass_count, check.fail_count, check.no_data_count)
            for check in result.checks
        ] == [
            (4, 0, 1),  # only NOFUND has no filings at all
            (3, 1, 1),  # LOWVOL fails the floor; NOBARS has no bars
            (4, 0, 1),  # NOBARS has no bars
            (3, 1, 1),  # UPTREND's RSI is far above 45; NOBARS has no bars
        ]

    def test_signal_pass_rate_ignores_the_pipeline_filter_narrowing(self, result):
        """An independent rate needs one population for every check.

        `trend_sma` therefore counts LOWVOL/NOFUND, which the real pipeline
        would have filtered out before the signal ever saw them.
        """
        assert _stats(result, TREND_CHECK).pass_rate == pytest.approx(4 / 5)

    def test_data_gaps_are_counted_apart_from_threshold_rejections(self, result):
        assert (_stats(result, FUNDAMENTALS_CHECK).no_data_count) == 1
        assert (_stats(result, FUNDAMENTALS_CHECK).fail_count) == 0
        assert (_stats(result, VOLUME_CHECK).fail_count) == 1

    def test_sole_blocker_counts_symbols_only_that_check_stops(self, result):
        assert {check.name: check.sole_blocker_count for check in result.checks} == {
            FUNDAMENTALS_CHECK: 1,  # NOFUND
            VOLUME_CHECK: 1,  # LOWVOL
            TREND_CHECK: 0,  # NOBARS is also blocked by two other checks
            PULLBACK_CHECK: 1,  # UPTREND
        }

    def test_blocked_count_distribution_partitions_the_universe(self, result):
        assert result.blocked_count_distribution == ((0, 1), (1, 3), (3, 1))
        assert sum(count for _blocked, count in result.blocked_count_distribution) == 5

    def test_co_blocked_matrix_pairs_only_multi_blocked_symbols(self, result):
        assert dict(result.co_blocked_counts) == {
            (VOLUME_CHECK, TREND_CHECK): 1,
            (VOLUME_CHECK, PULLBACK_CHECK): 1,
            (TREND_CHECK, PULLBACK_CHECK): 1,
        }

    def test_unblocked_symbols_are_the_zero_blocked_bucket(self, result):
        assert result.unblocked_symbols == ("PASSALL",)

    def test_as_of_and_strategy_are_carried_through(self, result):
        assert (result.as_of, result.strategy_key) == (AS_OF, "default")


class TestAsOfBoundary:
    """A bar dated exactly `as_of` is visible; the next one is not."""

    @pytest.mark.parametrize(
        ("as_of", "expected_pullback_passes"),
        [
            pytest.param(AS_OF, 3, id="exactly-at-cutoff"),
            pytest.param(AS_OF - timedelta(days=1), 3, id="one-session-before"),
        ],
    )
    def test_visible_history_stops_at_as_of(
        self, settings, default_strategy, as_of, expected_pullback_passes
    ):
        data = _screening_input()
        # The bars frame keeps every session; only `as_of` moves.
        shifted = ScreeningInput(
            as_of=as_of,
            universe=data.universe,
            fundamentals=data.fundamentals,
            bars=data.bars,
        )
        result = evaluate_filter_matrix(shifted, settings, default_strategy)
        assert _stats(result, PULLBACK_CHECK).pass_count == expected_pullback_passes

    def test_a_bar_after_as_of_cannot_rescue_a_symbol(self, settings, default_strategy):
        """Sessions after `as_of` cannot produce a hit.

        Truncating the visible history to just before the dip removes every
        pullback hit the later sessions would have produced.
        """
        data = _screening_input()
        before_dip = ScreeningInput(
            as_of=_START + timedelta(days=_RISING_SESSIONS - 1),
            universe=data.universe,
            fundamentals=data.fundamentals,
            bars=data.bars,
        )
        result = evaluate_filter_matrix(before_dip, settings, default_strategy)
        assert _stats(result, PULLBACK_CHECK).pass_count == 0


class TestFilingVisibilityBoundary:
    """A filing dated exactly `as_of` counts; one dated later does not."""

    @staticmethod
    def _late_filer_input(as_of: date) -> ScreeningInput:
        rows = healthy_fundamentals("LATE")
        rows[-1] = {
            **rows[-1],
            "filed_at": datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC),
        }
        return ScreeningInput(
            as_of=as_of,
            universe=(_member("LATE"),),
            fundamentals=pd.DataFrame(rows),
            bars=make_bars("LATE", _pullback_closes(), start=_START),
        )

    @pytest.mark.parametrize(
        ("as_of", "expected_pass", "expected_no_data"),
        [
            pytest.param(AS_OF, 1, 0, id="filed-exactly-at-as-of"),
            pytest.param(AS_OF - timedelta(days=1), 0, 1, id="filed-after-as-of"),
        ],
    )
    def test_fourth_quarter_filing_boundary_is_inclusive(
        self, settings, default_strategy, as_of, expected_pass, expected_no_data
    ):
        result = evaluate_filter_matrix(
            self._late_filer_input(as_of), settings, default_strategy
        )
        stats = _stats(result, FUNDAMENTALS_CHECK)
        assert (stats.pass_count, stats.no_data_count) == (
            expected_pass,
            expected_no_data,
        )


class TestEmptyUniverse:
    def test_empty_universe_reports_no_rate_instead_of_dividing_by_zero(
        self, settings, default_strategy
    ):
        empty = ScreeningInput(
            as_of=AS_OF,
            universe=(),
            fundamentals=pd.DataFrame(),
            bars=pd.DataFrame(),
        )
        result = evaluate_filter_matrix(empty, settings, default_strategy)
        assert result.universe_size == 0
        assert [check.pass_rate for check in result.checks] == [None] * 4
        assert result.blocked_count_distribution == ()
        assert result.unblocked_symbols == ()


_SHORT_RISING_SESSIONS = 50
#: The last bar of the short-history fixture series.
SHORT_AS_OF = _START + timedelta(days=_SHORT_RISING_SESSIONS + _FALLING_SESSIONS - 1)


def _short_pullback_closes() -> list[float]:
    """`_pullback_closes`'s shape, but too short for an SMA200 to exist."""
    rising = [100.0 + 0.5 * session for session in range(_SHORT_RISING_SESSIONS)]
    return rising + [
        rising[-1] - 1.0 * (session + 1) for session in range(_FALLING_SESSIONS)
    ]


def _short_history_input(*, blank_volume_tail: int = 0) -> ScreeningInput:
    """One symbol that clears every check but has no SMA200 to be ranked on.

    Args:
        blank_volume_tail: Trailing sessions whose volume is NaN. Blanking the
            whole `avg_volume_days` window makes `MinAverageVolumeFilter`
            reject on a NaN mean while its `rejection_classifier` mirror still
            reports a pass -- the disagreement `_filter_outcome` must not
            report as a threshold miss.
    """
    bars = make_bars("SHORT", _short_pullback_closes(), start=_START)
    if blank_volume_tail:
        bars["volume"] = bars["volume"].astype(float)
        bars.loc[bars.index[-blank_volume_tail:], "volume"] = float("nan")
    return ScreeningInput(
        as_of=SHORT_AS_OF,
        universe=(_member("SHORT"),),
        fundamentals=pd.DataFrame(healthy_fundamentals("SHORT")),
        bars=bars,
    )


class TestCandidateEquivalence:
    """The zero-blocked bucket is not the candidate set; the pipeline gates more."""

    def test_zero_blocked_symbols_are_candidate_equivalent_when_rankable(self, result):
        assert result.unblocked_symbols == ("PASSALL",)
        assert result.candidate_equivalent_symbols == ("PASSALL",)

    def test_a_symbol_without_ranking_metrics_is_excluded(self, settings, tmp_path):
        strategy = _strategy(tmp_path, signals_all=["pullback_rsi"])

        result = evaluate_filter_matrix(_short_history_input(), settings, strategy)

        # It passes every configured check, but `ScreeningPipeline` drops it
        # for a NaN SMA200 and the ledger records it as a data-quality
        # rejection, so it must not be reported as a candidate.
        assert result.unblocked_symbols == ("SHORT",)
        assert result.candidate_equivalent_symbols == ()

    def test_a_strategy_without_signals_produces_no_candidates(
        self, settings, tmp_path
    ):
        strategy = _strategy(tmp_path, signals_all=[])

        result = evaluate_filter_matrix(_screening_input(), settings, strategy)

        assert result.unblocked_symbols == ("PASSALL", "UPTREND")
        assert result.candidate_equivalent_symbols == ()


class TestDuplicateCheckKeys:
    def test_a_repeated_filter_key_is_measured_exactly_once(self, settings, tmp_path):
        strategy = _strategy(
            tmp_path,
            filters_all=["volume_min", "profitable_positive_fcf_equity", "volume_min"],
        )

        result = evaluate_filter_matrix(_screening_input(), settings, strategy)

        assert [check.name for check in result.checks] == [
            VOLUME_CHECK,
            FUNDAMENTALS_CHECK,
            TREND_CHECK,
            PULLBACK_CHECK,
        ]
        # Measuring `volume_min` twice would push LOWVOL into the 2-blocked
        # bucket and stop it counting as anybody's sole blocker.
        assert _stats(result, VOLUME_CHECK).sole_blocker_count == 1
        assert result.blocked_count_distribution == ((0, 1), (1, 3), (3, 1))

    def test_a_repeated_signal_key_is_measured_exactly_once(self, settings, tmp_path):
        strategy = _strategy(
            tmp_path, signals_all=["pullback_rsi", "trend_sma", "pullback_rsi"]
        )

        result = evaluate_filter_matrix(_screening_input(), settings, strategy)

        assert [check.name for check in result.checks] == [
            FUNDAMENTALS_CHECK,
            VOLUME_CHECK,
            PULLBACK_CHECK,
            TREND_CHECK,
        ]
        assert _stats(result, PULLBACK_CHECK).sole_blocker_count == 1


class TestPopulationDependentChecks:
    def test_the_default_strategy_flags_nothing(self, result):
        assert result.population_dependent_checks == ()

    def test_minervini_is_flagged_as_not_comparable_to_the_daily_run(
        self, settings, tmp_path
    ):
        strategy = _strategy(tmp_path, signals_all=["minervini_stage2"])

        result = evaluate_filter_matrix(_screening_input(), settings, strategy)

        assert result.population_dependent_checks == ("minervini_stage2",)


class TestClassifierDisagreement:
    def test_a_filter_its_mirror_disagrees_with_counts_as_a_data_gap(
        self, settings, tmp_path
    ):
        strategy = _strategy(
            tmp_path, filters_all=["volume_min"], signals_all=["pullback_rsi"]
        )

        result = evaluate_filter_matrix(
            _short_history_input(blank_volume_tail=25), settings, strategy
        )

        stats = _stats(result, VOLUME_CHECK)
        assert (stats.pass_count, stats.fail_count, stats.no_data_count) == (0, 0, 1)
