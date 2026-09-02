"""Read-only history-query contracts (P1-05, REQ-002..005)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from swing_copilot.models import RunMode, RunStatus
from swing_copilot.risk.checks import RiskAssessment
from swing_copilot.screening.base import (
    Candidate,
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
    ScreeningResult,
    TruncatedCandidate,
)
from swing_copilot.storage.audit_records import ScreeningRunMeta, SignalOutcomeRecord
from swing_copilot.storage.history_queries import (
    get_rejections,
    get_run_by_date,
    get_run_detail,
    get_run_started_at,
    get_run_statuses,
    get_signal_outcomes,
    get_successful_run,
    get_symbol_timeline,
    get_truncations,
    list_runs,
    run_exists,
)
from tests.support.runs import seed_run

if TYPE_CHECKING:
    from typing import NoReturn
    from uuid import UUID

    import pytest

    from swing_copilot.storage.state_store import StateStore


def _candidate(symbol: str = "AAPL", rank: int = 1) -> Candidate:
    return Candidate(
        symbol,
        date(2026, 7, 20),
        ("trend_sma",),
        {"close": 100.0, "score": 0.5},
        rank,
    )


def _insert_run(
    state_store: StateStore,
    run_id: UUID,
    run_date: date,
    status: str,
    started_at: datetime,
) -> None:
    """Insert a minimal `runs` row with an explicitly chosen lifecycle state."""
    seed_run(state_store, run_id, run_date, status=status, started_at=started_at)


class TestListRuns:
    def test_empty_database_returns_empty_list(self, state_store: StateStore) -> None:
        assert list_runs(state_store._database, limit=20) == []  # noqa: SLF001

    def test_derives_counts_via_left_join_so_zero_rows_still_show(
        self, state_store: StateStore
    ) -> None:
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
        state_store.record_candidates(
            [_candidate(f"SYM{i}", rank=i) for i in range(1, 3)], run_id, "default"
        )
        # A second run with zero candidates and zero rejections must still
        # appear, showing 0 rather than disappearing (LEFT JOIN contract).
        empty_run_id = state_store.start_run(date(2026, 7, 21), RunMode.LIVE, "cfg")

        rows = list_runs(state_store._database, limit=20)  # noqa: SLF001

        by_run_id = {row.run_id: row for row in rows}
        assert by_run_id[run_id].candidate_count == 2
        assert by_run_id[run_id].rejection_count == 0
        assert by_run_id[empty_run_id].candidate_count == 0
        assert by_run_id[empty_run_id].rejection_count == 0

    def test_orders_newest_first_and_respects_limit(
        self, state_store: StateStore
    ) -> None:
        older = state_store.start_run(date(2026, 7, 18), RunMode.LIVE, "cfg")
        newer = state_store.start_run(date(2026, 7, 22), RunMode.LIVE, "cfg")

        rows = list_runs(state_store._database, limit=1)  # noqa: SLF001

        assert len(rows) == 1
        assert rows[0].run_id == newer
        assert older != newer  # sanity: two distinct runs were created


class TestGetRunDetail:
    def test_unknown_run_id_returns_none(self, state_store: StateStore) -> None:
        assert get_run_detail(state_store._database, uuid4()) is None  # noqa: SLF001

    def test_returns_candidates_and_risk_for_the_run(
        self, state_store: StateStore
    ) -> None:
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[_candidate()],
                rejections=[
                    RejectionRecord(
                        symbol="MSFT",
                        stage=RejectionStage.TECHNICAL_SIGNAL,
                        reason_code=RejectionReasonCode.SIGNAL_TREND_NOT_MET,
                        detail={"rsi14": 70.0},
                    )
                ],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )
        state_store.record_risk_assessments(
            [
                RiskAssessment(
                    symbol="AAPL",
                    status="approved",
                    entry_price=100.0,
                    limit_price=101.0,
                    stop_price=95.0,
                    atr14=2.0,
                    stop_distance_pct=(101.0 - 95.0) / 101.0,
                    reasons=(),
                )
            ],
            run_id,
        )
        detail = get_run_detail(state_store._database, run_id)  # noqa: SLF001

        assert detail is not None
        assert detail.run_id == run_id
        assert detail.run_date == date(2026, 7, 20)
        assert len(detail.candidates) == 1
        assert detail.candidates[0].symbol == "AAPL"
        assert detail.candidates[0].score == 0.5
        assert detail.candidates[0].signal_names == ("trend_sma",)
        assert len(detail.risk_assessments) == 1
        assert detail.risk_assessments[0].binding_constraint is None


class TestGetRejections:
    def test_unknown_run_returns_empty_list(self, state_store: StateStore) -> None:
        assert get_rejections(state_store._database, uuid4()) == []  # noqa: SLF001

    def test_reads_p1_02_table_and_parses_detail_json(
        self, state_store: StateStore
    ) -> None:

        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[
                    RejectionRecord(
                        symbol="MSFT",
                        stage=RejectionStage.TECHNICAL_SIGNAL,
                        reason_code=RejectionReasonCode.SIGNAL_TREND_NOT_MET,
                        detail={"rsi14": 70.0, "note": "overbought"},
                    )
                ],
                truncated=[],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        rows = get_rejections(state_store._database, run_id)  # noqa: SLF001

        assert len(rows) == 1
        assert rows[0].symbol == "MSFT"
        assert rows[0].stage == "technical_signal"
        assert rows[0].reason_code == "SIGNAL_TREND_NOT_MET"
        assert rows[0].detail == {"rsi14": 70.0, "note": "overbought"}
        assert rows[0].as_of == date(2026, 7, 20)


class TestGetTruncations:
    """Issue #188: reading back the near-misses one past run recorded."""

    def test_unknown_run_returns_empty_list(self, state_store: StateStore) -> None:
        assert get_truncations(state_store._database, uuid4()) == []  # noqa: SLF001

    def test_returns_the_retained_tail_closest_to_the_cut_first(
        self, state_store: StateStore
    ) -> None:
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            ScreeningResult(
                candidates=[],
                rejections=[],
                truncated=[
                    TruncatedCandidate(
                        symbol=symbol,
                        rank=rank,
                        score=score,
                        score_breakdown={"score_liquidity": 0.5},
                        execution_state="READY",
                        execution_distance=None,
                    )
                    for symbol, rank, score in (("FAR", 7, 0.3), ("NEAR", 6, 0.4))
                ],
            ),
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
        )

        rows = get_truncations(state_store._database, run_id)  # noqa: SLF001

        assert [(row.symbol, row.rank, row.score) for row in rows] == [
            ("NEAR", 6, 0.4),
            ("FAR", 7, 0.3),
        ]
        assert rows[0].strategy_key == "default"
        assert rows[0].execution_state == "READY"
        assert rows[0].as_of == date(2026, 7, 20)


