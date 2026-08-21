"""Contract tests for `regime/dd_forward.py`.

The load-bearing one is `test_every_observation_matches_the_daily_run`: the
diagnostic is only worth reading if its classification is byte-for-byte the
daily run's, so the replay is checked against `calculate_regime_snapshot` on
every date rather than spot-checked.
"""

from __future__ import annotations

from datetime import date, timedelta
from math import isnan

import pandas as pd
import pytest

from swing_copilot.regime.dd_forward import (
    _TRAILING_ROWS,
    QQQ_TARGET,
    SPY_TARGET,
    UNIVERSE_TARGET,
    ForwardScanRequest,
    IndexCounts,
    level_series,
    scan_forward,
    summarise_level,
    summarise_levels,
)
from swing_copilot.regime.distribution import (
    DistributionLevel,
    DistributionThresholds,
    calculate_distribution_days,
    distribution_severity,
)
from swing_copilot.regime.gate import (
    DEFAULT_REGIME_THRESHOLDS,
    GateThresholds,
    RegimeThresholds,
    calculate_regime_snapshot,
)
from tests.regime.conftest import bars_for, market_bars, sawtooth

_HORIZONS = (2, 5)
_TEST_REGIME_THRESHOLDS = RegimeThresholds(
    gate=GateThresholds(
        # The production gate is SMA200. These compact fixtures intentionally
        # use a short window so the forward scanner can exercise many dates
        # without manufacturing 200 rows of unrelated history.
        sma_period=5,
        bear_spy_sma_ratio=DEFAULT_REGIME_THRESHOLDS.gate.bear_spy_sma_ratio,
        bear_vix_min=DEFAULT_REGIME_THRESHOLDS.gate.bear_vix_min,
    ),
    distribution=DEFAULT_REGIME_THRESHOLDS.distribution,
    ftd=DEFAULT_REGIME_THRESHOLDS.ftd,
)


def _request(bars: pd.DataFrame, **overrides: object) -> ForwardScanRequest:
    defaults: dict[str, object] = {
        "bars": bars,
        "start": date.min,
        "as_of": max(bars["date"]),
        "thresholds": _TEST_REGIME_THRESHOLDS,
        "horizons": _HORIZONS,
    }
    defaults.update(overrides)
    return ForwardScanRequest(**defaults)  # type: ignore[arg-type]


def test_every_observation_matches_the_daily_run() -> None:
    """Replayed counts, gate, and composite level equal `_calculate_regime_snapshot`.

    Iterating every date rather than sampling is deliberate: a trimming or
    warm-up bug would show up on a handful of dates only.
    """
    bars = market_bars()
    scan = scan_forward(_request(bars))
    assert scan.observations

    for observation in scan.observations:
        as_of = observation.as_of
        expected = calculate_regime_snapshot(
            bars.loc[bars["symbol"] == SPY_TARGET],
            bars.loc[bars["symbol"] == QQQ_TARGET],
            bars.loc[bars["symbol"] == "^VIX"],
            as_of,
            thresholds=_TEST_REGIME_THRESHOLDS,
        )
        assert observation.gate is expected.gate.verdict, as_of
        assert observation.spy == IndexCounts(
            expected.spy_distribution.d25,
            expected.spy_distribution.d15,
            expected.spy_distribution.d5,
        ), as_of
        assert observation.qqq == IndexCounts(
            expected.qqq_distribution.d25,
            expected.qqq_distribution.d15,
            expected.qqq_distribution.d5,
        ), as_of
        assert (
            observation.level(_TEST_REGIME_THRESHOLDS.distribution) is expected.dd_level
        ), as_of


