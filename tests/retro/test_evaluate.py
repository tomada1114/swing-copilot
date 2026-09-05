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

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pandas as pd
import pytest

from swing_copilot.config import PostmortemConfig
from swing_copilot.retro.evaluate import (
    HIT,
    MISS_MILD,
    MISS_SEVERE,
    NEUTRAL,
    EvaluateSummary,
    EvaluationRequest,
    classify_verdict_outcome,
    evaluate_verdicts,
)
from swing_copilot.storage.verdict_records import VerdictRecord
from tests.conftest import plant_non_finite_bars
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
    with state_store.database.connect() as conn:
        return conn.execute(
            sql + " ORDER BY symbol, horizon_days", parameters
        ).fetchall()


def _evaluate(
    market_store: MarketStore, state_store: StateStore, as_of: date
) -> EvaluateSummary:
    return evaluate_verdicts(
        market_store,
        state_store,
        EvaluationRequest(
            as_of=as_of, thresholds=PostmortemConfig(), benchmark_symbol=BENCHMARK
        ),
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
            market_store,
            state_store,
            EvaluationRequest(
                as_of=CALENDAR[25],
                thresholds=PostmortemConfig(),
                benchmark_symbol=BENCHMARK,
            ),
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
        _seed_verdict(state_store, run_id)
        # Benchmark 100 -> 102 over the same 5 sessions: +2.0%. The symbol
        # gains 1.5%, so it actually *lagged* -- the reading the raw
        # forward return alone cannot give. The calendar carries the maturity
        # close from the start rather than overwriting a stored 100.0: a 2%
        # revision is past what `write_bars` accepts as a correction, and it
        # would quarantine the benchmark outright (Issue #413).
        market_store.write_bars(
            bars(BENCHMARK, dict.fromkeys(CALENDAR, 100.0) | {MATURITY_5D: 102.0})
        )
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
        # Planted past `write_bars`' finite guard (Issue #227): a non-finite
        # close can only reach storage as history written before that guard.
        plant_non_finite_bars(
            market_store,
            bars(BENCHMARK, dict.fromkeys(CALENDAR, 100.0) | {RUN_DATE: float("nan")}),
        )
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _benchmark_returns(state_store) == [None]


def _benchmark_returns(state_store: StateStore) -> list[float | None]:
    with state_store.database.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT benchmark_return_pct FROM verdict_outcomes "
                "WHERE horizon_days = 5 ORDER BY symbol"
            ).fetchall()
        ]


def _audit_closes(state_store: StateStore) -> list[tuple[object, object]]:
    with state_store.database.connect() as conn:
        return [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT entry_close, maturity_close FROM verdict_outcomes "
                "WHERE horizon_days = 5 ORDER BY symbol"
            ).fetchall()
        ]


