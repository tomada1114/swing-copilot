"""P2-11 signal postmortem / forward-return verification contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pandas as pd
import pytest

from swing_copilot.config import PostmortemConfig
from swing_copilot.models import RunMode
from swing_copilot.pipeline.postmortem import (
    FALSE_POSITIVE_MILD,
    FALSE_POSITIVE_SEVERE,
    NEUTRAL,
    TRUE_POSITIVE,
    classify_forward_return,
    compute_signal_performance,
    run_postmortem_step,
)
from swing_copilot.screening.base import (
    Candidate,
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
    ScreeningResult,
    TruncatedCandidate,
)
from swing_copilot.storage.audit_records import ScreeningRunMeta
from swing_copilot.storage.database import Database
from swing_copilot.storage.history_queries import SignalOutcomeRow
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore

AS_OF = date(2026, 7, 24)


# --- classify_forward_return boundaries ------------------------------------


@pytest.mark.parametrize(
    ("forward_return_pct", "expected"),
    [
        (0.499, NEUTRAL),
        (0.5, NEUTRAL),
        (0.501, TRUE_POSITIVE),
        (-0.499, NEUTRAL),
        (-0.5, NEUTRAL),
        (-0.501, FALSE_POSITIVE_MILD),
        (-1.999, FALSE_POSITIVE_MILD),
        (-2.0, FALSE_POSITIVE_MILD),
        (-2.001, FALSE_POSITIVE_SEVERE),
    ],
)
def test_classify_forward_return_boundaries(
    forward_return_pct: float, expected: str
) -> None:
    assert classify_forward_return(forward_return_pct) == expected


# --- compute_signal_performance ---------------------------------------------


def _outcome(
    symbol: str,
    horizon_days: int,
    signal_names: tuple[str, ...],
    classification: str,
) -> SignalOutcomeRow:
    return SignalOutcomeRow(
        run_id=uuid4(),
        symbol=symbol,
        horizon_days=horizon_days,
        as_of=AS_OF,
        signal_names=signal_names,
        forward_return_pct=0.0,
        classification=classification,
    )


class TestComputeSignalPerformance:
    def test_weighted_hit_rate_matches_hand_calculation(self) -> None:
        # 5 TP at 5d (weight 0.6) + 3 TP at 20d (weight 0.4)
        #   + 2 FP at 5d (weight 0.6) + 1 FP at 20d (weight 0.4)
        # weighted_tp = 5*0.6 + 3*0.4 = 3.0 + 1.2 = 4.2
        # weighted_fp = 2*0.6 + 1*0.4 = 1.2 + 0.4 = 1.6
        # hit_rate = 4.2 / (4.2 + 1.6) = 4.2 / 5.8
        outcomes = (
            *([_outcome("A", 5, ("SIG",), TRUE_POSITIVE)] * 5),
            *([_outcome("A", 20, ("SIG",), TRUE_POSITIVE)] * 3),
            *([_outcome("A", 5, ("SIG",), FALSE_POSITIVE_MILD)] * 2),
            *([_outcome("A", 20, ("SIG",), FALSE_POSITIVE_SEVERE)] * 1),
        )

        rows = compute_signal_performance(outcomes, PostmortemConfig())

        assert len(rows) == 1
        row = rows[0]
        assert row.signal_name == "SIG"
        assert row.true_positive_count == 8
        assert row.false_positive_count == 3
        assert row.neutral_count == 0
        assert row.n == 11
        assert row.hit_rate == pytest.approx(4.2 / 5.8)
        assert row.is_preliminary is True  # n=11 < default threshold 20

    def test_multi_signal_candidate_attributes_outcome_to_every_signal(self) -> None:
        outcomes = (_outcome("A", 5, ("SIG_A", "SIG_B"), TRUE_POSITIVE),)

        rows = compute_signal_performance(outcomes, PostmortemConfig())

        by_name = {row.signal_name: row for row in rows}
        assert set(by_name) == {"SIG_A", "SIG_B"}
        assert by_name["SIG_A"].true_positive_count == 1
        assert by_name["SIG_B"].true_positive_count == 1
        assert by_name["SIG_A"].hit_rate == 1.0
        assert by_name["SIG_B"].hit_rate == 1.0

    @pytest.mark.parametrize(
        ("count", "expected_preliminary"),
        [(19, True), (20, False), (21, False)],
    )
    def test_preliminary_sample_boundary_is_a_raw_unweighted_count(
        self, count: int, expected_preliminary: bool
    ) -> None:
        outcomes = tuple(_outcome("A", 5, ("SIG",), NEUTRAL) for _ in range(count))

        rows = compute_signal_performance(outcomes, PostmortemConfig())

        assert len(rows) == 1
        assert rows[0].n == count
        assert rows[0].is_preliminary is expected_preliminary

    def test_neutral_rows_are_excluded_from_the_hit_rate_denominator(self) -> None:
        outcomes = (
            _outcome("A", 5, ("SIG",), TRUE_POSITIVE),
            *([_outcome("A", 5, ("SIG",), NEUTRAL)] * 5),
        )

        rows = compute_signal_performance(outcomes, PostmortemConfig())

        assert len(rows) == 1
        row = rows[0]
        # NEUTRAL doesn't touch weighted_tp/weighted_fp, so hit_rate is 1.0
        # (the lone TP), not diluted by the 5 NEUTRAL occurrences.
        assert row.hit_rate == 1.0
        assert row.neutral_count == 5
        assert row.n == 6

    def test_signal_with_only_neutral_outcomes_has_none_hit_rate(self) -> None:
        outcomes = (_outcome("A", 5, ("SIG",), NEUTRAL),)

        rows = compute_signal_performance(outcomes, PostmortemConfig())

        assert rows[0].hit_rate is None

    def test_empty_outcomes_produce_empty_aggregation(self) -> None:
        assert compute_signal_performance((), PostmortemConfig()) == ()

    def test_rows_are_sorted_alphabetically_by_signal_name(self) -> None:
        outcomes = (
            _outcome("A", 5, ("Z_SIGNAL",), TRUE_POSITIVE),
            _outcome("A", 5, ("A_SIGNAL",), TRUE_POSITIVE),
        )

        rows = compute_signal_performance(outcomes, PostmortemConfig())

        assert [row.signal_name for row in rows] == ["A_SIGNAL", "Z_SIGNAL"]


# --- run_postmortem_step integration tests ----------------------------------


def _bars(symbol: str, prices: dict[date, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "date": bar_date,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 1_000_000,
                "provider": "test",
                "fetched_at": datetime(2026, 7, 24, tzinfo=UTC),
            }
            for bar_date, price in prices.items()
        ]
    )


def _calendar_dates(as_of: date, span_days: int = 30) -> list[date]:
    """`span_days` consecutive calendar dates ending at `as_of` (ascending)."""
    return [as_of - timedelta(days=i) for i in range(span_days - 1, -1, -1)]


def _candidate(symbol: str, run_date: date, close: float) -> Candidate:
    return Candidate(
        symbol=symbol,
        as_of=run_date,
        signal_names=("trend_sma",),
        metrics={"close": close},
        rank=1,
    )


@pytest.fixture
def market_store(tmp_path):
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store


def _seed_benchmark(market_store: MarketStore, as_of: date) -> None:
    dates = _calendar_dates(as_of)
    market_store.write_bars(_bars("SPY", dict.fromkeys(dates, 100.0)))


def _signal_outcome_rows(
    state_store: StateStore, symbol: str, horizon_days: int
) -> list[tuple[object, ...]]:
    with state_store._database.connect() as conn:  # noqa: SLF001
        return conn.execute(
            "SELECT forward_return_pct, classification FROM signal_outcomes "
            "WHERE symbol = ? AND horizon_days = ?",
            [symbol, horizon_days],
        ).fetchall()


class TestRunPostmortemStepHappyPath:
    def test_computes_and_persists_the_expected_classification(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        run_id = state_store.start_run(run_date_5d, RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[_candidate("AAPL", run_date_5d, 100.0)],
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", run_date_5d, 5),
        )
        # 100 -> 101.5: +1.5% -> TRUE_POSITIVE (> 0.5% neutral threshold).
        market_store.write_bars(_bars("AAPL", {run_date_5d: 100.0, AS_OF: 101.5}))

        note, performance = run_postmortem_step(
            market_store, state_store, AS_OF, PostmortemConfig(), "SPY"
        )

        rows = _signal_outcome_rows(state_store, "AAPL", 5)
        assert len(rows) == 1
        assert rows[0][0] == pytest.approx(1.5)
        assert rows[0][1] == TRUE_POSITIVE
        assert len(performance) == 1
        assert performance[0].signal_name == "trend_sma"
        # 20d horizon has no prior run at that date: reflected in the note.
        assert note is not None
        assert "20d" in note


class TestRunPostmortemStepRerunCorrection:
    def test_rerun_with_corrected_prices_updates_the_existing_row(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        run_id = state_store.start_run(run_date_5d, RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[_candidate("AAPL", run_date_5d, 100.0)],
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", run_date_5d, 5),
        )
        market_store.write_bars(_bars("AAPL", {run_date_5d: 100.0, AS_OF: 101.5}))

        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")
        first_rows = _signal_outcome_rows(state_store, "AAPL", 5)
        assert len(first_rows) == 1
        assert first_rows[0][1] == TRUE_POSITIVE

        # Corrected price data: the as_of close is revised down sharply.
        # 100 -> 95.0: -5.0% -> FALSE_POSITIVE_SEVERE (< -2%). A move that
        # large is a change of basis, not a correction, so `write_bars` now
        # quarantines it (Issue #413); the operator's repair path is a
        # wholesale rebuild of the symbol, which is what lands here.
        market_store.replace_symbol_bars(
            ["AAPL"], _bars("AAPL", {run_date_5d: 100.0, AS_OF: 95.0})
        )

        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")
        second_rows = _signal_outcome_rows(state_store, "AAPL", 5)

        # Exactly one row for this (run_id, symbol, horizon_days): updated,
        # not duplicated -- the correction-upsert invariant (AGENTS.md).
        assert len(second_rows) == 1
        assert second_rows[0][0] == pytest.approx(-5.0)
        assert second_rows[0][1] == FALSE_POSITIVE_SEVERE


class TestRunPostmortemStepNoPriorRun:
    def test_missing_run_at_target_date_skips_that_horizon_without_raising(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        _seed_benchmark(market_store, AS_OF)
        # No runs seeded at all: both horizons should be skipped.

        note, performance = run_postmortem_step(
            market_store, state_store, AS_OF, PostmortemConfig(), "SPY"
        )

        assert note is not None
        assert "5d" in note
        assert "20d" in note
        assert performance == ()

    def test_completely_fresh_database_with_no_benchmark_bars_does_not_raise(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # Brand-new install: not even the benchmark symbol has bars yet.
        note, performance = run_postmortem_step(
            market_store, state_store, AS_OF, PostmortemConfig(), "SPY"
        )

        assert note is not None
        assert performance == ()


class TestRunPostmortemStepMissingPriceData:
    def test_candidate_with_no_bars_is_skipped_without_raising(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        run_id = state_store.start_run(run_date_5d, RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[_candidate("MISSING", run_date_5d, 100.0)],
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", run_date_5d, 5),
        )
        # No bars written for "MISSING" at all -- a genuine data-quality gap.

        note, performance = run_postmortem_step(
            market_store, state_store, AS_OF, PostmortemConfig(), "SPY"
        )

        assert _signal_outcome_rows(state_store, "MISSING", 5) == []
        assert performance == ()
        # 5d found its run but produced zero persistable outcomes; 20d has no
        # prior run at all -- both are reflected as a plain None-note return
        # for the step overall (no candidate outcomes is not itself a
        # skip-worthy horizon note, only "no prior run" is).
        assert note is not None
        assert "20d" in note

    def test_candidate_with_bars_only_on_unrelated_dates_is_skipped(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        run_id = state_store.start_run(run_date_5d, RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[_candidate("GAP", run_date_5d, 100.0)],
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", run_date_5d, 5),
        )
        # A bar exists in the read window, but not on either date actually
        # needed (run_date_5d or AS_OF) -- a data gap, not a total absence.
        market_store.write_bars(_bars("GAP", {run_date_5d + timedelta(days=1): 50.0}))

        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")

        assert _signal_outcome_rows(state_store, "GAP", 5) == []

    def test_candidate_with_zero_close_on_run_date_is_skipped(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        run_id = state_store.start_run(run_date_5d, RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[_candidate("ZERO", run_date_5d, 0.0)],
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", run_date_5d, 5),
        )
        # A zero entry close would divide-by-zero if not guarded.
        market_store.write_bars(_bars("ZERO", {run_date_5d: 0.0, AS_OF: 10.0}))

        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")

        assert _signal_outcome_rows(state_store, "ZERO", 5) == []

    def test_candidate_with_bars_but_none_on_exactly_run_date_or_as_of_is_skipped(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        run_id = state_store.start_run(run_date_5d, RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[_candidate("GAPPY", run_date_5d, 100.0)],
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", run_date_5d, 5),
        )
        # A bar exists inside [run_date, as_of], but neither endpoint has one
        # -- still a data-quality skip, not a crash.
        market_store.write_bars(
            _bars("GAPPY", {run_date_5d + timedelta(days=1): 100.0})
        )

        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")

        assert _signal_outcome_rows(state_store, "GAPPY", 5) == []


class TestFindTargetTradingDayInsufficientHistory:
    def test_horizon_beyond_available_calendar_history_skips_that_horizon_only(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # Only 10 calendar days of SPY bars: enough distinct trading days for
        # the 5d horizon, but not for the 20d horizon (needs 21+).
        short_as_of = date(2026, 3, 1)
        dates = _calendar_dates(short_as_of, span_days=10)
        market_store.write_bars(_bars("SPY", dict.fromkeys(dates, 100.0)))
        run_date_5d = short_as_of - timedelta(days=5)
        run_id = state_store.start_run(run_date_5d, RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[_candidate("AAPL", run_date_5d, 100.0)],
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", run_date_5d, 5),
        )
        market_store.write_bars(_bars("AAPL", {run_date_5d: 100.0, short_as_of: 101.5}))

        note, _performance = run_postmortem_step(
            market_store, state_store, short_as_of, PostmortemConfig(), "SPY"
        )

        # 5d succeeded (persisted); 20d couldn't even locate a target date.
        assert _signal_outcome_rows(state_store, "AAPL", 5) != []
        assert note is not None
        assert "20d" in note
        assert "insufficient trading-day history" in note


class TestRunPostmortemStepLookAheadPrevention:
    def test_a_bar_dated_after_as_of_is_ignored(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        run_id = state_store.start_run(run_date_5d, RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[_candidate("AAPL", run_date_5d, 100.0)],
                rejections=[],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", run_date_5d, 5),
        )
        market_store.write_bars(
            _bars(
                "AAPL",
                {
                    run_date_5d: 100.0,
                    AS_OF: 101.5,
                    # An extreme future price that must never influence the
                    # computed forward return (REQ-006, look-ahead ban).
                    AS_OF + timedelta(days=3): 999_999.0,
                },
            )
        )

        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")

        rows = _signal_outcome_rows(state_store, "AAPL", 5)
        assert len(rows) == 1
        assert rows[0][0] == pytest.approx(1.5)
        assert rows[0][1] == TRUE_POSITIVE


def _universe_return_rows(
    state_store: StateStore, horizon_days: int
) -> list[tuple[object, ...]]:
    with state_store._database.connect() as conn:  # noqa: SLF001
        return conn.execute(
            "SELECT symbol, outcome_class, reason_code, forward_return_pct "
            "FROM universe_forward_returns WHERE horizon_days = ? ORDER BY symbol",
            [horizon_days],
        ).fetchall()


def _seed_control_group_run(
    market_store: MarketStore, state_store: StateStore, run_date: date
) -> None:
    """One past run holding a candidate, a near-miss, and a rejection."""
    run_id = state_store.start_run(run_date, RunMode.LIVE, "cfg")
    state_store.record_screening_results(
        ScreeningResult(
            candidates=[_candidate("CAND", run_date, 100.0)],
            rejections=[
                RejectionRecord(
                    symbol="GONE",
                    stage=RejectionStage.FUNDAMENTAL_FILTER,
                    reason_code=RejectionReasonCode.FILTER_NEGATIVE_FCF,
                    detail={"fcf": -1.0, "threshold": 0},
                )
            ],
            truncated=[
                TruncatedCandidate(
                    symbol="NEAR",
                    rank=6,
                    score=0.4,
                    score_breakdown={"score_liquidity": 0.5},
                    execution_state="READY",
                    execution_distance=None,
                )
            ],
        ),
        ScreeningRunMeta(run_id, "default", run_date, 5),
    )
    market_store.write_bars(_bars("CAND", {run_date: 100.0, AS_OF: 101.5}))
    market_store.write_bars(_bars("NEAR", {run_date: 50.0, AS_OF: 55.0}))
    market_store.write_bars(_bars("GONE", {run_date: 20.0, AS_OF: 19.0}))


class TestRunPostmortemStepControlGroups:
    """Issue #188: the near-misses and rejections get forward returns too."""

    def test_records_the_union_of_candidates_truncations_and_rejections(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        _seed_control_group_run(market_store, state_store, run_date_5d)

        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")

        assert _universe_return_rows(state_store, 5) == [
            ("CAND", "candidate", None, pytest.approx(1.5)),
            ("GONE", "rejected", "FILTER_NEGATIVE_FCF", pytest.approx(-5.0)),
            ("NEAR", "truncated", None, pytest.approx(10.0)),
        ]

    def test_signal_outcomes_still_cover_candidates_only(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        # The widened measurement must not leak into the per-signal hit-rate
        # table, whose rows are attributed to signals that actually fired.
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        _seed_control_group_run(market_store, state_store, run_date_5d)

        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")

        with state_store._database.connect() as conn:  # noqa: SLF001
            symbols = conn.execute(
                "SELECT symbol FROM signal_outcomes ORDER BY symbol"
            ).fetchall()
        assert symbols == [("CAND",)]

    def test_rerun_replaces_rather_than_duplicates_and_takes_corrections(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        _seed_control_group_run(market_store, state_store, run_date_5d)
        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")

        # Corrected bars for the rejected symbol, then an identical rerun.
        # 19.0 -> 22.0 is past `write_bars`' immutability tolerance, so the
        # repair arrives the way a real one does: a rebuild of that symbol.
        market_store.replace_symbol_bars(
            ["GONE"], _bars("GONE", {run_date_5d: 20.0, AS_OF: 22.0})
        )
        run_postmortem_step(market_store, state_store, AS_OF, PostmortemConfig(), "SPY")

        rows = _universe_return_rows(state_store, 5)
        assert len(rows) == 3
        assert rows[1] == (
            "GONE",
            "rejected",
            "FILTER_NEGATIVE_FCF",
            pytest.approx(10.0),
        )

    def test_a_symbol_without_bars_is_skipped_rather_than_stored(
        self, market_store: MarketStore, state_store: StateStore
    ) -> None:
        run_date_5d = AS_OF - timedelta(days=5)
        _seed_benchmark(market_store, AS_OF)
        run_id = state_store.start_run(run_date_5d, RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[
                    RejectionRecord(
                        symbol="NOBARS",
                        stage=RejectionStage.DATA_QUALITY,
                        reason_code=RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
                        detail={"available_bars": 0, "required_bars": 200},
                    )
                ],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", run_date_5d, 5),
        )

        note, _performance = run_postmortem_step(
            market_store, state_store, AS_OF, PostmortemConfig(), "SPY"
        )

        assert _universe_return_rows(state_store, 5) == []
        assert note is not None
        assert "20d" in note
