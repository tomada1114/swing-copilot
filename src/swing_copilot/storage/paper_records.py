"""Paper-trading journal writes, split out of `state_store.py` (FR-11, CON-04).

Same extraction pattern as `audit_records.py`/`llm_records.py`/
`text_records.py`. `trades_journal.journal_id` is a synthetic primary key,
but the business natural key `paper/journal.py` upserts on is
`(run_id, symbol, strategy_key)` — the table's actual `UNIQUE` constraint
(`docs/goal-prompts/swing-copilot-p2-report-paper-wrapup/decisions.md`: the
schema, not `docs/04_detailed_design.md` 3.20's stale pseudocode, is ground
truth here).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from uuid import UUID

    from swing_copilot.storage.database import Database


@dataclass(frozen=True, slots=True)
class TradeDecisionRecord:
    """One `trades_journal` row's business fields (`journal_id` is assigned on insert)."""

    run_id: UUID
    symbol: str
    strategy_key: str
    position_id: UUID | None
    decision: str  # "followed" | "ignored" | "modified"
    reason_memo: str | None
    virtual_fill_price: float | None


def record_trade_decision(database: Database, record: TradeDecisionRecord) -> None:
    """Upsert a trade decision, keyed by `(run_id, symbol, strategy_key)`."""
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO trades_journal (
                journal_id, run_id, symbol, strategy_key, position_id,
                decision, reason_memo, virtual_fill_price, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())
            ON CONFLICT (run_id, symbol, strategy_key) DO UPDATE SET
                position_id = EXCLUDED.position_id,
                decision = EXCLUDED.decision,
                reason_memo = EXCLUDED.reason_memo,
                virtual_fill_price = EXCLUDED.virtual_fill_price,
                created_at = EXCLUDED.created_at
            """,
            [
                str(uuid4()),
                str(record.run_id),
                record.symbol,
                record.strategy_key,
                str(record.position_id) if record.position_id is not None else None,
                record.decision,
                record.reason_memo,
                record.virtual_fill_price,
            ],
        )
