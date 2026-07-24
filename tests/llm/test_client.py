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
) -> AnalyzeRequest:
    return AnalyzeRequest(
        run_id=uuid4(),
        system_prompt="System safety instructions.",
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
            row = conn.execute("SELECT status, response_json FROM llm_calls").fetchone()
        assert row == ("failed", None)


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
            row = conn.execute("SELECT status, response_json FROM llm_calls").fetchone()
        assert row == ("failed", None)

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