class TestGetSymbolTimeline:
    def test_never_a_candidate_returns_none(self, state_store: StateStore) -> None:
        assert get_symbol_timeline(state_store._database, "ZZZZ") is None  # noqa: SLF001

    def test_merges_candidacy_across_runs(self, state_store: StateStore) -> None:
        run_id = state_store.start_run(date(2026, 7, 18), RunMode.LIVE, "cfg")
        state_store.record_candidates([_candidate()], run_id, "default")

        timeline = get_symbol_timeline(state_store._database, "AAPL")  # noqa: SLF001

        assert timeline is not None
        assert timeline.symbol == "AAPL"
        assert len(timeline.candidacies) == 1
        assert timeline.candidacies[0].run_id == run_id
        assert timeline.candidacies[0].score == 0.5


class TestRunExists:
    def test_unknown_run_id_is_false(self, state_store: StateStore) -> None:
        assert run_exists(state_store._database, uuid4()) is False  # noqa: SLF001

    def test_known_run_id_is_true(self, state_store: StateStore) -> None:
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
        assert run_exists(state_store._database, run_id) is True  # noqa: SLF001


class TestGetRunByDate:
    """P2-11: locating "the run N trading days back" by its `run_date`."""

    def test_unknown_date_returns_none(self, state_store: StateStore) -> None:
        assert (
            get_run_by_date(state_store._database, date(2026, 7, 20))  # noqa: SLF001
            is None
        )

    def test_known_date_returns_the_run_id(self, state_store: StateStore) -> None:
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")

        found = get_run_by_date(state_store._database, date(2026, 7, 20))  # noqa: SLF001

        assert found == run_id

    def test_multiple_runs_same_date_picks_the_most_recently_started(
        self, state_store: StateStore
    ) -> None:
        run_date = date(2026, 7, 20)
        seed_run(
            state_store,
            uuid4(),
            run_date,
            started_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
        )
        newer_run_id = uuid4()
        seed_run(
            state_store,
            newer_run_id,
            run_date,
            started_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
        )

        found = get_run_by_date(state_store._database, run_date)  # noqa: SLF001

        assert found == newer_run_id


