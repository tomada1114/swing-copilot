"""LLM call audit log: record, cache lookup, monthly cost (NFR-01, NFR-05).

Split out of `state_store.py` (which delegates one-line wrappers) to keep
that module under the project's 300-line guideline, same as
`audit_records.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

    from swing_copilot.storage.database import Database


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """One Claude API call's audit record (`llm_calls` table schema)."""

    call_id: UUID
    run_id: UUID
    model: str
    schema_name: str
    schema_version: int
    prompt_text: str
    prompt_hash: str
    source_ids: tuple[str, ...]
    status: str  # "success" | "failed" | "budget_skipped"
    input_tokens: int
    output_tokens: int
    input_price_per_mtok: float
    output_price_per_mtok: float
    cost_usd: float
    response_json: str | None = None
    error_detail: str | None = None


def record_llm_call(database: Database, call: LLMCallRecord) -> None:
    """Append one LLM call's audit record."""
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO llm_calls (
                call_id, run_id, model, schema_name, schema_version,
                prompt_text, prompt_hash, source_ids, status,
                input_tokens, output_tokens, input_price_per_mtok,
                output_price_per_mtok, cost_usd, response_json, error_detail,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
            """,
            [
                str(call.call_id),
                str(call.run_id),
                call.model,
                call.schema_name,
                call.schema_version,
                call.prompt_text,
                call.prompt_hash,
                list(call.source_ids),
                call.status,
                call.input_tokens,
                call.output_tokens,
                call.input_price_per_mtok,
                call.output_price_per_mtok,
                call.cost_usd,
                call.response_json,
                call.error_detail,
            ],
        )


def get_cached_response(
    database: Database, model: str, prompt_hash: str, schema_version: int
) -> str | None:
    """Return the most recent successful `response_json` for this natural key.

    Args:
        database: Shared DuckDB connection owner.
        model: Model ID the original call used.
        prompt_hash: Hash of the original prompt text.
        schema_version: Schema version the original call used.

    Returns:
        The cached `response_json`, or `None` if no successful call matches.
    """
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT response_json FROM llm_calls
            WHERE model = ? AND prompt_hash = ? AND schema_version = ? AND status = 'success'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [model, prompt_hash, schema_version],
        ).fetchone()
    return row[0] if row is not None else None


def get_monthly_cost(database: Database, as_of: date) -> float:
    """Return the sum of `cost_usd` for every call in `as_of`'s month.

    Sums across every `status` ("success", "failed", "budget_skipped"), not
    only "success" -- a call that Anthropic already billed for and then
    failed our own post-hoc validation (schema/CON-03) or was refused still
    has `cost_usd > 0` (`llm/client.py::analyze()`), and NFR-01's monthly
    budget cap must see that real spend to actually bound it (roadmap §5
    P6-26). Rows that were never billed (e.g. `budget_skipped`, or a pre-
    response API error) keep `cost_usd == 0.0`, so including them here is a
    no-op for the total.

    Args:
        database: Shared DuckDB connection owner.
        as_of: Any date within the month to total.

    Returns:
        Total realized cost in USD for that calendar month.
    """
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT coalesce(sum(cost_usd), 0.0) FROM llm_calls
            WHERE date_trunc('month', created_at) = date_trunc('month', ?::TIMESTAMPTZ)
            """,
            [as_of],
        ).fetchone()
    return float(row[0]) if row is not None else 0.0
