"""Execution state and audit log repository (NFR-05).

`StateStore` owns every DuckDB table that is not bars/fundamentals (those
belong to `MarketStore`): universe history, run/step tracking, signals,
candidates, risk assessments, positions, the paper-trading journal, text
items, and LLM call audit records. `init_schema()` creates the full DDL from
`docs/04_detailed_design.md` 4.2 up front — later checklist items (screening,
risk, LLM, paper trading) add the write methods for their own tables as they
land, without re-touching schema creation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from swing_copilot.models import Position, RunStatus, StepStatus
from swing_copilot.storage import (
    audit_records,
    llm_records,
    paper_records,
    text_records,
)
from swing_copilot.storage.schema import INIT_SCHEMA_STATEMENTS
from swing_copilot.universe import UniverseMember

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path
    from uuid import UUID

    from swing_copilot.models import RunMode
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate, SignalHit
    from swing_copilot.storage.database import Database
    from swing_copilot.storage.llm_records import LLMCallRecord
    from swing_copilot.storage.paper_records import TradeDecisionRecord
    from swing_copilot.text.base import TextItem

_UNIVERSE_SOURCE = "wikipedia"


class StateStore:
    """Execution state and audit log repository, backed by one `Database`."""

    def __init__(self, database: Database) -> None:
        """Create the store.

        Args:
            database: Shared DuckDB connection owner.
        """
        self._database = database

    def init_schema(self) -> None:
        """Create every table this store owns (idempotent, additive only)."""
        with self._database.connect() as conn:
            for statement in INIT_SCHEMA_STATEMENTS:
                conn.execute(statement)

    def start_run(self, run_date: date, mode: RunMode, config_hash: str) -> UUID:
        """Start a new run and record it as `running`.

        Args:
            run_date: Evaluation market date for this run.
            mode: Whether this run is `live` or `dry_run`.
            config_hash: Hash of the effective configuration, for audit.

        Returns:
            The newly generated `run_id`.
        """
        run_id = uuid4()
        with self._database.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, run_date, mode, config_hash, status, started_at
                ) VALUES (?, ?, ?, ?, 'running', now())
                """,
                [str(run_id), run_date, mode.value, config_hash],
            )
        return run_id

    def complete_run(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        report_path: Path | None = None,
        error_summary: str | None = None,
    ) -> None:
        """Mark a run finished.

        Args:
            run_id: The run to complete.
            status: Final run status.
            report_path: Path to the generated report, if any.
            error_summary: Short failure summary, if the run failed.
        """
        with self._database.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = now(), report_path = ?, error_summary = ?
                WHERE run_id = ?
                """,
                [
                    status.value,
                    str(report_path) if report_path is not None else None,
                    error_summary,
                    str(run_id),
                ],
            )

    def record_run_step(
        self,
        run_id: UUID,
        step: str,
        status: StepStatus,
        detail: str | None,
        duration_s: float,
    ) -> None:
        """Upsert one step's outcome for a run.

        Args:
            run_id: The run this step belongs to.
            step: Step identifier (e.g. `"1_prices"`).
            status: Step outcome.
            detail: Free-form detail (e.g. a degraded-mode reason).
            duration_s: Step duration in seconds.
        """
        with self._database.connect() as conn:
            conn.execute(
                """
                INSERT INTO run_steps (run_id, step, status, detail, duration_s)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (run_id, step) DO UPDATE SET
                    status = EXCLUDED.status,
                    detail = EXCLUDED.detail,
                    duration_s = EXCLUDED.duration_s
                """,
                [str(run_id), step, status.value, detail, duration_s],
            )

    def upsert_position(self, position: Position) -> None:
        """Insert or update a position, keyed by `position_id`.

        Args:
            position: The position to persist.
        """
        with self._database.connect() as conn:
            conn.execute(
                """
                INSERT INTO positions (
                    position_id, symbol, is_paper, entry_date, entry_price,
                    shares, stop_price, status, close_date, close_price, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
                ON CONFLICT (position_id) DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    is_paper = EXCLUDED.is_paper,
                    entry_date = EXCLUDED.entry_date,
                    entry_price = EXCLUDED.entry_price,
                    shares = EXCLUDED.shares,
                    stop_price = EXCLUDED.stop_price,
                    status = EXCLUDED.status,
                    close_date = EXCLUDED.close_date,
                    close_price = EXCLUDED.close_price
                """,
                [
                    str(position.position_id),
                    position.symbol,
                    position.is_paper,
                    position.entry_date,
                    position.entry_price,
                    position.shares,
                    position.stop_price,
                    position.status,
                    position.close_date,
                    position.close_price,
                ],
            )

    _POSITION_COLUMNS = (
        "position_id, symbol, is_paper, entry_date, entry_price, "
        "shares, stop_price, status, close_date, close_price"
    )

    @staticmethod
    def _position_from_row(row: tuple[Any, ...]) -> Position:  # Any: untyped DuckDB row
        return Position(
            position_id=row[0],
            symbol=row[1],
            is_paper=row[2],
            entry_date=row[3],
            entry_price=row[4],
            shares=row[5],
            stop_price=row[6],
            status=row[7],
            close_date=row[8],
            close_price=row[9],
        )

    def get_open_positions(self, is_paper: bool = True) -> list[Position]:
        """Return open positions matching `is_paper`.

        Args:
            is_paper: Whether to return paper or live positions.

        Returns:
            Open positions, unordered.
        """
        with self._database.connect() as conn:
            rows = conn.execute(
                f"SELECT {self._POSITION_COLUMNS} "  # noqa: S608 - constant column list
                "FROM positions WHERE status = 'open' AND is_paper = ?",
                [is_paper],
            ).fetchall()
        return [self._position_from_row(row) for row in rows]

    def get_position(self, position_id: UUID) -> Position | None:
        """Return one position by ID, regardless of status.

        Args:
            position_id: The position to look up.

        Returns:
            The matching position, or `None` if it doesn't exist.
        """
        with self._database.connect() as conn:
            row = conn.execute(
                f"SELECT {self._POSITION_COLUMNS} "  # noqa: S608 - constant column list
                "FROM positions WHERE position_id = ?",
                [str(position_id)],
            ).fetchone()
        return None if row is None else self._position_from_row(row)

    def get_closed_positions(
        self, is_paper: bool = True, as_of: date | None = None
    ) -> list[Position]:
        """Return closed positions matching `is_paper`.

        Args:
            is_paper: Whether to return paper or live positions.
            as_of: Optional point-in-time cutoff; when given, only positions
                with `close_date <= as_of` are returned (inclusive), so a
                position closed after `as_of` never leaks into a summary
                computed for that date. `None` returns every closed position
                regardless of `close_date`.

        Returns:
            Closed positions, unordered.
        """
        query = (
            f"SELECT {self._POSITION_COLUMNS} "  # noqa: S608 - constant column list
            "FROM positions WHERE status = 'closed' AND is_paper = ?"
        )
        params: list[Any] = [is_paper]
        if as_of is not None:
            query += " AND close_date <= ?"
            params.append(as_of)
        with self._database.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._position_from_row(row) for row in rows]

    def record_trade_decision(self, record: TradeDecisionRecord) -> None:
        """Upsert a paper-trading decision, keyed by `(run_id, symbol, strategy_key)`.

        Args:
            record: The decision to persist.
        """
        paper_records.record_trade_decision(self._database, record)

    def record_universe_membership(
        self, snapshot_date: date, members: Sequence[UniverseMember]
    ) -> None:
        """Persist a universe snapshot, satisfying `universe.UniverseStateStore`.

        Args:
            snapshot_date: The as-of date this snapshot represents.
            members: Universe membership to persist.
        """
        with self._database.connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(
                    "DELETE FROM universe_membership WHERE snapshot_date = ?",
                    [snapshot_date],
                )
                for member in members:
                    conn.execute(
                        """
                        INSERT INTO universe_membership (
                            snapshot_date, symbol, source_symbol, company_name,
                            gics_sector, source
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            snapshot_date,
                            member.symbol,
                            member.source_symbol,
                            member.company_name,
                            member.gics_sector,
                            _UNIVERSE_SOURCE,
                        ],
                    )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def get_latest_universe_membership(
        self, as_of: date | None = None
    ) -> tuple[date, tuple[UniverseMember, ...]] | None:
        """Return the latest universe snapshot not after `as_of`, if any.

        Args:
            as_of: Optional point-in-time cutoff. `None` returns the newest snapshot.

        Returns:
            `(snapshot_date, members)` for the latest snapshot, or `None`.
        """
        with self._database.connect() as conn:
            if as_of is None:
                latest = conn.execute(
                    "SELECT max(snapshot_date) FROM universe_membership"
                ).fetchone()
            else:
                latest = conn.execute(
                    "SELECT max(snapshot_date) FROM universe_membership "
                    "WHERE snapshot_date <= ?",
                    [as_of],
                ).fetchone()
            if latest is None or latest[0] is None:
                return None
            snapshot_date = latest[0]

            rows = conn.execute(
                """
                SELECT symbol, company_name, gics_sector, source_symbol
                FROM universe_membership
                WHERE snapshot_date = ?
                ORDER BY symbol
                """,
                [snapshot_date],
            ).fetchall()

        members = tuple(
            UniverseMember(
                symbol=row[0],
                company_name=row[1],
                gics_sector=row[2],
                source_symbol=row[3],
            )
            for row in rows
        )
        return (snapshot_date, members)

    def record_signals(
        self, signals: Sequence[SignalHit], run_date: date, strategy_key: str
    ) -> None:
        """Upsert signal hits by their business natural key.

        Args:
            signals: Signal hits to record.
            run_date: Evaluation market date the signals were computed for.
            strategy_key: Which `strategies.yaml` entry produced them.
        """
        audit_records.record_signals(self._database, signals, run_date, strategy_key)

    def record_candidates(
        self, candidates: Sequence[Candidate], run_id: UUID, strategy_key: str
    ) -> None:
        """Record one run's ranked candidates, keyed by `(run_id, symbol, strategy_key)`.

        Args:
            candidates: Ranked candidates to record.
            run_id: The run these candidates belong to.
            strategy_key: Which `strategies.yaml` entry produced them.
        """
        audit_records.record_candidates(
            self._database, candidates, run_id, strategy_key
        )

    def record_risk_assessments(
        self, assessments: Sequence[RiskAssessment], run_id: UUID
    ) -> None:
        """Record one run's risk assessments, keyed by `(run_id, symbol)`.

        Args:
            assessments: Risk assessments to record.
            run_id: The run these assessments belong to.
        """
        audit_records.record_risk_assessments(self._database, assessments, run_id)

    def record_llm_call(self, call: LLMCallRecord) -> None:
        """Append one LLM call's audit record.

        Args:
            call: The call to record.
        """
        llm_records.record_llm_call(self._database, call)

    def get_cached_llm_response(
        self, model: str, prompt_hash: str, schema_version: int
    ) -> str | None:
        """Return the most recent successful response for this natural key.

        Args:
            model: Model ID the original call used.
            prompt_hash: Hash of the original prompt text.
            schema_version: Schema version the original call used.

        Returns:
            The cached `response_json`, or `None` if no successful call matches.
        """
        return llm_records.get_cached_response(
            self._database, model, prompt_hash, schema_version
        )

    def record_text_items(self, items: Sequence[TextItem]) -> None:
        """Persist collected text items, upserted by `source_id`.

        Args:
            items: Text items collected this run (news, filings, calendar).
        """
        text_records.record_text_items(self._database, items)

    def get_source_urls(self, source_ids: Sequence[str]) -> dict[str, str]:
        """Resolve known `source_ids` to their `source_url`.

        Args:
            source_ids: Source IDs to resolve (e.g. an LLM fact's `source_ids`).

        Returns:
            A mapping for every `source_id` with a recorded text item.
        """
        return text_records.get_source_urls(self._database, source_ids)

    def get_monthly_llm_cost(self, as_of: date) -> float:
        """Return realized LLM cost for `as_of`'s calendar month.

        Args:
            as_of: Any date within the month to total.

        Returns:
            Total realized cost in USD for that calendar month.
        """
        return llm_records.get_monthly_cost(self._database, as_of)
