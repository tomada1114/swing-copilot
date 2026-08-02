"""`verdict_positions` / `_marks` / `_notes` repository (verdict tracking).

The ledger behind `copilot-track`: one virtual position per `proceed` verdict,
its daily marks, and the human's notes. Written only by `tracking/`.

Write discipline, following `paper_records.upsert_position_excursions`:

* One position's advance -- the position row plus every mark it produced -- is
  a single transaction. A failure part way through rolls the whole advance
  back, so a position never keeps a `last_marked_date` whose mark is missing.
* Marks and notes are correction upserts on their natural key, never
  `ON CONFLICT DO NOTHING`: re-running a day against corrected bars must
  update the stored figures rather than silently keep the stale ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from swing_copilot.storage.database import Database

_UPSERT_POSITION = """
    INSERT INTO verdict_positions (
        run_id, symbol, strategy_key, no_trade, entry_date, entry_price,
        stop_price, days_held, status, exit_date, exit_price, exit_reason,
        realized_return_pct, last_marked_date
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (run_id, symbol) DO UPDATE SET
        strategy_key = EXCLUDED.strategy_key,
        no_trade = EXCLUDED.no_trade,
        entry_date = EXCLUDED.entry_date,
        entry_price = EXCLUDED.entry_price,
        stop_price = EXCLUDED.stop_price,
        days_held = EXCLUDED.days_held,
        status = EXCLUDED.status,
        exit_date = EXCLUDED.exit_date,
        exit_price = EXCLUDED.exit_price,
        exit_reason = EXCLUDED.exit_reason,
        realized_return_pct = EXCLUDED.realized_return_pct,
        last_marked_date = EXCLUDED.last_marked_date
"""

_UPSERT_MARK = """
    INSERT INTO verdict_position_marks (
        run_id, symbol, as_of_date, close, stop_price, unrealized_return_pct
    ) VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (run_id, symbol, as_of_date) DO UPDATE SET
        close = EXCLUDED.close,
        stop_price = EXCLUDED.stop_price,
        unrealized_return_pct = EXCLUDED.unrealized_return_pct
"""

_UPSERT_NOTE = """
    INSERT INTO verdict_position_notes (run_id, symbol, note_date, note)
    VALUES (?, ?, ?, ?)
    ON CONFLICT (run_id, symbol, note_date) DO UPDATE SET
        note = EXCLUDED.note
"""

_POSITION_COLUMNS = """
    run_id, symbol, strategy_key, no_trade, entry_date, entry_price,
    stop_price, days_held, status, exit_date, exit_price, exit_reason,
    realized_return_pct, last_marked_date
"""

OPEN = "open"
CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class VerdictPosition:
    """One virtual position opened from a `proceed` verdict.

    `stop_price` is `None` while no stop could be computed at all (the risk
    assessment had none and there were too few bars for ATR(14)); the exit
    rules then fall back to max-hold only. `last_marked_date` is the last
    trading day already replayed, which is where the next update resumes.

    `no_trade` carries the verdict's own run-level flag forward: `True` means
    this symbol's `proceed` came from a run whose overall regime told the
    human not to trade that day (e.g. `CASH_PRIORITY`). The position is still
    opened and tracked -- withholding it would leave the ledger empty on any
    day the regime says no trade -- but `list`/`show` mark it so a reader
    never mistakes it for a buy that was actually put on offer.
    """

    run_id: UUID
    symbol: str
    strategy_key: str
    no_trade: bool
    entry_date: date
    entry_price: float
    stop_price: float | None
    days_held: int
    status: str
    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    realized_return_pct: float | None = None
    last_marked_date: date | None = None


@dataclass(frozen=True, slots=True)
class VerdictPositionMark:
    """One trading day's close-based snapshot of a tracked position."""

    run_id: UUID
    symbol: str
    as_of_date: date
    close: float
    stop_price: float | None
    unrealized_return_pct: float


@dataclass(frozen=True, slots=True)
class VerdictPositionNote:
    """One dated judgement memo the human (via a skill) left on a position."""

    run_id: UUID
    symbol: str
    note_date: date
    note: str


@dataclass(frozen=True, slots=True)
class TrackableVerdict:
    """A `proceed` verdict not yet tracked, with the risk row's price seeds.

    `entry_price`/`stop_price` come from `risk_assessments` and are both
    nullable: a `CASH_PRIORITY` regime or a `not_calculable` assessment leaves
    them unset, and the tracker then falls back to the bars (design 3.24).

    `no_trade` is the verdict's own run-level flag, carried through to the
    position that gets opened from it (design 3.24.3): a symbol can be
    `proceed` while its run overall was `no_trade` (e.g. `CASH_PRIORITY`), and
    the ledger tracks it anyway rather than going empty on such a day.
    """

    run_id: UUID
    symbol: str
    as_of: date
    strategy_key: str
    no_trade: bool
    entry_price: float | None
    stop_price: float | None


def get_untracked_proceed_verdicts(
    database: Database, as_of: date
) -> tuple[TrackableVerdict, ...]:
    """Return tradeable `proceed` verdicts dated `<= as_of` with no position yet.

    `no_trade` verdicts are included, not excluded: a run's overall regime
    (e.g. `CASH_PRIORITY`) telling the human not to trade that day does not
    mean an individual symbol's `proceed` is meaningless as a data point on
    the analysis's judgement quality, only that it was never actually on
    offer as a buy. `TrackableVerdict.no_trade` carries the flag through so
    the position, and later `list`/`show`, can mark it as such.

    Args:
        database: Shared DuckDB connection owner.
        as_of: Inclusive point-in-time cutoff on the verdict's run date.

    Returns:
        Rows ordered by `(as_of, run_id, symbol)`.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT v.run_id, v.symbol, v.as_of, v.strategy_key, v.no_trade,
                   ra.entry_price, ra.stop_price
            FROM verdicts v
            LEFT JOIN risk_assessments ra
              ON ra.run_id = v.run_id AND ra.symbol = v.symbol
            LEFT JOIN verdict_positions vp
              ON vp.run_id = v.run_id AND vp.symbol = v.symbol
            WHERE v.recommendation = 'proceed'
              AND v.as_of <= ?
              AND vp.run_id IS NULL
            ORDER BY v.as_of, v.run_id, v.symbol
            """,
            [as_of],
        ).fetchall()
    return tuple(
        TrackableVerdict(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            as_of=row[2],
            strategy_key=row[3],
            no_trade=bool(row[4]),
            entry_price=row[5],
            stop_price=row[6],
        )
        for row in rows
    )