class TestEvaluateAuditCloses:
    """Issue #413: which prices the classification was actually computed at."""

    def test_it_records_both_closes_behind_the_classification(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _audit_closes(state_store) == [
            (pytest.approx(100.0), pytest.approx(101.5))
        ]

    def test_a_split_inside_the_horizon_records_the_maturity_basis_not_the_raw_close(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # The as-traded run-day close is 97.23; the 2:1 split before maturity
        # rebases it to 48.615, which is the number the ratio divided. Storing
        # 97.23 here would make the audit pair contradict its own return --
        # the exact confusion the columns exist to settle (MNST, Issue #413).
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id, "MNST")
        market_store.write_bars(bars("MNST", {RUN_DATE: 97.23, MATURITY_5D: 47.81}))
        market_store.write_corporate_actions(
            pd.DataFrame(
                {
                    "symbol": ["MNST"],
                    "ex_date": [CALENDAR[3]],
                    "kind": ["split"],
                    "value": [2.0],
                }
            ),
            provider="test",
            fetched_at=datetime(2027, 3, 1, tzinfo=UTC),
        )

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _audit_closes(state_store) == [
            (pytest.approx(48.615), pytest.approx(47.81))
        ]

    def test_a_skipped_symbol_writes_no_row_at_all(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # The columns never stand in for a missing measurement: a symbol whose
        # closes are not both available is skipped outright, as before.
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _audit_closes(state_store) == []


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
            EvaluationRequest(
                as_of=MATURITY_5D - timedelta(days=1),
                thresholds=PostmortemConfig(),
                benchmark_symbol=BENCHMARK,
            ),
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
            market_store,
            state_store,
            EvaluationRequest(
                as_of=CALENDAR[10],
                thresholds=PostmortemConfig(),
                benchmark_symbol=BENCHMARK,
            ),
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
            market_store,
            state_store,
            EvaluationRequest(
                as_of=CALENDAR[10],
                thresholds=PostmortemConfig(),
                benchmark_symbol=BENCHMARK,
            ),
        )

        assert (summary.evaluated_slice_count, summary.outcome_count) == (0, 0)
        assert _outcome_rows(state_store) == []

    def test_an_empty_database_is_a_normal_success(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        summary = evaluate_verdicts(
            market_store,
            state_store,
            EvaluationRequest(
                as_of=CALENDAR[10],
                thresholds=PostmortemConfig(),
                benchmark_symbol=BENCHMARK,
            ),
        )

        assert (summary.evaluated_slice_count, summary.notes) == (0, ())


class TestEvaluatePreservesUnrecomputableRows:
    """Issue #424: a slice replace must correct a row, never quietly delete it.

    `copilot-backfill rebuild` can make a provider re-fetch drop a historical
    bar that used to be there (the MNST 2026-08-10 case in the issue). Before
    this fix, `_evaluate_slice` simply omitted the now-unrecomputable symbol
    from `outcomes`, and the full-slice `replace_verdict_outcomes` call then
    deleted its previously recorded row along with everything else -- a
    contaminated (or perfectly valid) outcome vanished instead of being
    corrected.
    """

    def test_a_row_whose_maturity_bar_later_disappears_is_kept_not_deleted(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))
        _evaluate(market_store, state_store, CALENDAR[10])
        first = _outcome_rows(state_store, 5)
        assert first == [("AAPL", 5, MATURITY_5D, "proceed", pytest.approx(1.5), HIT)]

        # The provider re-fetch drops the maturity-day bar entirely -- the
        # store no longer has anything to recompute the return from.
        market_store.replace_symbol_bars(["AAPL"], bars("AAPL", {RUN_DATE: 100.0}))

        summary = _evaluate(market_store, state_store, CALENDAR[15])

        assert _outcome_rows(state_store, 5) == first
        assert summary.preserved_outcome_count == 1
        assert any("既存の評価行を保持した" in note for note in summary.notes)

    def test_a_row_is_dropped_not_carried_forward_after_its_verdict_is_corrected(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # A symbol whose bars later disappear *and* whose verdict was
        # separately corrected (proceed -> skip) cannot be trusted to keep
        # its old `proceed` classification -- that would misrepresent a
        # verdict the store no longer holds. It is dropped, exactly as an
        # unrecomputable row always was before this fix.
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))
        _evaluate(market_store, state_store, CALENDAR[10])
        assert _outcome_rows(state_store, 5) != []

        _seed_verdict(state_store, run_id, recommendation="skip")
        market_store.replace_symbol_bars(["AAPL"], bars("AAPL", {RUN_DATE: 100.0}))

        summary = _evaluate(market_store, state_store, CALENDAR[15])

        assert _outcome_rows(state_store, 5) == []
        assert summary.preserved_outcome_count == 0
        assert any("満期日" in note and "スキップ" in note for note in summary.notes)

    def test_a_symbol_with_no_previous_row_still_writes_nothing_for_it(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # The read-back-and-carry-forward path must not manufacture a row
        # out of nothing: a symbol that never had a recorded outcome stays
        # absent, exactly as `TestEvaluateFailSoft` already expects.
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)

        summary = _evaluate(market_store, state_store, CALENDAR[10])

        assert _outcome_rows(state_store, 5) == []
        assert summary.preserved_outcome_count == 0


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

        # The maturity close is revised down sharply -- far past the 0.5%
        # `write_bars` accepts as a correction, so the repaired history
        # arrives through the rebuild path the operator actually uses.
        market_store.replace_symbol_bars(
            ["AAPL"], bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 95.0})
        )
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
            market_store,
            state_store,
            EvaluationRequest(
                as_of=as_of, thresholds=thresholds, benchmark_symbol=BENCHMARK
            ),
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
            market_store,
            state_store,
            EvaluationRequest(
                as_of=as_of, thresholds=thresholds, benchmark_symbol=BENCHMARK
            ),
        )

        assert summary.outcome_count >= 1