def test_trailing_slice_is_exact_for_the_counter() -> None:
    """`_TRAILING_ROWS` history yields the same counts as the full history.

    `scan_forward` trims to keep the replay linear. The trim is only sound
    because a day expires after `window_days` and `count()` never reaches
    further back, so this pins the assumption rather than trusting the comment.
    """
    closes = sawtooth(200)
    bars = bars_for(SPY_TARGET, closes)
    thresholds = DistributionThresholds()

    for index in range(_TRAILING_ROWS, len(closes)):
        as_of = bars.iloc[index]["date"]
        full = calculate_distribution_days(
            bars.iloc[: index + 1], as_of, thresholds=thresholds
        )
        trimmed = calculate_distribution_days(
            bars.iloc[index - _TRAILING_ROWS : index + 1], as_of, thresholds=thresholds
        )
        assert (full.d25, full.d15, full.d5, full.level) == (
            trimmed.d25,
            trimmed.d15,
            trimmed.d5,
            trimmed.level,
        ), as_of


def test_bars_after_as_of_change_nothing() -> None:
    """The inclusive `date <= as_of` boundary holds for classification.

    The row exactly at `as_of` must be used; rows after it must not exist for
    any purpose, including the forward outcomes.
    """
    bars = market_bars(140)
    cutoff = sorted(bars["date"].unique())[110]
    truncated = bars.loc[bars["date"] <= cutoff]

    full_scan = scan_forward(_request(bars, as_of=cutoff))
    truncated_scan = scan_forward(_request(truncated, as_of=cutoff))
    assert full_scan.observations == truncated_scan.observations


def test_forward_outcomes_are_close_to_close_and_clamped() -> None:
    """Return and drawdown are hand-checkable, and a rising window has no drawdown."""
    closes = [*sawtooth(120), 100.0, 90.0, 95.0, 110.0]
    bars = pd.concat(
        [
            bars_for(SPY_TARGET, closes),
            bars_for(QQQ_TARGET, closes),
            bars_for("^VIX", [15.0] * len(closes)),
        ],
        ignore_index=True,
    )
    scan = scan_forward(_request(bars, horizons=(3,)))
    entry = next(
        observation
        for observation in scan.observations
        if observation.as_of == bars.iloc[119]["date"]
    )
    outcome = entry.outcome(SPY_TARGET, 3)
    assert outcome is not None
    # Close at index 119 is the entry; the window is 100.0, 90.0, 95.0.
    base = closes[119]
    assert outcome.total_return == pytest.approx(95.0 / base - 1.0)
    assert outcome.max_drawdown == pytest.approx(min(0.0, 90.0 / base - 1.0))

    rising = next(
        observation
        for observation in scan.observations
        if observation.as_of == bars.iloc[120]["date"]
    )
    rising_outcome = rising.outcome(SPY_TARGET, 3)
    assert rising_outcome is not None
    assert rising_outcome.max_drawdown == pytest.approx(min(0.0, 90.0 / 100.0 - 1.0))


def test_windows_running_off_the_end_are_absent_not_zero() -> None:
    """The last `horizon` observations carry no outcome for that horizon."""
    scan = scan_forward(_request(market_bars(), horizons=(5,)))
    tail = scan.observations[-5:]
    assert all(observation.outcome(SPY_TARGET, 5) is None for observation in tail)
    assert scan.observations[-6].outcome(SPY_TARGET, 5) is not None


def test_equal_weight_basket_chains_member_returns() -> None:
    """Two members with known returns produce the arithmetic-mean index level."""
    length = 130
    base = market_bars(length)
    up = bars_for("AAA", [100.0 * (1.02**index) for index in range(length)])
    flat = bars_for("BBB", [50.0] * length)
    scan = scan_forward(_request(pd.concat([base, up, flat], ignore_index=True)))

    assert scan.universe_symbols == 2
    assert UNIVERSE_TARGET in scan.targets
    observation = scan.observations[0]
    outcome = observation.outcome(UNIVERSE_TARGET, 2)
    assert outcome is not None
    # Each day the basket returns mean(+2%, 0%) = +1%, chained over 2 days.
    assert outcome.total_return == pytest.approx(1.01**2 - 1.0)


