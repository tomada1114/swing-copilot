"""Claude API gateway: structured output, cache, budget gate, audit (FR-08, NFR-01, NFR-05, NFR-06).

Retries are the Anthropic SDK's own (`max_retries=2`, i.e. 3 total attempts,
per `docs/goal-prompts/.../decisions.md` D8) — this client does not wrap a
second retry loop around it ("do not double-retry SDK retries"). A `timeout`
bounds the whole call so total wall-clock backoff stays capped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import anthropic

from swing_copilot.clock import SystemClock
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.storage.llm_records import LLMCallRecord

if TYPE_CHECKING:
    from uuid import UUID

    from pydantic import BaseModel

    from swing_copilot.clock import Clock
    from swing_copilot.llm.pricing import ModelPricing
    from swing_copilot.storage.state_store import StateStore

_MAX_TOTAL_TIMEOUT_SECONDS = 60.0
_SDK_MAX_RETRIES = 2  # + 1 initial attempt = 3 total, per decisions.md D8
_CHARS_PER_TOKEN_ESTIMATE = 4  # rough, conservative pre-call budget estimate


class LLMError(SwingCopilotError):
    """Raised when a Claude call fails after the SDK's own retries."""


class BudgetExceededError(LLMError):
    """Raised when a call would exceed the monthly LLM budget cap."""


class SchemaValidationError(LLMError):
    """Raised when structured output violates a contract `pydantic` can't express."""


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


def _validate_source_ids(parsed: BaseModel, source_ids: tuple[str, ...]) -> None:
    facts = getattr(parsed, "facts", None)
    if not facts:
        return
    allowed = set(source_ids)
    for fact in facts:
        unknown = set(fact.source_ids) - allowed
        if unknown:
            msg = f"Fact cites unknown source_ids not provided to the model: {unknown}"
            raise SchemaValidationError(msg)


@dataclass(frozen=True, slots=True)
class AnalyzeRequest:
    """Grouped `analyze()` parameters (keeps the method under 5 arguments)."""

    run_id: UUID
    prompt: str
    source_ids: tuple[str, ...]
    schema: type[BaseModel]
    schema_version: int
    model: str
    max_tokens: int


@dataclass(frozen=True, slots=True)
class LLMClientTestSeams:
    """Injectable collaborators, used by tests to avoid real network/dates."""

    # object: real type is anthropic.Anthropic, but tests inject fakes with an
    # arbitrary `.messages.parse(...)` shape that Protocol attribute matching
    # can't express without over-constraining the fake's own typing.
    anthropic_client: object | None = None
    clock: Clock | None = None


@dataclass(frozen=True, slots=True)
class _CallOutcome:
    """Everything about one `analyze()` attempt that ends up in the audit log."""

    status: str  # "success" | "failed" | "budget_skipped"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    response_json: str | None = None
    error_detail: str | None = None


