"""Persistence contracts for LLM call audit records (NFR-01, NFR-05, P6-26)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from swing_copilot.storage.llm_records import LLMCallRecord

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore

MODEL = "claude-haiku-4-5-20251001"


def _today() -> date:
    # `record_llm_call()` always stamps `created_at` with the storage
    # layer's real `now()` (an audit insert time, not an injected `Clock`'s
    # `as_of`), so these tests need the real current date to land in the
    # same calendar month as the rows they just inserted.
    return datetime.now(UTC).date()


def _call(
    *,
    status: str,
    cost_usd: float,
    prompt_hash: str = "hash-1",
    schema_version: int = 1,
    response_json: str | None = None,
) -> LLMCallRecord:
    return LLMCallRecord(
        call_id=uuid4(),
        run_id=uuid4(),
        model=MODEL,
        schema_name="NewsSummary",
        schema_version=schema_version,
        prompt_text="prompt",
        prompt_hash=prompt_hash,
        source_ids=("news:1",),
        status=status,
        input_tokens=100,
        output_tokens=50,
        input_price_per_mtok=1.0,
        output_price_per_mtok=5.0,
        cost_usd=cost_usd,
        response_json=response_json,
    )


class TestGetMonthlyCost:
    """P6-26: sums every `status`, not only `status='success'` (the pre-fix bug).

    A call that Anthropic already billed for and then failed our own
    post-hoc validation (schema/CON-03) or was refused still has
    `cost_usd > 0` (`llm/client.py::analyze()`); the monthly budget gate
    must see that real spend.
    """

    def test_sums_successful_calls(self, state_store: StateStore) -> None:
        state_store.record_llm_call(_call(status="success", cost_usd=0.5))
        state_store.record_llm_call(_call(status="success", cost_usd=0.25))

        assert state_store.get_monthly_llm_cost(_today()) == pytest.approx(0.75)

    def test_includes_failed_calls_that_billed_real_tokens(
        self, state_store: StateStore
    ) -> None:
        state_store.record_llm_call(_call(status="success", cost_usd=0.1))
        state_store.record_llm_call(_call(status="failed", cost_usd=0.2))

        assert state_store.get_monthly_llm_cost(_today()) == pytest.approx(0.3)

    def test_budget_skipped_calls_contribute_nothing(
        self, state_store: StateStore
    ) -> None:
        # A `budget_skipped` row is never billed, so its `cost_usd` is always
        # 0.0 -- including it in the sum is a no-op, not a double-count risk.
        state_store.record_llm_call(_call(status="success", cost_usd=0.1))
        state_store.record_llm_call(_call(status="budget_skipped", cost_usd=0.0))

        assert state_store.get_monthly_llm_cost(_today()) == pytest.approx(0.1)

    def test_calls_outside_the_requested_month_are_excluded(
        self, state_store: StateStore
    ) -> None:
        # `created_at` is always the storage layer's real insertion time
        # (`llm_records.py::record_llm_call()` uses SQL `now()`), so a date
        # far outside the current month must total 0.
        state_store.record_llm_call(_call(status="success", cost_usd=0.5))

        assert state_store.get_monthly_llm_cost(date(2000, 1, 1)) == 0.0

    def test_no_calls_recorded_returns_zero(self, state_store: StateStore) -> None:
        assert state_store.get_monthly_llm_cost(_today()) == 0.0


class TestGetCachedResponse:
    """Unlike `get_monthly_cost()`, caching stays `status='success'`-only.

    A failed/budget_skipped row has no trustworthy `response_json` to serve
    -- P6-26 only changed cost *accounting*, not which rows are cacheable.
    """

    def test_returns_none_when_no_successful_call_matches(
        self, state_store: StateStore
    ) -> None:
        state_store.record_llm_call(
            _call(status="failed", cost_usd=0.2, response_json='{"symbol": "AAPL"}')
        )

        assert state_store.get_cached_llm_response(MODEL, "hash-1", 1) is None

    def test_returns_the_response_from_a_successful_call(
        self, state_store: StateStore
    ) -> None:
        state_store.record_llm_call(
            _call(status="failed", cost_usd=0.2, response_json='{"symbol": "STALE"}')
        )
        state_store.record_llm_call(
            _call(status="success", cost_usd=0.1, response_json='{"symbol": "AAPL"}')
        )

        assert state_store.get_cached_llm_response(MODEL, "hash-1", 1) == (
            '{"symbol": "AAPL"}'
        )

    def test_schema_version_mismatch_is_a_cache_miss(
        self, state_store: StateStore
    ) -> None:
        state_store.record_llm_call(
            _call(
                status="success",
                cost_usd=0.1,
                schema_version=1,
                response_json='{"symbol": "AAPL"}',
            )
        )

        assert state_store.get_cached_llm_response(MODEL, "hash-1", 2) is None