def test_a_member_joining_mid_history_does_not_print_a_jump() -> None:
    """A symbol whose bars start late contributes only from its second bar."""
    length = 130
    base = market_bars(length)
    early = bars_for("AAA", [100.0] * length)
    late_start = date(2027, 1, 1) + timedelta(days=length - 10)
    late = bars_for("BBB", [1_000.0] * 10, start=late_start)
    scan = scan_forward(_request(pd.concat([base, early, late], ignore_index=True)))

    outcomes = [
        outcome
        for observation in scan.observations
        for outcome in observation.outcomes
        if outcome.target == UNIVERSE_TARGET
    ]
    assert outcomes
    assert all(abs(outcome.total_return) < 0.01 for outcome in outcomes)


def test_missing_index_bars_are_an_explicit_error() -> None:
    """A scan without both indices cannot produce the daily run's composite."""
    spy_only = market_bars()
    spy_only = spy_only.loc[spy_only["symbol"] != QQQ_TARGET]
    with pytest.raises(ValueError, match="QQQ"):
        scan_forward(_request(spy_only))


def test_warmup_dates_are_counted_not_silently_dropped() -> None:
    """Dates without a full window or a seeded SMA are reported, never hidden."""
    bars = market_bars()
    scan = scan_forward(_request(bars))
    trading_days = bars.loc[bars["symbol"] == SPY_TARGET]
    assert len(scan.observations) + scan.warmup_skipped == len(trading_days)
    assert scan.warmup_skipped > 0


def test_summarise_level_reports_absent_buckets_as_none() -> None:
    """An empty level is `None`, never a zero-valued row."""
    scan = scan_forward(_request(market_bars()))
    thresholds = DEFAULT_REGIME_THRESHOLDS.distribution
    levels = level_series(scan, thresholds)
    absent = next(
        level
        for level in DistributionLevel
        if level is not DistributionLevel.UNKNOWN and level not in levels
    )
    assert summarise_level(scan, levels, absent, (SPY_TARGET, 2)) is None


def test_summarise_levels_orders_strictest_first_and_counts_episodes() -> None:
    """Rows come back in severity order, with runs counted separately from days."""
    scan = scan_forward(_request(market_bars()))
    stats = summarise_levels(
        scan, DEFAULT_REGIME_THRESHOLDS.distribution, (SPY_TARGET, 2)
    )
    assert stats
    severities = [distribution_severity(entry.level) for entry in stats]
    assert severities == sorted(severities, reverse=True)
    for entry in stats:
        assert 1 <= entry.episode_count <= entry.sample_size


def test_level_series_reclassifies_without_recounting() -> None:
    """Moving only the boundaries changes levels, never the underlying counts."""
    scan = scan_forward(_request(market_bars()))
    strict = DistributionThresholds(severe_d25=1, severe_d15=1, high_d25=1, high_d15=1)
    loose = DistributionThresholds(
        severe_d25=99, severe_d15=99, high_d25=98, high_d15=98, high_d5=99
    )
    assert set(level_series(scan, strict)) == {DistributionLevel.SEVERE}
    assert set(level_series(scan, loose)) <= {
        DistributionLevel.NORMAL,
        DistributionLevel.CAUTION,
    }


def test_scan_targets_omit_the_basket_when_only_indices_are_stored() -> None:
    """No universe members means no basket target, not an all-zero one."""
    scan = scan_forward(_request(market_bars()))
    assert scan.universe_symbols == 0
    assert scan.targets == (SPY_TARGET, QQQ_TARGET)
    assert all(
        outcome.target != UNIVERSE_TARGET
        for observation in scan.observations
        for outcome in observation.outcomes
    )


def test_thresholds_flow_through_to_the_counts() -> None:
    """A different counting rule really does change the replayed counts."""
    bars = market_bars()
    lenient = RegimeThresholds(
        gate=_TEST_REGIME_THRESHOLDS.gate,
        distribution=DistributionThresholds(dd_decline_pct=-0.5),
    )
    baseline = scan_forward(_request(bars))
    changed = scan_forward(_request(bars, thresholds=lenient))
    assert all(observation.spy.d25 == 0.0 for observation in changed.observations)
    assert any(observation.spy.d25 > 0.0 for observation in baseline.observations)