class LLMClient:
    """Anthropic SDK wrapper: structured output, cache, budget gate, audit."""

    def __init__(
        self,
        api_key: str,
        state_store: StateStore,
        pricing: ModelPricing,
        monthly_budget_cap_usd: float,
        test_seams: LLMClientTestSeams | None = None,
    ) -> None:
        """Create the client.

        Args:
            api_key: `ANTHROPIC_API_KEY`.
            state_store: Store used for caching and audit logging.
            pricing: Per-model USD-per-MTok pricing lookup.
            monthly_budget_cap_usd: Monthly LLM spend cap in USD (NFR-01).
            test_seams: Injectable SDK client/clock, used by tests to avoid
                real network calls or dates.
        """
        seams = test_seams or LLMClientTestSeams()
        self._state_store = state_store
        self._pricing = pricing
        self._monthly_budget_cap_usd = monthly_budget_cap_usd
        self._clock = seams.clock or SystemClock()
        self._client = seams.anthropic_client or anthropic.Anthropic(
            api_key=api_key,
            max_retries=_SDK_MAX_RETRIES,
            timeout=_MAX_TOTAL_TIMEOUT_SECONDS,
        )

    def analyze(self, request: AnalyzeRequest) -> BaseModel:
        """Call Claude and return schema-validated structured output.

        Args:
            request: The grouped analysis request (see `AnalyzeRequest`).

        Returns:
            The parsed, schema-validated response.

        Raises:
            BudgetExceededError: The monthly LLM budget cap would be exceeded.
            LLMError: The call failed (after the SDK's own retries) or the
                model refused to produce structured output.
            SchemaValidationError: A fact cited a source ID not provided.
        """
        prompt_hash = _prompt_hash(request.prompt)
        cached = self._state_store.get_cached_llm_response(
            request.model, prompt_hash, request.schema_version
        )
        if cached is not None:
            cached_result: BaseModel = request.schema.model_validate_json(cached)
            return cached_result

        pricing = self._pricing.get(request.model)
        estimated_cost = self._estimate_cost(request, pricing)
        current_cost = self._state_store.get_monthly_llm_cost(self._clock.today())
        if current_cost + estimated_cost > self._monthly_budget_cap_usd:
            self._record(request, prompt_hash, pricing, _CallOutcome("budget_skipped"))
            msg = f"Monthly LLM budget cap (${self._monthly_budget_cap_usd}) would be exceeded"
            raise BudgetExceededError(msg)

        try:
            response = self._client.messages.parse(  # type: ignore[attr-defined]
                model=request.model,
                max_tokens=request.max_tokens,
                messages=[{"role": "user", "content": request.prompt}],
                output_format=request.schema,
            )
        except anthropic.AnthropicError as exc:
            outcome = _CallOutcome("failed", error_detail=str(exc))
            self._record(request, prompt_hash, pricing, outcome)
            msg = f"Claude API call failed: {exc}"
            raise LLMError(msg) from exc

        raw_parsed = response.parsed_output
        if response.stop_reason == "refusal" or raw_parsed is None:
            detail = f"model refused or returned no structured output (stop_reason={response.stop_reason})"
            self._record(
                request,
                prompt_hash,
                pricing,
                _CallOutcome("failed", error_detail=detail),
            )
            msg = "Claude refused to produce structured output"
            raise LLMError(msg)
        parsed: BaseModel = raw_parsed

        try:
            _validate_source_ids(parsed, request.source_ids)
        except SchemaValidationError as exc:
            outcome = _CallOutcome("failed", error_detail=str(exc))
            self._record(request, prompt_hash, pricing, outcome)
            raise

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = (input_tokens * pricing[0] + output_tokens * pricing[1]) / 1_000_000
        outcome = _CallOutcome(
            "success",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            response_json=parsed.model_dump_json(),
        )
        self._record(request, prompt_hash, pricing, outcome)
        return parsed

    def _estimate_cost(
        self, request: AnalyzeRequest, pricing: tuple[float, float]
    ) -> float:
        input_price, output_price = pricing
        estimated_input_tokens = len(request.prompt) / _CHARS_PER_TOKEN_ESTIMATE
        return (
            estimated_input_tokens * input_price + request.max_tokens * output_price
        ) / 1_000_000

    def _record(
        self,
        request: AnalyzeRequest,
        prompt_hash: str,
        pricing: tuple[float, float],
        outcome: _CallOutcome,
    ) -> None:
        input_price, output_price = pricing
        self._state_store.record_llm_call(
            LLMCallRecord(
                call_id=uuid4(),
                run_id=request.run_id,
                model=request.model,
                schema_name=request.schema.__name__,
                schema_version=request.schema_version,
                prompt_text=request.prompt,
                prompt_hash=prompt_hash,
                source_ids=request.source_ids,
                status=outcome.status,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                input_price_per_mtok=input_price,
                output_price_per_mtok=output_price,
                cost_usd=outcome.cost_usd,
                response_json=outcome.response_json,
                error_detail=outcome.error_detail,
            )
        )