def get_verdict_positions(
    database: Database, status: str | None = None
) -> tuple[VerdictPosition, ...]:
    """Return tracked positions, optionally narrowed to one status.

    Args:
        database: Shared DuckDB connection owner.
        status: `"open"`, `"closed"`, or `None` for both.

    Returns:
        Rows ordered by `(entry_date, run_id, symbol)`.
    """
    clause = "" if status is None else "WHERE status = ?"
    parameters: list[object] = [] if status is None else [status]
    query = f"""
        SELECT {_POSITION_COLUMNS}
        FROM verdict_positions
        {clause}
        ORDER BY entry_date, run_id, symbol
    """  # noqa: S608 - the only interpolation is a fixed column list / clause
    with database.connect() as conn:
        rows = conn.execute(query, parameters).fetchall()
    return tuple(_position(row) for row in rows)


def get_verdict_position(
    database: Database, run_id: UUID, symbol: str
) -> VerdictPosition | None:
    """Return one tracked position, or `None` when it was never opened."""
    query = f"""
        SELECT {_POSITION_COLUMNS}
        FROM verdict_positions
        WHERE run_id = ? AND symbol = ?
    """  # noqa: S608 - the only interpolation is a fixed column list
    with database.connect() as conn:
        row = conn.execute(query, [str(run_id), symbol]).fetchone()
    return None if row is None else _position(row)


def _position(row: Sequence[object]) -> VerdictPosition:
    """Rebuild a `VerdictPosition` from a `_POSITION_COLUMNS` row."""
    return VerdictPosition(
        run_id=UUID(str(row[0])),
        symbol=str(row[1]),
        strategy_key=str(row[2]),
        no_trade=bool(row[3]),
        entry_date=row[4],  # type: ignore[arg-type]
        entry_price=float(row[5]),  # type: ignore[arg-type]
        stop_price=None if row[6] is None else float(row[6]),  # type: ignore[arg-type]
        days_held=int(row[7]),  # type: ignore[call-overload]
        status=str(row[8]),
        exit_date=row[9],  # type: ignore[arg-type]
        exit_price=None if row[10] is None else float(row[10]),  # type: ignore[arg-type]
        exit_reason=None if row[11] is None else str(row[11]),
        realized_return_pct=(
            None if row[12] is None else float(row[12])  # type: ignore[arg-type]
        ),
        last_marked_date=row[13],  # type: ignore[arg-type]
    )


