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
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from datetime import date
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


@dataclass(frozen=True, slots=True)
class DecisionHistoryEntry:
    """Prior live decision safe to include in a later live LLM request."""

    run_id: UUID
    run_date: date
    symbol: str
    strategy_key: str
    decision: str
    reason_memo: str | None
    virtual_fill_price: float | None
    realized_return_pct: float | None


@dataclass(frozen=True, slots=True)
class PositionExcursionRecord:
    """Cumulative per-share MAE/MFE snapshot for one position and day."""

    position_id: UUID
    as_of_date: date
    mae_per_share: float | None
    mfe_per_share: float | None
    data_quality: str


def upsert_position_excursions(
    database: Database, records: list[PositionExcursionRecord]
) -> None:
    """Correction-upsert a logical day's snapshots in one transaction."""
    if not records:
        return
    conn = database.connect()
    try:
        conn.execute("BEGIN TRANSACTION")
        for record in records:
            conn.execute(
                """
                INSERT INTO position_excursions (
                    position_id, as_of_date, mae_per_share, mfe_per_share,
                    data_quality, created_at
                ) VALUES (?, ?, ?, ?, ?, now())
                ON CONFLICT (position_id, as_of_date) DO UPDATE SET
                    mae_per_share = EXCLUDED.mae_per_share,
                    mfe_per_share = EXCLUDED.mfe_per_share,
                    data_quality = EXCLUDED.data_quality,
                    created_at = EXCLUDED.created_at
                """,
                [
                    str(record.position_id),
                    record.as_of_date,
                    record.mae_per_share,
                    record.mfe_per_share,
                    record.data_quality,
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_position_excursions(
    database: Database, position_ids: list[UUID], as_of: date
) -> dict[UUID, PositionExcursionRecord]:
    """Return each requested position's latest snapshot at or before `as_of`."""
    if not position_ids:
        return {}
    placeholders = ",".join("?" for _ in position_ids)
    query = f"""
        SELECT position_id, as_of_date, mae_per_share, mfe_per_share, data_quality
        FROM position_excursions
        WHERE position_id IN ({placeholders}) AND as_of_date <= ?
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY position_id ORDER BY as_of_date DESC
        ) = 1
    """  # noqa: S608 - placeholders only
    with database.connect() as conn:
        rows = conn.execute(
            query, [*(str(position_id) for position_id in position_ids), as_of]
        ).fetchall()
    return {
        row[0]: PositionExcursionRecord(row[0], row[1], row[2], row[3], row[4])
        for row in rows
    }


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
                virtual_fill_price = EXCLUDED.virtual_fill_price
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


def get_decision_history(
    database: Database,
    symbol: str,
    strategy_key: str,
    before_date: date,
    limit: int,
) -> list[DecisionHistoryEntry]:
    """Return bounded prior live decisions with realized paper returns."""
    if limit <= 0:
        return []
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT t.run_id, r.run_date, t.symbol, t.strategy_key, t.decision,
                   t.reason_memo, t.virtual_fill_price,
                   CASE
                     WHEN p.status = 'closed' AND p.entry_price > 0
                          AND p.close_price IS NOT NULL
                     THEN (p.close_price - p.entry_price) / p.entry_price
                     ELSE NULL
                   END AS realized_return_pct
            FROM trades_journal AS t
            JOIN runs AS r ON r.run_id = t.run_id
            LEFT JOIN positions AS p ON p.position_id = t.position_id
            WHERE t.symbol = ? AND t.strategy_key = ?
              AND r.mode = 'live' AND r.run_date < ?
            ORDER BY r.run_date DESC, t.created_at DESC
            LIMIT ?
            """,
            [symbol, strategy_key, before_date, limit],
        ).fetchall()
    return [
        DecisionHistoryEntry(
            run_id=row[0],
            run_date=row[1],
            symbol=row[2],
            strategy_key=row[3],
            decision=row[4],
            reason_memo=row[5],
            virtual_fill_price=row[6],
            realized_return_pct=row[7],
        )
        for row in rows
    ]


def get_candidate_strategy_keys(
    database: Database, run_id: UUID, symbol: str
) -> tuple[str, ...]:
    """Return strategies that produced `symbol` in one audited run."""
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT strategy_key FROM candidates WHERE run_id = ? AND symbol = ? "
            "ORDER BY strategy_key",
            [str(run_id), symbol],
        ).fetchall()
    return tuple(row[0] for row in rows)


def get_trade_decisions(database: Database, run_id: UUID) -> list[TradeDecisionRecord]:
    """Return all recorded decisions for a run in stable display order."""
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, symbol, strategy_key, position_id, decision,
                   reason_memo, virtual_fill_price
            FROM trades_journal
            WHERE run_id = ?
            ORDER BY symbol, strategy_key
            """,
            [str(run_id)],
        ).fetchall()
    return [
        TradeDecisionRecord(
            run_id=row[0],
            symbol=row[1],
            strategy_key=row[2],
            position_id=row[3],
            decision=row[4],
            reason_memo=row[5],
            virtual_fill_price=row[6],
        )
        for row in rows
    ]


def get_run_report_path(database: Database, run_id: UUID) -> Path | None:
    """Return a run's generated artifact path, when recorded."""
    with database.connect() as conn:
        row = conn.execute(
            "SELECT report_path FROM runs WHERE run_id = ?", [str(run_id)]
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return Path(row[0])


def get_latest_run_report_path(database: Database) -> Path | None:
    """Return the newest completed run artifact recorded in this database."""
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT report_path
            FROM runs
            WHERE report_path IS NOT NULL AND completed_at IS NOT NULL
            ORDER BY completed_at DESC, started_at DESC, run_id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return Path(row[0])
