"""Read-only history-query contracts (P1-05, REQ-002..005)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from swing_copilot.models import Position, RunMode
from swing_copilot.risk.checks import RiskAssessment
from swing_copilot.screening.base import (
    Candidate,
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
)
from swing_copilot.storage.audit_records import ScreeningRunMeta, SignalOutcomeRecord
from swing_copilot.storage.history_queries import (
    get_rejections,
    get_run_by_date,
    get_run_detail,
    get_signal_outcomes,
    get_symbol_timeline,
    list_runs,
    run_exists,
)
from swing_copilot.storage.paper_records import TradeDecisionRecord

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore


def _candidate(symbol: str = "AAPL", rank: int = 1) -> Candidate:
    return Candidate(
        symbol,
        date(2026, 7, 20),
        ("trend_sma",),
        {"close": 100.0, "score": 0.5},
        rank,
    )


class TestListRuns:
    def test_empty_database_returns_empty_list(self, state_store: StateStore) -> None:
        assert list_runs(state_store._database, limit=20) == []  # noqa: SLF001

    def test_derives_counts_via_left_join_so_zero_rows_still_show(
        self, state_store: StateStore
    ) -> None:
        # Example 1: run_id with 10 candidates / 3 rejections / 2 decisions.
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
        state_store.record_candidates(
            [_candidate(f"SYM{i}", rank=i) for i in range(1, 3)], run_id, "default"
        )
        state_store.record_trade_decision(
            TradeDecisionRecord(
                run_id=run_id,
                symbol="SYM1",
                strategy_key="default",
                position_id=None,
                decision="followed",
                reason_memo=None,
                virtual_fill_price=100.0,
            )
        )
        # A second run with zero candidates/rejections/decisions must still
        # appear, showing 0 rather than disappearing (LEFT JOIN contract).
        empty_run_id = state_store.start_run(date(2026, 7, 21), RunMode.LIVE, "cfg")

        rows = list_runs(state_store._database, limit=20)  # noqa: SLF001

        by_run_id = {row.run_id: row for row in rows}
        assert by_run_id[run_id].candidate_count == 2
        assert by_run_id[run_id].rejection_count == 0
        assert by_run_id[run_id].decision_count == 1
        assert by_run_id[empty_run_id].candidate_count == 0
        assert by_run_id[empty_run_id].rejection_count == 0
        assert by_run_id[empty_run_id].decision_count == 0

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

    def test_returns_candidates_risk_and_decisions_for_the_run(
        self, state_store: StateStore
    ) -> None:
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            [_candidate()],
            [
                RejectionRecord(
                    symbol="MSFT",
                    stage=RejectionStage.TECHNICAL_SIGNAL,
                    reason_code=RejectionReasonCode.SIGNAL_TREND_NOT_MET,
                    detail={"rsi14": 70.0},
                )
            ],
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20)),
        )
        state_store.record_risk_assessments(
            [
                RiskAssessment(
                    symbol="AAPL",
                    status="approved",
                    max_shares=100,
                    entry_price=100.0,
                    stop_price=95.0,
                    reasons=(),
                    binding_constraint="trade_risk",
                )
            ],
            run_id,
        )
        state_store.record_trade_decision(
            TradeDecisionRecord(
                run_id=run_id,
                symbol="AAPL",
                strategy_key="default",
                position_id=None,
                decision="followed",
                reason_memo="test",
                virtual_fill_price=100.0,
            )
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
        assert detail.risk_assessments[0].binding_constraint == "trade_risk"
        assert len(detail.decisions) == 1
        assert detail.decisions[0].decision == "followed"


class TestGetRejections:
    def test_unknown_run_returns_empty_list(self, state_store: StateStore) -> None:
        assert get_rejections(state_store._database, uuid4()) == []  # noqa: SLF001

    def test_reads_p1_02_table_and_parses_detail_json(
        self, state_store: StateStore
    ) -> None:

        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
        state_store.record_screening_results(
            [],
            [
                RejectionRecord(
                    symbol="MSFT",
                    stage=RejectionStage.TECHNICAL_SIGNAL,
                    reason_code=RejectionReasonCode.SIGNAL_TREND_NOT_MET,
                    detail={"rsi14": 70.0, "note": "overbought"},
                )
            ],
            ScreeningRunMeta(run_id, "default", date(2026, 7, 20)),
        )

        rows = get_rejections(state_store._database, run_id)  # noqa: SLF001

        assert len(rows) == 1
        assert rows[0].symbol == "MSFT"
        assert rows[0].stage == "technical_signal"
        assert rows[0].reason_code == "SIGNAL_TREND_NOT_MET"
        assert rows[0].detail == {"rsi14": 70.0, "note": "overbought"}
        assert rows[0].as_of == date(2026, 7, 20)


class TestGetSymbolTimeline:
    def test_never_a_candidate_returns_none(self, state_store: StateStore) -> None:
        assert get_symbol_timeline(state_store._database, "ZZZZ") is None  # noqa: SLF001

    def test_merges_candidacy_and_decisions_across_runs(
        self, state_store: StateStore
    ) -> None:
        position_id = uuid4()
        state_store.upsert_position(
            Position(
                position_id=position_id,
                symbol="AAPL",
                is_paper=True,
                entry_date=date(2026, 7, 18),
                entry_price=100.0,
                shares=10,
                status="closed",
                close_date=date(2026, 7, 21),
                close_price=110.0,
                exit_reason="target",
            )
        )
        run_id = state_store.start_run(date(2026, 7, 18), RunMode.LIVE, "cfg")
        state_store.record_candidates([_candidate()], run_id, "default")
        state_store.record_trade_decision(
            TradeDecisionRecord(
                run_id=run_id,
                symbol="AAPL",
                strategy_key="default",
                position_id=position_id,
                decision="followed",
                reason_memo="test",
                virtual_fill_price=100.0,
            )
        )

        timeline = get_symbol_timeline(state_store._database, "AAPL")  # noqa: SLF001

        assert timeline is not None
        assert timeline.symbol == "AAPL"
        assert len(timeline.candidacies) == 1
        assert timeline.candidacies[0].run_id == run_id
        assert timeline.candidacies[0].score == 0.5
        assert len(timeline.decisions) == 1
        assert timeline.decisions[0].decision == "followed"
        assert timeline.decisions[0].realized_return_pct == 0.1


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
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
                "started_at) VALUES (?, ?, 'live', 'cfg', 'success', ?)",
                [str(uuid4()), run_date, datetime(2026, 7, 20, 8, tzinfo=UTC)],
            )
            newer_run_id = uuid4()
            conn.execute(
                "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
                "started_at) VALUES (?, ?, 'live', 'cfg', 'success', ?)",
                [str(newer_run_id), run_date, datetime(2026, 7, 20, 9, tzinfo=UTC)],
            )

        found = get_run_by_date(state_store._database, run_date)  # noqa: SLF001

        assert found == newer_run_id


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
