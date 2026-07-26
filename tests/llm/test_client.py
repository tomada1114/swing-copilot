"""Tests for LLMClient: structured output, cache, budget gate, audit (FR-08)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import anthropic
import httpx
import pytest

from swing_copilot.llm.client import (
    AnalyzeRequest,
    BudgetExceededError,
    LLMClient,
    LLMClientTestSeams,
    LLMError,
    SchemaValidationError,
)
from swing_copilot.llm.pricing import ModelPricing
from swing_copilot.llm.safety import ForbiddenLanguageError
from swing_copilot.llm.schemas import NewsSummary, SourcedFact
from swing_copilot.storage.database import Database
from swing_copilot.storage.state_store import StateStore

if TYPE_CHECKING:
    from pydantic import BaseModel

MODEL = "claude-haiku-4-5-20251001"


class FakeUsage:
    def __init__(self, input_tokens: int = 100, output_tokens: int = 50) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(
        self,
        parsed_output: BaseModel | None,
        stop_reason: str = "end_turn",
        usage: FakeUsage | None = None,
    ) -> None:
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage()


class FakeMessages:
    def __init__(self, response_or_exception: FakeResponse | Exception) -> None:
        self._response_or_exception = response_or_exception
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        if isinstance(self._response_or_exception, Exception):
            raise self._response_or_exception
        return self._response_or_exception


class FakeAnthropicClient:
    def __init__(self, response_or_exception: FakeResponse | Exception) -> None:
        self.messages = FakeMessages(response_or_exception)


class FakeClock:
    def __init__(self, today: date) -> None:
        self._today = today

    def today(self) -> date:
        return self._today

    def now(self) -> datetime:
        return datetime.combine(self._today, datetime.min.time(), tzinfo=UTC)


def _news_summary(symbol: str = "AAPL", source_id: str = "news:1") -> NewsSummary:
    return NewsSummary(
        symbol=symbol,
        period="2027-01",
        facts=[SourcedFact(statement="Revenue grew", source_ids=[source_id])],
        interpretation=["This may suggest growth."],
        sentiment=1,
        risk_flags=[],
        sources=["https://example.com"],
        catalyst_quality="none",
        catalyst_quality_source_ids=[source_id],
    )


def _request(
    prompt: str = "Summarize this news.",
    source_ids: tuple[str, ...] = ("news:1",),
    model: str = MODEL,
    schema_version: int = 1,
    system_prompt: str = "System safety instructions.",
) -> AnalyzeRequest:
    return AnalyzeRequest(
        run_id=uuid4(),
        system_prompt=system_prompt,
        prompt=prompt,
        source_ids=source_ids,
        schema=NewsSummary,
        schema_version=schema_version,
        model=model,
        max_tokens=1024,
    )


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store


class TestSuccessfulAnalyze:
    def test_returns_parsed_schema_and_records_success(self, state_store):
        response = FakeResponse(_news_summary())
        fake_anthropic = FakeAnthropicClient(response)
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=fake_anthropic,
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

        result = client.analyze(_request())

        assert result == _news_summary()
        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, input_tokens, output_tokens, cost_usd FROM llm_calls"
            ).fetchone()
        assert row[0] == "success"
        assert row[1] == 100
        assert row[2] == 50
        assert row[3] == pytest.approx((100 * 1.0 + 50 * 5.0) / 1_000_000)

        call = fake_anthropic.messages.calls[0]
        assert call["system"] == "System safety instructions."
        assert call["messages"] == [{"role": "user", "content": "Summarize this news."}]

    def test_redacts_api_key_from_persisted_prompt_and_response(self, state_store):
        summary = _news_summary().model_copy(
            update={"interpretation": ["The literal token test-key appeared."]}
        )
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(summary)),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

        client.analyze(_request(prompt="Article accidentally contains test-key"))

        with state_store._database.connect() as conn:  # noqa: SLF001
            prompt_text, response_json = conn.execute(
                "SELECT prompt_text, response_json FROM llm_calls"
            ).fetchone()
        assert "test-key" not in prompt_text
        assert "test-key" not in response_json
        assert "[REDACTED]" in prompt_text


class TestCaching:
    def test_second_call_with_same_natural_key_does_not_call_the_api(self, state_store):
        response = FakeResponse(_news_summary())
        fake_client = FakeAnthropicClient(response)
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=fake_client, clock=FakeClock(date(2027, 1, 1))
            ),
        )
        request = _request()

        first = client.analyze(request)
        second = client.analyze(request)

        assert first == second
        assert len(fake_client.messages.calls) == 1

    def test_different_prompt_is_not_cached(self, state_store):
        response = FakeResponse(_news_summary())
        fake_client = FakeAnthropicClient(response)
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=fake_client, clock=FakeClock(date(2027, 1, 1))
            ),
        )

        client.analyze(_request(prompt="prompt A"))
        client.analyze(_request(prompt="prompt B"))

        assert len(fake_client.messages.calls) == 2

    def test_different_market_regime_in_system_prompt_is_not_cached(self, state_store):
        response = FakeResponse(_news_summary())
        fake_client = FakeAnthropicClient(response)
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=fake_client, clock=FakeClock(date(2027, 1, 1))
            ),
        )

        client.analyze(
            _request(system_prompt="System\n<market_regime>Gate: BULL</market_regime>")
        )
        client.analyze(
            _request(system_prompt="System\n<market_regime>Gate: BEAR</market_regime>")
        )

        assert len(fake_client.messages.calls) == 2

    def test_cached_response_is_revalidated_against_current_source_ids(
        self, state_store
    ):
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(_news_summary())),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )
        client.analyze(_request(source_ids=("news:1",)))

        with pytest.raises(SchemaValidationError):
            client.analyze(_request(source_ids=("news:2",)))

    def test_different_schema_version_is_not_cached(self, state_store):
        """Different schema_version is not cached.

        P2-12 item 6: a `schema_version` bump must not spuriously hit an
        older cache row (e.g. after `NewsSummary` gained new required fields).
        """
        response = FakeResponse(_news_summary())
        fake_client = FakeAnthropicClient(response)
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=fake_client, clock=FakeClock(date(2027, 1, 1))
            ),
        )
        request = _request(schema_version=1)

        client.analyze(request)
        client.analyze(_request(schema_version=2))

        assert len(fake_client.messages.calls) == 2


class TestGetCachedAt:
    """P6-27: `get_cached_at()` never alters `analyze()`'s own behavior.

    Purely additive: it only reports the natural key's most recent
    successful call's creation date (or `None`).
    """

    def test_returns_none_before_any_call_is_recorded(self, state_store):
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(_news_summary())),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

        assert client.get_cached_at(_request()) is None

    def test_returns_a_date_right_after_a_fresh_call_is_recorded(self, state_store):
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(_news_summary())),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )
        request = _request()

        client.analyze(request)

        assert client.get_cached_at(request) is not None

    def test_a_cache_hit_reports_the_original_calls_date_not_a_new_one(
        self, state_store
    ):
        # `analyze()`'s cache-hit branch returns early without re-recording
        # (`llm/client.py::analyze()`), so `get_cached_at()` must keep
        # reporting the *original* row -- otherwise every cache hit would
        # look artificially fresh and near-stale warnings would never fire.
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(_news_summary())),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )
        request = _request()
        client.analyze(request)
        first_cached_at = client.get_cached_at(request)

        client.analyze(request)  # cache hit: no new `llm_calls` row
        second_cached_at = client.get_cached_at(request)

        assert first_cached_at == second_cached_at

    def test_does_not_match_a_different_natural_key(self, state_store):
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(_news_summary())),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )
        client.analyze(_request(prompt="prompt A"))

        assert client.get_cached_at(_request(prompt="prompt B")) is None


class TestRefusalAndErrors:
    def test_refusal_stop_reason_raises_and_records_failure(self, state_store):
        response = FakeResponse(None, stop_reason="refusal")
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(response),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

        with pytest.raises(LLMError, match="refused"):
            client.analyze(_request())

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, input_tokens, output_tokens, cost_usd FROM llm_calls"
            ).fetchone()
        assert row[0] == "failed"
        # P6-26: a refusal still bills Anthropic for the tokens already
        # consumed (`response.usage`, the `FakeResponse` default), so this
        # must not silently record 0/0/0.0 like the pre-P6-26 behavior.
        assert row[1] == 100
        assert row[2] == 50
        assert row[3] == pytest.approx((100 * 1.0 + 50 * 5.0) / 1_000_000)

    def test_api_error_raises_llm_error_and_records_failure(self, state_store):
        exc = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(exc),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

        with pytest.raises(LLMError):
            client.analyze(_request())

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, input_tokens, output_tokens, cost_usd FROM llm_calls"
            ).fetchone()
        assert row[0] == "failed"
        # No `response` exists on this branch (the SDK call itself raised),
        # so there is no usage to report -- 0/0/0.0 stays correct here,
        # unlike the refusal/validation-failure branches above (P6-26).
        assert row[1] == 0
        assert row[2] == 0
        assert row[3] == 0.0

    def test_fact_citing_unknown_source_id_raises_schema_validation_error(
        self, state_store
    ):
        response = FakeResponse(_news_summary(source_id="news:unknown"))
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(response),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

        with pytest.raises(SchemaValidationError):
            client.analyze(_request(source_ids=("news:1",)))

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, input_tokens, output_tokens, cost_usd FROM llm_calls"
            ).fetchone()
        assert row[0] == "failed"
        # P6-26: a schema-validation failure still bills Anthropic for the
        # tokens already consumed.
        assert row[1] == 100
        assert row[2] == 50
        assert row[3] == pytest.approx((100 * 1.0 + 50 * 5.0) / 1_000_000)

    def test_catalyst_quality_citing_unknown_source_id_raises_schema_validation_error(
        self, state_store
    ):
        """P2-12 (REQ-008): mirrors the facts-based unknown-source-id test above."""
        unsafe = _news_summary().model_copy(
            update={"catalyst_quality_source_ids": ["news:unknown"]}
        )
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(unsafe)),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

        with pytest.raises(SchemaValidationError):
            client.analyze(_request(source_ids=("news:1",)))

    def test_forbidden_language_is_failed_before_it_can_be_cached(self, state_store):
        unsafe = _news_summary().model_copy(
            update={"interpretation": ["You should buy now."]}
        )
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(unsafe)),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

        with pytest.raises(ForbiddenLanguageError):
            client.analyze(_request())

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, response_json, input_tokens, output_tokens, cost_usd "
                "FROM llm_calls"
            ).fetchone()
        assert row[0] == "failed"
        assert row[1] is None
        # P6-26: a CON-03 forbidden-language failure still bills Anthropic
        # for the tokens already consumed.
        assert row[2] == 100
        assert row[3] == 50
        assert row[4] == pytest.approx((100 * 1.0 + 50 * 5.0) / 1_000_000)


class TestBudgetGate:
    def test_exceeding_monthly_cap_skips_the_call_and_records_budget_skipped(
        self, state_store
    ):
        fake_client = FakeAnthropicClient(FakeResponse(_news_summary()))
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=0.000001,
            test_seams=LLMClientTestSeams(
                anthropic_client=fake_client, clock=FakeClock(date(2027, 1, 1))
            ),
        )

        with pytest.raises(BudgetExceededError):
            client.analyze(_request())

        assert fake_client.messages.calls == []
        with state_store._database.connect() as conn:  # noqa: SLF001
            status = conn.execute("SELECT status FROM llm_calls").fetchone()
        assert status == ("budget_skipped",)

    def test_a_prior_failed_calls_real_cost_counts_toward_the_monthly_gate(
        self, state_store
    ):
        """P6-26: `get_monthly_cost()` must not silently ignore failed-call cost.

        A refusal still bills Anthropic for the tokens it consumed
        (`response.usage`). If the monthly-cost query only summed
        `status='success'` rows (the pre-P6-26 bug), that real spend would
        never reach the budget gate, and a tiny follow-up call would sail
        through even though the account has already been billed past its cap.

        `record_llm_call()` stamps `created_at` with the storage layer's own
        real `now()` (an audit insert time, not the injected `Clock`'s
        `as_of`), so both clients here use `today()` = the real current date
        -- the injected `Clock` only drives which calendar month
        `get_monthly_llm_cost()` totals, and it must be the month the row
        was actually just inserted into.
        """
        today = datetime.now(UTC).date()
        refusing_client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(
                    FakeResponse(None, stop_reason="refusal")
                ),
                clock=FakeClock(today),
            ),
        )
        with pytest.raises(LLMError, match="refused"):
            refusing_client.analyze(_request())
        # Real cost billed by the refusal above: (100*1.0 + 50*5.0) / 1e6.

        # This second call's own estimated cost is tiny (1-char prompt/
        # system_prompt, max_tokens=1) -- small enough to fit under the cap
        # entirely on its own. It is blocked only because the refusal above
        # already spent real budget this month.
        tiny_request = AnalyzeRequest(
            run_id=uuid4(),
            system_prompt="s",
            prompt="p",
            source_ids=("news:1",),
            schema=NewsSummary,
            schema_version=1,
            model=MODEL,
            max_tokens=1,
        )
        gated_client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=0.0001,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(_news_summary())),
                clock=FakeClock(today),
            ),
        )

        with pytest.raises(BudgetExceededError):
            gated_client.analyze(tiny_request)


class TestEstimateCost:
    """P6-26: `_CHARS_PER_TOKEN_ESTIMATE` (2.0) drives the pre-call estimate.

    `_estimate_cost()` has no public equivalent (it only feeds the pre-call
    budget check inside `analyze()`), so it is exercised directly here,
    matching this file's existing pattern of reaching past the public
    interface (`state_store._database`) when a formula has no other
    observable seam.
    """

    def _client(self, state_store: StateStore) -> LLMClient:
        return LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(_news_summary())),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

    def test_applies_the_chars_per_token_estimate_to_input_tokens(self, state_store):
        client = self._client(state_store)
        pricing = ModelPricing().get(MODEL)  # (1.0, 5.0)

        # 200 chars / 2.0 chars-per-token (P6-26) = 100 estimated input tokens.
        estimated = client._estimate_cost(  # noqa: SLF001
            "x" * 200, max_tokens=1000, pricing=pricing
        )

        expected = (100 * pricing[0] + 1000 * pricing[1]) / 1_000_000
        assert estimated == pytest.approx(expected)

    def test_scales_linearly_with_prompt_length(self, state_store):
        client = self._client(state_store)
        pricing = ModelPricing().get(MODEL)

        short = client._estimate_cost("x" * 20, max_tokens=0, pricing=pricing)  # noqa: SLF001
        long = client._estimate_cost("x" * 2000, max_tokens=0, pricing=pricing)  # noqa: SLF001

        assert long == pytest.approx(short * 100)

    def test_empty_prompt_still_reflects_max_tokens_cost(self, state_store):
        client = self._client(state_store)
        pricing = ModelPricing().get(MODEL)

        estimated = client._estimate_cost("", max_tokens=1000, pricing=pricing)  # noqa: SLF001

        assert estimated == pytest.approx(1000 * pricing[1] / 1_000_000)


class TestUnknownPricing:
    def test_unknown_model_raises_before_any_api_call(self, state_store):
        fake_client = FakeAnthropicClient(FakeResponse(_news_summary()))
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=fake_client, clock=FakeClock(date(2027, 1, 1))
            ),
        )

        with pytest.raises(Exception, match="Unknown pricing"):
            client.analyze(_request(model="claude-made-up-model"))

        assert fake_client.messages.calls == []


class TestCon03BehavioralClaims:
    """P2-12 (REQ-009/021): unevidenced behavioral/psychological claims fail-closed."""

    def test_bare_psychological_diagnosis_raises_and_is_never_cached(self, state_store):
        unsafe = _news_summary().model_copy(
            update={"interpretation": ["経営陣は動揺している可能性が高い。"]}
        )
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=FakeAnthropicClient(FakeResponse(unsafe)),
                clock=FakeClock(date(2027, 1, 1)),
            ),
        )

        with pytest.raises(ForbiddenLanguageError):
            client.analyze(_request())

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, response_json, input_tokens, output_tokens, cost_usd "
                "FROM llm_calls"
            ).fetchone()
        assert row[0] == "failed"
        assert row[1] is None
        # P6-26: a CON-03 forbidden-language failure still bills Anthropic
        # for the tokens already consumed.
        assert row[2] == 100
        assert row[3] == 50
        assert row[4] == pytest.approx((100 * 1.0 + 50 * 5.0) / 1_000_000)

    def test_evidenced_behavioral_claim_is_accepted_and_cached(self, state_store):
        evidenced = _news_summary().model_copy(
            update={
                "interpretation": [
                    "実績が計画を10%下回ったことから、"
                    "経営陣が動揺している可能性がある。"
                ]
            }
        )
        fake_client = FakeAnthropicClient(FakeResponse(evidenced))
        client = LLMClient(
            "test-key",
            state_store,
            ModelPricing(),
            monthly_budget_cap_usd=5.0,
            test_seams=LLMClientTestSeams(
                anthropic_client=fake_client, clock=FakeClock(date(2027, 1, 1))
            ),
        )
        request = _request()

        first = client.analyze(request)
        second = client.analyze(request)

        assert first == second == evidenced
        assert len(fake_client.messages.calls) == 1
        with state_store._database.connect() as conn:  # noqa: SLF001
            status = conn.execute("SELECT status FROM llm_calls").fetchone()
        assert status == ("success",)
