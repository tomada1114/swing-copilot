"""P8-30: `copilot-retro evaluate` -- maturity semantics and verdict classification.

Two contracts dominate here:

* Maturity, not observation. A `(run, horizon)` is evaluated only once its
  maturity session is on or before `as_of`, and the maturity session is what
  lands in `verdict_outcomes.as_of` -- so re-running the batch on any later
  day reproduces the same row (design §5.2, decision D7).
* Asymmetric classification. `proceed` claims "no severe adverse move", so it
  has no NEUTRAL bucket; `skip` claims "avoiding a decline", so an upside move
  is a (milder) miss. The boundary cases below pin every threshold edge in
  design §3.3.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from swing_copilot.config import PostmortemConfig
from swing_copilot.retro.evaluate import (
    HIT,
    MISS_MILD,
    MISS_SEVERE,
    NEUTRAL,
    EvaluateSummary,
    classify_verdict_outcome,
    evaluate_verdicts,
)
from swing_copilot.storage.verdict_records import VerdictRecord
from tests.retro.conftest import bars

if TYPE_CHECKING:
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore

BENCHMARK = "SPY"
RUN_DATE = date(2027, 3, 1)
CALENDAR = [RUN_DATE + timedelta(days=offset) for offset in range(40)]
MATURITY_5D = CALENDAR[5]
MATURITY_20D = CALENDAR[20]


# --- classify_verdict_outcome boundaries ------------------------------------


@pytest.mark.parametrize(
    ("forward_return_pct", "expected"),
    [
        pytest.param(0.5, HIT, id="clearly-up"),
        pytest.param(-0.499, HIT, id="just-inside-noise"),
        pytest.param(-0.5, MISS_MILD, id="exactly-at-noise-boundary"),
        pytest.param(-0.501, MISS_MILD, id="just-past-noise-boundary"),
        pytest.param(-1.999, MISS_MILD, id="just-inside-severe"),
        pytest.param(-2.0, MISS_SEVERE, id="exactly-at-severe-boundary"),
        pytest.param(-2.001, MISS_SEVERE, id="just-past-severe-boundary"),
    ],
)
def test_proceed_classification_boundaries(
    forward_return_pct: float, expected: str
) -> None:
    # `proceed` is a one-sided claim ("no severe adverse move"), so every
    # non-adverse return is a HIT and there is deliberately no NEUTRAL.
    assert classify_verdict_outcome("proceed", forward_return_pct) == expected


@pytest.mark.parametrize(
    ("forward_return_pct", "expected"),
    [
        pytest.param(-2.5, HIT, id="large-decline-avoided"),
        pytest.param(-0.501, HIT, id="just-past-noise-boundary"),
        pytest.param(-0.5, HIT, id="exactly-at-negative-noise-boundary"),
        pytest.param(-0.499, NEUTRAL, id="just-inside-noise"),
        pytest.param(0.0, NEUTRAL, id="flat"),
        pytest.param(0.499, NEUTRAL, id="just-inside-positive-noise"),
        pytest.param(0.5, MISS_MILD, id="exactly-at-positive-noise-boundary"),
        pytest.param(1.999, MISS_MILD, id="just-inside-severe"),
        pytest.param(2.0, MISS_SEVERE, id="exactly-at-severe-boundary"),
        pytest.param(2.001, MISS_SEVERE, id="just-past-severe-boundary"),
    ],
)
def test_skip_classification_boundaries(
    forward_return_pct: float, expected: str
) -> None:
    assert classify_verdict_outcome("skip", forward_return_pct) == expected


def test_classification_honours_configured_thresholds() -> None:
    # The thresholds come from `settings.postmortem`; no new threshold set is
    # introduced for verdicts (decision D6).
    assert (
        classify_verdict_outcome(
            "proceed", -0.8, neutral_threshold_pct=1.0, severe_threshold_pct=3.0
        )
        == HIT
    )


# --- evaluate_verdicts -------------------------------------------------------


def _seed_calendar(market_store: MarketStore) -> None:
    market_store.write_bars(bars(BENCHMARK, dict.fromkeys(CALENDAR, 100.0)))


def _seed_verdict(
    state_store: StateStore,
    run_id: UUID,
    symbol: str = "AAPL",
    recommendation: str = "proceed",
    *,
    run_date: date = RUN_DATE,
) -> None:
    state_store.replace_run_verdicts(
        run_id,
        [
            VerdictRecord(
                run_id=run_id,
                symbol=symbol,
                as_of=run_date,
                strategy_key="default",
                recommendation=recommendation,
                reasons=(),
                no_trade=False,
            )
        ],
        [],
    )


def _outcome_rows(
    state_store: StateStore, horizon_days: int | None = None
) -> list[tuple[object, ...]]:
    sql = (
        "SELECT symbol, horizon_days, as_of, recommendation, forward_return_pct, "
        "classification FROM verdict_outcomes"
    )
    parameters: list[object] = []
    if horizon_days is not None:
        sql += " WHERE horizon_days = ?"
        parameters.append(horizon_days)
    with state_store._database.connect() as conn:  # noqa: SLF001
        return conn.execute(
            sql + " ORDER BY symbol, horizon_days", parameters
        ).fetchall()


def _evaluate(
    market_store: MarketStore, state_store: StateStore, as_of: date
) -> EvaluateSummary:
    return evaluate_verdicts(
        market_store, state_store, as_of, PostmortemConfig(), BENCHMARK
    )


class TestEvaluateHappyPath:
    def test_records_the_maturity_session_as_the_outcome_as_of(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        # 100 -> 101.5 over the 5 sessions to maturity: +1.5%.
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _outcome_rows(state_store, 5) == [
            ("AAPL", 5, MATURITY_5D, "proceed", pytest.approx(1.5), HIT)
        ]

    def test_evaluates_each_matured_horizon_separately(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(
            bars(
                "AAPL",
                {RUN_DATE: 100.0, MATURITY_5D: 101.5, MATURITY_20D: 97.0},
            )
        )

        summary = evaluate_verdicts(
            market_store, state_store, CALENDAR[25], PostmortemConfig(), BENCHMARK
        )

        assert (summary.evaluated_slice_count, summary.outcome_count) == (2, 2)
        assert _outcome_rows(state_store) == [
            ("AAPL", 5, MATURITY_5D, "proceed", pytest.approx(1.5), HIT),
            ("AAPL", 20, MATURITY_20D, "proceed", pytest.approx(-3.0), MISS_SEVERE),
        ]

    def test_classifies_a_skip_verdict_with_the_asymmetric_table(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id, recommendation="skip")
        # A +3% move the skip missed out on: a severe opportunity-cost miss.
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 103.0}))

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _outcome_rows(state_store, 5) == [
            ("AAPL", 5, MATURITY_5D, "skip", pytest.approx(3.0), MISS_SEVERE)
        ]


class TestEvaluateBenchmarkReturn:
    """Issue #190: each classification records the market's own move too."""

    def test_records_the_benchmarks_return_over_the_identical_span(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        # Benchmark 100 -> 102 over the same 5 sessions: +2.0%. The symbol
        # gains 1.5%, so it actually *lagged* -- the reading the raw
        # forward return alone cannot give.
        market_store.write_bars(bars(BENCHMARK, {MATURITY_5D: 102.0}))
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _benchmark_returns(state_store) == [pytest.approx(2.0)]

    def test_a_missing_benchmark_close_is_recorded_as_unmeasured_not_zero(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # The trading calendar comes from the benchmark, so its maturity bar
        # exists; what is missing here is the *run day's* close, which is what
        # a return needs. NULL must not be read later as "the market was flat".
        run_id = uuid4()
        market_store.write_bars(
            bars(BENCHMARK, dict.fromkeys(CALENDAR, 100.0) | {RUN_DATE: float("nan")})
        )
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _benchmark_returns(state_store) == [None]


def _benchmark_returns(state_store: StateStore) -> list[float | None]:
    with state_store._database.connect() as conn:  # noqa: SLF001
        return [
            row[0]
            for row in conn.execute(
                "SELECT benchmark_return_pct FROM verdict_outcomes "
                "WHERE horizon_days = 5 ORDER BY symbol"
            ).fetchall()
        ]


class TestEvaluateMaturityCutoff:
    def test_a_horizon_maturing_exactly_at_as_of_is_evaluated(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        _evaluate(market_store, state_store, MATURITY_5D)

        assert len(_outcome_rows(state_store, 5)) == 1

    def test_a_horizon_maturing_one_session_after_as_of_is_pending(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        summary = evaluate_verdicts(
            market_store,
            state_store,
            MATURITY_5D - timedelta(days=1),
            PostmortemConfig(),
            BENCHMARK,
        )

        assert summary.pending_slice_count == 2
        assert _outcome_rows(state_store) == []

    def test_prices_after_the_maturity_session_never_change_the_return(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(
            bars(
                "AAPL",
                {
                    RUN_DATE: 100.0,
                    MATURITY_5D: 101.5,
                    # Later sessions are visible at `as_of` but are outside the
                    # 5-day horizon, so they must not leak into its return.
                    CALENDAR[10]: 999_999.0,
                },
            )
        )

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _outcome_rows(state_store, 5)[0][4] == pytest.approx(1.5)


class TestEvaluateFailSoft:
    def test_a_symbol_without_bars_is_skipped_with_a_note(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        state_store.replace_run_verdicts(
            run_id,
            [
                VerdictRecord(
                    run_id=run_id,
                    symbol=symbol,
                    as_of=RUN_DATE,
                    strategy_key="default",
                    recommendation="proceed",
                    reasons=(),
                    no_trade=False,
                )
                for symbol in ("AAPL", "MISSING")
            ],
            [],
        )
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        summary = evaluate_verdicts(
            market_store, state_store, CALENDAR[10], PostmortemConfig(), BENCHMARK
        )

        assert [row[0] for row in _outcome_rows(state_store, 5)] == ["AAPL"]
        assert any("MISSING" in note for note in summary.notes)

    def test_a_run_whose_benchmark_calendar_is_absent_produces_no_rows(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        summary = evaluate_verdicts(
            market_store, state_store, CALENDAR[10], PostmortemConfig(), BENCHMARK
        )

        assert (summary.evaluated_slice_count, summary.outcome_count) == (0, 0)
        assert _outcome_rows(state_store) == []

    def test_an_empty_database_is_a_normal_success(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        summary = evaluate_verdicts(
            market_store, state_store, CALENDAR[10], PostmortemConfig(), BENCHMARK
        )

        assert (summary.evaluated_slice_count, summary.notes) == (0, ())


class TestEvaluateIdempotence:
    def test_rerunning_on_a_later_day_reproduces_the_same_row(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        _evaluate(market_store, state_store, CALENDAR[10])
        first = _outcome_rows(state_store, 5)
        _evaluate(market_store, state_store, CALENDAR[15])

        assert _outcome_rows(state_store, 5) == first

    def test_corrected_prices_update_the_classification_in_place(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))
        _evaluate(market_store, state_store, CALENDAR[10])

        # The maturity close is revised down sharply.
        market_store.write_bars(bars("AAPL", {MATURITY_5D: 95.0}))
        _evaluate(market_store, state_store, CALENDAR[10])

        rows = _outcome_rows(state_store, 5)
        assert len(rows) == 1
        assert (rows[0][4], rows[0][5]) == (pytest.approx(-5.0), MISS_SEVERE)


class TestEvaluateWindow:
    def test_a_run_older_than_the_lookback_window_is_not_evaluated(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        thresholds = PostmortemConfig()
        as_of = CALENDAR[10]
        stale_run_date = as_of - timedelta(days=thresholds.lookback_window_days + 31)
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id, run_date=stale_run_date)

        summary = evaluate_verdicts(
            market_store, state_store, as_of, thresholds, BENCHMARK
        )

        assert summary.evaluated_slice_count == 0
        assert _outcome_rows(state_store) == []

    def test_a_run_at_the_window_boundary_is_evaluated(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        thresholds = PostmortemConfig()
        as_of = CALENDAR[10]
        boundary_run_date = as_of - timedelta(days=thresholds.lookback_window_days + 30)
        calendar = [boundary_run_date + timedelta(days=offset) for offset in range(160)]
        market_store.write_bars(bars(BENCHMARK, dict.fromkeys(calendar, 100.0)))
        run_id = uuid4()
        _seed_verdict(state_store, run_id, run_date=boundary_run_date)
        market_store.write_bars(
            bars(
                "AAPL",
                {
                    boundary_run_date: 100.0,
                    boundary_run_date + timedelta(days=5): 101.5,
                },
            )
        )

        summary = evaluate_verdicts(
            market_store, state_store, as_of, thresholds, BENCHMARK
        )

        assert summary.outcome_count >= 1