def test_a_gap_in_a_target_omits_that_horizon_instead_of_emitting_nan() -> None:
    """A missing bar leaves `NaN` after the calendar reindex; that horizon is dropped.

    `NaN` propagates silently through `fmean`, so one gap would poison a whole
    level's average. Omitting is the only outcome that stays honest.
    """
    length = 130
    bars = market_bars(length)
    gap_date = sorted(bars["date"].unique())[120]
    holed = bars.loc[~((bars["symbol"] == QQQ_TARGET) & (bars["date"] == gap_date))]
    scan = scan_forward(_request(holed, horizons=(2,)))

    returns = [
        outcome.total_return
        for observation in scan.observations
        for outcome in observation.outcomes
    ]
    assert returns
    assert not any(isnan(value) for value in returns)
    # The two observations whose window spans the hole lose only the QQQ leg.
    affected = [
        observation
        for observation in scan.observations
        if observation.outcome(QQQ_TARGET, 2) is None
        and observation.outcome(SPY_TARGET, 2) is not None
    ]
    assert affected


def test_an_interior_gap_omits_the_horizon_even_when_its_last_bar_exists() -> None:
    """A hole mid-window drops the horizon; a surviving endpoint is not enough.

    `Series.min` skips `NaN`, so an interior gap would otherwise report a
    drawdown searched over fewer bars than the horizon claims to cover.
    """
    bars = market_bars(130)
    dates = sorted(bars["date"].unique())
    holed = bars.loc[~((bars["symbol"] == QQQ_TARGET) & (bars["date"] == dates[121]))]
    scan = scan_forward(_request(holed, horizons=(3,)))

    entry = next(
        observation
        for observation in scan.observations
        if observation.as_of == dates[119]
    )
    # The window is dates[120..122]: its endpoint survives, its middle bar does not.
    assert entry.outcome(SPY_TARGET, 3) is not None
    assert entry.outcome(QQQ_TARGET, 3) is None


def test_forward_outcome_target_is_absent_when_its_close_is_zero_or_negative() -> None:
    """A collapsed-to-zero close can never become a division base for its own target.

    Other targets at the same observation are unaffected -- the guard is
    per-series, not per-observation.
    """
    bars = market_bars(140)
    zero_date = sorted(bars.loc[bars["symbol"] == SPY_TARGET, "date"].unique())[100]
    zeroed = bars.copy()
    mask = (zeroed["symbol"] == SPY_TARGET) & (zeroed["date"] == zero_date)
    zeroed.loc[mask, "close"] = 0.0

    scan = scan_forward(_request(zeroed, horizons=(2,)))
    entry = next(
        observation
        for observation in scan.observations
        if observation.as_of == zero_date
    )
    assert entry.outcome(SPY_TARGET, 2) is None
    assert entry.outcome(QQQ_TARGET, 2) is not None


def test_a_short_sma_with_an_unfilled_distribution_window_is_still_warmup() -> None:
    """A shorter SMA period than the distribution window still counts as warm-up.

    `_TRAILING_ROWS`-bounded counting can return `UNKNOWN` (insufficient rows)
    even after the gate's own SMA has seeded, when `window_days` outlasts
    `sma_period`. That gap must stay silent warm-up, never a crash or a
    stray `Observation`.
    """
    bars = market_bars(140)
    thresholds = RegimeThresholds(
        gate=GateThresholds(sma_period=5),
        distribution=DistributionThresholds(window_days=60),
    )
    scan = scan_forward(_request(bars, thresholds=thresholds, horizons=(5,)))
    trading_days = bars.loc[bars["symbol"] == SPY_TARGET]
    dates = sorted(trading_days["date"].unique())

    assert len(scan.observations) + scan.warmup_skipped == len(trading_days)
    observed_dates = {observation.as_of for observation in scan.observations}
    # Index 30: the SMA is seeded (needs 5 rows) but window_days=60 needs 61.
    assert dates[30] not in observed_dates
    # Index 60: the 61st row finally fills the distribution window.
    assert dates[60] in observed_dates