class TestOnlyPendingScope:
    """Issue #209: the daily pass evaluates only what is not already recorded.

    It runs ahead of the analysis export now, so its cost has to follow the
    slices that newly matured rather than the whole evaluation window -- but
    never at the price of keeping a stale classification of a corrected
    verdict.
    """

    def _pending_only(
        self, market_store: MarketStore, state_store: StateStore, as_of: date
    ) -> EvaluateSummary:
        return evaluate_verdicts(
            market_store,
            state_store,
            EvaluationRequest(
                as_of=as_of,
                thresholds=PostmortemConfig(),
                benchmark_symbol=BENCHMARK,
                only_pending=True,
            ),
        )

    def test_an_already_recorded_slice_is_left_alone(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))
        first = self._pending_only(market_store, state_store, CALENDAR[10])
        assert (first.evaluated_slice_count, first.recorded_slice_count) == (1, 0)

        # A later bar for the same maturity session would reclassify the slice
        # if it were re-read; the daily pass deliberately does not re-read it.
        market_store.write_bars(bars("AAPL", {MATURITY_5D: 90.0}))

        second = self._pending_only(market_store, state_store, CALENDAR[10])

        assert (second.evaluated_slice_count, second.recorded_slice_count) == (0, 1)
        assert _outcome_rows(state_store, 5) == [
            ("AAPL", 5, MATURITY_5D, "proceed", pytest.approx(1.5), HIT)
        ]

    def test_a_full_batch_still_reclassifies_after_a_price_correction(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # The manual `copilot-retro evaluate` keeps the correction path the
        # daily pass gives up: same inputs, `only_pending` off.
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))
        self._pending_only(market_store, state_store, CALENDAR[10])
        market_store.replace_symbol_bars(
            ["AAPL"], bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 90.0})
        )

        _evaluate(market_store, state_store, CALENDAR[10])

        assert _outcome_rows(state_store, 5) == [
            ("AAPL", 5, MATURITY_5D, "proceed", pytest.approx(-10.0), MISS_SEVERE)
        ]

    def test_a_corrected_verdict_is_reclassified(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        _seed_calendar(market_store)
        _seed_verdict(state_store, run_id)
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))
        self._pending_only(market_store, state_store, CALENDAR[10])

        # `retro collect` re-collected the run and the verdict flipped, so the
        # recorded slice no longer describes this run's verdicts.
        _seed_verdict(state_store, run_id, recommendation="skip")

        summary = self._pending_only(market_store, state_store, CALENDAR[10])

        assert (summary.evaluated_slice_count, summary.recorded_slice_count) == (1, 0)
        assert _outcome_rows(state_store, 5) == [
            ("AAPL", 5, MATURITY_5D, "skip", pytest.approx(1.5), MISS_MILD)
        ]

    def test_a_slice_missing_a_symbols_bars_keeps_being_retried(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # The recorded set is a strict subset of the run's verdicts, so the
        # slice never counts as done and picks the symbol up once its bars
        # arrive.
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
                for symbol in ("AAPL", "MSFT")
            ],
            [],
        )
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, MATURITY_5D: 101.5}))
        first = self._pending_only(market_store, state_store, CALENDAR[10])
        assert len(first.notes) == 1

        market_store.write_bars(bars("MSFT", {RUN_DATE: 100.0, MATURITY_5D: 99.0}))

        second = self._pending_only(market_store, state_store, CALENDAR[10])

        assert second.recorded_slice_count == 0
        assert _outcome_rows(state_store, 5) == [
            ("AAPL", 5, MATURITY_5D, "proceed", pytest.approx(1.5), HIT),
            ("MSFT", 5, MATURITY_5D, "proceed", pytest.approx(-1.0), MISS_MILD),
        ]

    def test_an_empty_window_asks_the_store_for_nothing(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        _seed_calendar(market_store)

        summary = self._pending_only(market_store, state_store, CALENDAR[10])

        assert (summary.evaluated_slice_count, summary.recorded_slice_count) == (0, 0)
        assert state_store.get_recorded_outcome_slices(()) == {}