def upsert_verdict_position(
    database: Database,
    position: VerdictPosition,
    marks: Sequence[VerdictPositionMark] = (),
) -> None:
    """Atomically persist one position's advance together with its new marks.

    Args:
        database: Shared DuckDB connection owner.
        position: The position's state after the advance.
        marks: Marks produced by the same advance. Every mark must belong to
            `position`.

    Raises:
        ValueError: A mark belongs to a different `(run_id, symbol)`.
    """
    if any(
        mark.run_id != position.run_id or mark.symbol != position.symbol
        for mark in marks
    ):
        msg = "all marks must belong to the position being written"
        raise ValueError(msg)

    conn = database.connect()
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            _UPSERT_POSITION,
            [
                str(position.run_id),
                position.symbol,
                position.strategy_key,
                position.no_trade,
                position.entry_date,
                position.entry_price,
                position.stop_price,
                position.days_held,
                position.status,
                position.exit_date,
                position.exit_price,
                position.exit_reason,
                position.realized_return_pct,
                position.last_marked_date,
            ],
        )
        for mark in marks:
            conn.execute(
                _UPSERT_MARK,
                [
                    str(mark.run_id),
                    mark.symbol,
                    mark.as_of_date,
                    mark.close,
                    mark.stop_price,
                    mark.unrealized_return_pct,
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_verdict_position_marks(
    database: Database, run_id: UUID, symbol: str
) -> tuple[VerdictPositionMark, ...]:
    """Return one position's marks in trading-date order."""
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT as_of_date, close, stop_price, unrealized_return_pct
            FROM verdict_position_marks
            WHERE run_id = ? AND symbol = ?
            ORDER BY as_of_date
            """,
            [str(run_id), symbol],
        ).fetchall()
    return tuple(
        VerdictPositionMark(
            run_id=run_id,
            symbol=symbol,
            as_of_date=row[0],
            close=row[1],
            stop_price=row[2],
            unrealized_return_pct=row[3],
        )
        for row in rows
    )


def get_latest_verdict_position_marks(
    database: Database,
) -> dict[tuple[UUID, str], VerdictPositionMark]:
    """Return every tracked position's most recent mark, keyed by its identity."""
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, symbol, as_of_date, close, stop_price,
                   unrealized_return_pct
            FROM verdict_position_marks
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY run_id, symbol ORDER BY as_of_date DESC
            ) = 1
            """
        ).fetchall()
    return {
        (UUID(str(row[0])), row[1]): VerdictPositionMark(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            as_of_date=row[2],
            close=row[3],
            stop_price=row[4],
            unrealized_return_pct=row[5],
        )
        for row in rows
    }


def upsert_verdict_position_note(database: Database, note: VerdictPositionNote) -> None:
    """Correction-upsert one dated note on a tracked position."""
    with database.connect() as conn:
        conn.execute(
            _UPSERT_NOTE,
            [str(note.run_id), note.symbol, note.note_date, note.note],
        )


def get_verdict_position_notes(
    database: Database, run_id: UUID, symbol: str
) -> tuple[VerdictPositionNote, ...]:
    """Return one position's notes in date order."""
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT note_date, note
            FROM verdict_position_notes
            WHERE run_id = ? AND symbol = ?
            ORDER BY note_date
            """,
            [str(run_id), symbol],
        ).fetchall()
    return tuple(
        VerdictPositionNote(run_id=run_id, symbol=symbol, note_date=row[0], note=row[1])
        for row in rows
    )


def get_verdict_reasons_json(
    database: Database, run_id: UUID, symbol: str
) -> str | None:
    """Return the raw `verdicts.reasons_json` behind a tracked position.

    `copilot-track show` pairs the position with why the analysis said
    `proceed`; the JSON is returned unparsed because the caller only renders
    it. `None` means the verdict row is gone (a database that tracked a
    position before its verdicts were re-collected).
    """
    with database.connect() as conn:
        row = conn.execute(
            "SELECT reasons_json FROM verdicts WHERE run_id = ? AND symbol = ?",
            [str(run_id), symbol],
        ).fetchone()
    return None if row is None else str(row[0])
