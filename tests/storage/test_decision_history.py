"""Point-in-time decision-history queries used by later LLM calls."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from swing_copilot.models import Position, RunMode
from swing_copilot.screening.base import Candidate
from swing_copilot.storage.paper_records import TradeDecisionRecord

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore


def _candidate(symbol: str = "AAPL") -> Candidate:
    return Candidate(symbol, date(2026, 7, 20), ("trend_sma",), {"close": 100.0}, 1)


def _record_decision(
    state_store: StateStore,
    run_id: UUID,
    *,
    decision: str = "followed",
    position_id: UUID | None = None,
) -> None:
    state_store.record_trade_decision(
        TradeDecisionRecord(
            run_id=run_id,
            symbol="AAPL",
            strategy_key="default",
            position_id=position_id,
            decision=decision,
            reason_memo="出来高の増加を確認",
            virtual_fill_price=100.0 if decision != "ignored" else None,
        )
    )


def test_history_is_prior_live_same_symbol_strategy_and_has_realized_return(
    state_store: StateStore,
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
        )
    )
    prior = state_store.start_run(date(2026, 7, 18), RunMode.LIVE, "cfg")
    state_store.record_candidates([_candidate()], prior, "default")
    _record_decision(state_store, prior, position_id=position_id)

    same_day = state_store.start_run(date(2026, 7, 22), RunMode.LIVE, "cfg")
    state_store.record_candidates([_candidate()], same_day, "default")
    _record_decision(state_store, same_day, decision="ignored")

    dry_run = state_store.start_run(date(2026, 7, 19), RunMode.DRY_RUN, "cfg")
    state_store.record_candidates([_candidate()], dry_run, "default")
    _record_decision(state_store, dry_run, decision="ignored")

    history = state_store.get_decision_history(
        "AAPL", "default", before_date=date(2026, 7, 22), limit=3
    )

    assert len(history) == 1
    assert history[0].run_id == prior
    assert history[0].run_date == date(2026, 7, 18)
    assert history[0].decision == "followed"
    assert history[0].reason_memo == "出来高の増加を確認"
    assert history[0].realized_return_pct == 0.1


def test_candidate_strategy_is_inferred_only_when_unambiguous(
    state_store: StateStore,
) -> None:
    run_id = state_store.start_run(date(2026, 7, 22), RunMode.LIVE, "cfg")
    state_store.record_candidates([_candidate()], run_id, "default")

    assert state_store.get_candidate_strategy_keys(run_id, "AAPL") == ("default",)
    assert state_store.get_candidate_strategy_keys(run_id, "MSFT") == ()