class TestGetRunStartedAt:
    """P8-119: `retro/collect.py`'s same-day duplicate tie-break input."""

    def test_unknown_run_id_returns_none(self, state_store: StateStore) -> None:
        assert get_run_started_at(state_store._database, uuid4()) is None  # noqa: SLF001

    def test_known_run_id_returns_its_started_at(self, state_store: StateStore) -> None:
        run_id = uuid4()
        started_at = datetime(2026, 8, 6, 15, 6, 7, tzinfo=UTC)
        seed_run(state_store, run_id, date(2026, 8, 6), started_at=started_at)

        found = get_run_started_at(state_store._database, run_id)  # noqa: SLF001

        assert found == started_at


class TestGetRunStatuses:
    """Issue #129: the incomplete-run scan's DB-side enrichment."""

    def test_returns_status_and_started_at_keyed_by_run_id(
        self, state_store: StateStore
    ) -> None:
        finished = uuid4()
        unfinished = uuid4()
        started_at = datetime(2026, 8, 10, 18, 30, tzinfo=UTC)
        _insert_run(state_store, finished, date(2026, 8, 10), "success", started_at)
        _insert_run(state_store, unfinished, date(2026, 8, 11), "failed", started_at)

        found = get_run_statuses(state_store._database, [finished, unfinished])  # noqa: SLF001

        assert found[finished].status == "success"
        assert found[finished].started_at == started_at
        assert found[unfinished].status == "failed"

    def test_unknown_run_id_is_absent_rather_than_defaulted(
        self, state_store: StateStore
    ) -> None:
        # The caller must be able to tell "no `runs` row" (a `reports/` vs DB
        # divergence) apart from any real status value.
        known = uuid4()
        unknown = uuid4()
        _insert_run(
            state_store,
            known,
            date(2026, 8, 10),
            "success",
            datetime(2026, 8, 10, 18, 30, tzinfo=UTC),
        )

        found = get_run_statuses(state_store._database, [known, unknown])  # noqa: SLF001

        assert set(found) == {known}

    def test_empty_request_returns_empty_without_opening_a_connection(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The scan calls this with an empty list whenever `reports/` holds no
        # unfinished run at all, which is the common case.
        def fail(*_args: object, **_kwargs: object) -> NoReturn:
            msg = "connect() must not be called for an empty request"
            raise AssertionError(msg)

        database = state_store._database  # noqa: SLF001
        monkeypatch.setattr(database, "connect", fail)

        assert get_run_statuses(database, []) == {}


class TestGetSuccessfulRun:
    """P8-118: the same-day rerun guard's existing-run lookup."""

    def test_no_run_on_the_date_returns_none(self, state_store: StateStore) -> None:
        assert (
            get_successful_run(state_store._database, date(2026, 8, 7))  # noqa: SLF001
            is None
        )

    def test_a_success_run_is_returned_with_its_report_path(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        # `StateStore.insert_run()` has no `report_path` parameter (only
        # `complete_run()` sets it), and this test asserts on it, so this
        # seed stays raw SQL.
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
                "started_at, report_path) VALUES (?, ?, 'live', 'cfg', 'success', "
                "?, ?)",
                [
                    str(run_id),
                    date(2026, 8, 7),
                    datetime(2026, 8, 7, 15, 5, tzinfo=UTC),
                    "reports/2026-08-07/x.md",
                ],
            )

        found = get_successful_run(state_store._database, date(2026, 8, 7))  # noqa: SLF001

        assert found is not None
        assert found.run_id == run_id
        assert found.report_path == Path("reports/2026-08-07/x.md")

    def test_a_null_report_path_is_returned_as_none(
        self, state_store: StateStore
    ) -> None:
        seed_run(
            state_store,
            uuid4(),
            date(2026, 8, 7),
            started_at=datetime(2026, 8, 7, 15, 5, tzinfo=UTC),
        )

        found = get_successful_run(state_store._database, date(2026, 8, 7))  # noqa: SLF001

        assert found is not None
        assert found.report_path is None

    def test_only_success_status_counts(self, state_store: StateStore) -> None:
        for status in (RunStatus.FAILED, RunStatus.RUNNING, RunStatus.DEGRADED):
            seed_run(
                state_store,
                uuid4(),
                date(2026, 8, 7),
                status=status,
                started_at=datetime(2026, 8, 7, 15, 5, tzinfo=UTC),
            )

        assert (
            get_successful_run(state_store._database, date(2026, 8, 7))  # noqa: SLF001
            is None
        )

    def test_multiple_success_runs_returns_the_most_recently_started(
        self, state_store: StateStore
    ) -> None:
        older_id = uuid4()
        newer_id = uuid4()
        seed_run(
            state_store,
            older_id,
            date(2026, 8, 6),
            started_at=datetime(2026, 8, 6, 15, 6, tzinfo=UTC),
        )
        seed_run(
            state_store,
            newer_id,
            date(2026, 8, 6),
            started_at=datetime(2026, 8, 6, 16, 52, tzinfo=UTC),
        )

        found = get_successful_run(state_store._database, date(2026, 8, 6))  # noqa: SLF001

        assert found is not None
        assert found.run_id == newer_id


class TestGetSignalOutcomes:
    """P2-11: read-back of `signal_outcomes` rows for the markdown aggregation."""

    def test_empty_table_returns_empty_tuple(self, state_store: StateStore) -> None:
        rows = get_signal_outcomes(
            state_store._database,  # noqa: SLF001
            date(2026, 7, 1),
            date(2026, 7, 24),
        )
        assert rows == ()

    def test_returns_rows_within_the_inclusive_window(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.record_signal_outcomes(
            [
                SignalOutcomeRecord(
                    run_id=run_id,
                    symbol="AAPL",
                    horizon_days=5,
                    as_of=date(2026, 7, 20),
                    signal_names=("trend_sma",),
                    forward_return_pct=1.5,
                    classification="TRUE_POSITIVE",
                ),
                SignalOutcomeRecord(
                    run_id=run_id,
                    symbol="MSFT",
                    horizon_days=20,
                    as_of=date(2026, 6, 1),  # outside the queried window
                    signal_names=("trend_sma",),
                    forward_return_pct=-3.0,
                    classification="FALSE_POSITIVE_SEVERE",
                ),
            ]
        )

        rows = get_signal_outcomes(
            state_store._database,  # noqa: SLF001
            date(2026, 7, 1),
            date(2026, 7, 24),
        )

        assert len(rows) == 1
        assert rows[0].symbol == "AAPL"
        assert rows[0].horizon_days == 5
        assert rows[0].signal_names == ("trend_sma",)
        assert rows[0].forward_return_pct == 1.5
        assert rows[0].classification == "TRUE_POSITIVE"
