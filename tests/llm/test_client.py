"""Tests for LLMClient: structured output, cache, budget gate, audit (FR-08)."""

from __future__ import annotations

from datetime import date
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


def _news_summary(symbol: str = "AAPL", source_id: str = "news:1") -> NewsSummary:
    return NewsSummary(
        symbol=symbol,
        period="2027-01",
        facts=[SourcedFact(statement="Revenue grew", source_ids=[source_id])],
        interpretation=["This may suggest growth."],
        sentiment=1,
        risk_flags=[],
        sources=["https://example.com"],
    )


def _request(
    prompt: str = "Summarize this news.",
    source_ids: tuple[str, ...] = ("news:1",),
    model: str = MODEL,
) -> AnalyzeRequest:
    return AnalyzeRequest(
        run_id=uuid4(),
        prompt=prompt,
        source_ids=source_ids,
        schema=NewsSummary,
        schema_version=1,
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
            status = conn.execute("SELECT status FROM llm_calls").fetchone()
        assert status == ("failed",)

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
            status = conn.execute("SELECT status FROM llm_calls").fetchone()
        assert status == ("failed",)

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
