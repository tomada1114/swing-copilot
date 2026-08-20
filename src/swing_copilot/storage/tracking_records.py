"""`verdict_positions` / `verdict_position_marks` repository (verdict tracking).

The ledger behind `copilot-track`: one virtual position per verdict --
`proceed` *and*, since Issue #190, `skip` -- and its daily marks. Written only
by `tracking/`. Both sides are carried under identical exit rules so "buy only
the proceeds" and "buy every screened candidate" become the same measurement
taken twice; the skip side is a research population, never a suggestion, so
the CLI keeps it out of the default view.

Everything here is mechanical: the human judgement memos this repository once
also held were removed in 2026-08, so the ledger can be published as a track
record without carrying anyone's personal trading notes.

Write discipline:

* One position's advance -- the position row plus every mark it produced -- is
  a single transaction. A failure part way through rolls the whole advance
  back, so a position never keeps a `last_marked_date` whose mark is missing.
* Marks are correction upserts on their natural key, never
  `ON CONFLICT DO NOTHING`: re-running a day against corrected bars must
  update the stored figures rather than silently keep the stale ones.
* The ledger is derived state, so a correction that removes a `proceed`
  verdict must remove what that verdict opened
  (`delete_orphaned_verdict_positions`); a position outliving its own verdict
  would keep publishing P&L as evidence about a judgement nobody made.
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
        run_id, symbol, strategy_key, recommendation, no_trade, entry_date,
        entry_price, stop_price, days_held, status, exit_date, exit_price,
        exit_reason, realized_return_pct, last_marked_date
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (run_id, symbol) DO UPDATE SET
        strategy_key = EXCLUDED.strategy_key,
        recommendation = EXCLUDED.recommendation,
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

_POSITION_COLUMNS = """
    run_id, symbol, strategy_key, recommendation, no_trade, entry_date,
    entry_price, stop_price, days_held, status, exit_date, exit_price,
    exit_reason, realized_return_pct, last_marked_date
"""

OPEN = "open"
CLOSED = "closed"

PROCEED = "proceed"
SKIP = "skip"

#: The verdict sides the ledger opens a shadow position for (Issue #190).
#: Both are carried under identical exit rules, which is the only way
#: "proceed only" and "every screened candidate" become comparable.
TRACKED_RECOMMENDATIONS = (PROCEED, SKIP)

#: Every reason `tracking/update.py` can stamp on a closed shadow position.
#: Deliberately not `backtest.metrics.EXIT_REASONS`: this ledger has no
#: end-of-backtest liquidation, and it does have a human `manual` override.
TRACKING_EXIT_REASONS = ("stop", "max_hold", "manual")


@dataclass(frozen=True, slots=True)
class VerdictPosition:
    """One virtual position opened from a verdict.

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

    `recommendation` says which side of the verdict this position shadows
    (Issue #190). A `skip` position was never on offer as a buy at all; it
    exists so the ledger can answer "what would the skipped candidates have
    done under the same exit rules", which is the counterfactual the whole
    qualitative layer is judged against. `copilot-track list`/`show` therefore
    default to `proceed` only, and everything skip-side is opt-in.
    """

    run_id: UUID
    symbol: str
    strategy_key: str
    recommendation: str
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
class TrackableVerdict:
    """A verdict not yet tracked, with the risk row's price seeds.

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
    recommendation: str
    no_trade: bool
    entry_price: float | None
    stop_price: float | None


def get_untracked_verdicts(
    database: Database,
    as_of: date,
    recommendations: Sequence[str] = TRACKED_RECOMMENDATIONS,
) -> tuple[TrackableVerdict, ...]:
    """Return verdicts dated `<= as_of` that have no shadow position yet.

    The only filter is the verdict's own recommendation (design 3.24.3): the
    ledger measures the qualitative layer's judgement, so what the risk layer
    later did with a candidate -- including rejecting it on a sector cap --
    does not change whether that judgement is worth tracking.

    Issue #190 widened `recommendations` from a hard-coded `proceed` to a
    caller-supplied set. Tracking `skip` under the *same* exit rules is what
    turns "does the verdict layer add value" from a pooled comparison of two
    differently-measured populations into an actual counterfactual, and it
    grows the sample from the accepted minority to every screened candidate.

    `no_trade` verdicts are included, not excluded: a run's overall regime
    (e.g. `CASH_PRIORITY`) telling the human not to trade that day does not
    mean an individual symbol's verdict is meaningless as a data point on the
    analysis's judgement quality, only that it was never actually on offer as
    a buy. `TrackableVerdict.no_trade` carries the flag through so the
    position, and later `list`/`show`, can mark it as such.

    Args:
        database: Shared DuckDB connection owner.
        as_of: Inclusive point-in-time cutoff on the verdict's run date.
        recommendations: Verdict sides to open positions for. An empty
            sequence selects nothing rather than everything -- "track no
            side" has to stay expressible.

    Returns:
        Rows ordered by `(as_of, run_id, symbol)`.
    """
    if not recommendations:
        return ()
    placeholders = ", ".join("?" for _ in recommendations)
    query = f"""
        SELECT v.run_id, v.symbol, v.as_of, v.strategy_key, v.recommendation,
               v.no_trade, ra.entry_price, ra.stop_price
        FROM verdicts v
        LEFT JOIN risk_assessments ra
          ON ra.run_id = v.run_id AND ra.symbol = v.symbol
        LEFT JOIN verdict_positions vp
          ON vp.run_id = v.run_id AND vp.symbol = v.symbol
        WHERE v.recommendation IN ({placeholders})
          AND v.as_of <= ?
          AND vp.run_id IS NULL
        ORDER BY v.as_of, v.run_id, v.symbol
    """  # noqa: S608 - the interpolation is a generated `?` placeholder list
    with database.connect() as conn:
        rows = conn.execute(query, [*recommendations, as_of]).fetchall()
    return tuple(
        TrackableVerdict(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            as_of=row[2],
            strategy_key=row[3],
            recommendation=str(row[4]),
            no_trade=bool(row[5]),
            entry_price=row[6],
            stop_price=row[7],
        )
        for row in rows
    )


#: `TrackableVerdict.recommendation` for a near-miss opened from
#: `screening_truncations` rather than from a verdict (Issue #188). The column
#: has no DB-level CHECK, and the tracking ledger already carries two sides
#: (`proceed` / `skip`), so a third label is what keeps them separable.
TRUNCATED_SIDE = "truncated"


def get_untracked_truncations(
    database: Database, as_of: date
) -> tuple[TrackableVerdict, ...]:
    """Return truncated candidates dated `<= as_of` with no shadow position yet.

    The extension point Issue #188's DoD asks for, and only that:
    `tracking/update.py` does not call this yet, so no truncated position is
    opened today. It exists because the shape of the answer is the whole
    question -- once these rows come back as `TrackableVerdict`s, applying
    the ledger's 2.5xATR / 25-session exit rules to the near-misses is a
    matter of concatenating them onto `get_untracked_verdicts`' result, with
    `_seed_position`'s existing fallbacks (close on `as_of` for the entry, an
    ATR-derived stop) covering the fact that a truncated symbol never reached
    the risk layer and therefore has no `risk_assessments` row.

    Deliberately *not* filtered to one strategy: a symbol truncated under two
    strategies on the same day is one virtual position either way, which is
    why the natural key is `(run_id, symbol)` here as in `verdict_positions`.

    Args:
        database: Shared DuckDB connection owner.
        as_of: Inclusive point-in-time cutoff on the truncation's `as_of`.

    Returns:
        Rows ordered by `(as_of, run_id, symbol)`, each with
        `recommendation=TRUNCATED_SIDE`, `no_trade=False`, and no
        risk-layer entry/stop price.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT t.run_id, t.symbol, min(t.as_of) AS as_of,
                   min(t.strategy_key) AS strategy_key
            FROM screening_truncations t
            LEFT JOIN verdict_positions vp
              ON vp.run_id = t.run_id AND vp.symbol = t.symbol
            WHERE t.as_of <= ?
              AND vp.run_id IS NULL
            GROUP BY t.run_id, t.symbol
            ORDER BY as_of, t.run_id, t.symbol
            """,
            [as_of],
        ).fetchall()
    return tuple(
        TrackableVerdict(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            as_of=row[2],
            strategy_key=row[3],
            recommendation=TRUNCATED_SIDE,
            no_trade=False,
            entry_price=None,
            stop_price=None,
        )
        for row in rows
    )


_ORPHANED_POSITIONS = """
    SELECT vp.run_id, vp.symbol
    FROM verdict_positions vp
    LEFT JOIN verdicts v
      ON v.run_id = vp.run_id
     AND v.symbol = vp.symbol
    WHERE v.run_id IS NULL
    ORDER BY vp.entry_date, vp.run_id, vp.symbol
"""

_DRIFTED_POSITIONS = """
    SELECT vp.run_id, vp.symbol, v.recommendation
    FROM verdict_positions vp
    JOIN verdicts v
      ON v.run_id = vp.run_id AND v.symbol = vp.symbol
    WHERE COALESCE(vp.recommendation, 'proceed') <> v.recommendation
    ORDER BY vp.entry_date, vp.run_id, vp.symbol
"""


def sync_verdict_position_recommendations(
    database: Database,
) -> tuple[tuple[UUID, str, str], ...]:
    """Realign each tracked position with its verdict's current recommendation.

    Before Issue #190 only `proceed` was tracked, so a symbol demoted to
    `skip` by a re-ingested `analysis_result.json` simply became an orphan and
    was deleted. Now that both sides are shadow-tracked under identical exit
    rules, deleting would throw away a position whose replay is still exactly
    right -- the entry is the same run's close either way -- and would silently
    shrink the skip sample every time an analysis was corrected. The ledger is
    derived state, so the stored side follows its source instead.

    Args:
        database: Shared DuckDB connection owner.

    Returns:
        `(run_id, symbol, new_recommendation)` for each realigned position,
        ordered by `(entry_date, run_id, symbol)`, so the caller can report
        what moved. Empty when nothing drifted, which costs one read.
    """
    conn = database.connect()
    try:
        drifted = tuple(
            (UUID(str(row[0])), str(row[1]), str(row[2]))
            for row in conn.execute(_DRIFTED_POSITIONS).fetchall()
        )
        if not drifted:
            return ()
        conn.execute("BEGIN TRANSACTION")
        try:
            for run_id, symbol, recommendation in drifted:
                conn.execute(
                    "UPDATE verdict_positions SET recommendation = ? "
                    "WHERE run_id = ? AND symbol = ?",
                    [recommendation, str(run_id), symbol],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()
    return drifted


def delete_orphaned_verdict_positions(
    database: Database,
) -> tuple[tuple[UUID, str], ...]:
    """Drop every tracked position whose verdict row no longer exists.

    Re-ingesting a corrected `analysis_result.json` replaces a run's verdicts
    wholesale (`verdict_records.replace_run_verdicts`), so a symbol dropped
    from the result entirely leaves behind a position that nothing else would
    ever close. Left alone it keeps being advanced and keeps being listed,
    attributing a P&L to a judgement the analysis has since retracted, with
    `show` unable to print a single reason for it.

    A symbol that merely moved between `proceed` and `skip` is *not* an orphan
    since Issue #190 -- both sides are tracked, so the row is realigned by
    `sync_verdict_position_recommendations` rather than destroyed.

    The position and its marks go in one transaction: a position whose marks
    survived it would reappear in `list`'s "last close" column without a row
    to explain itself.

    Args:
        database: Shared DuckDB connection owner.

    Returns:
        The deleted positions' identities, ordered by `(entry_date, run_id,
        symbol)`, so the caller can report what it removed.
    """
    conn = database.connect()
    try:
        orphans = tuple(
            (UUID(str(row[0])), str(row[1]))
            for row in conn.execute(_ORPHANED_POSITIONS).fetchall()
        )
        if not orphans:
            return ()
        conn.execute("BEGIN TRANSACTION")
        for run_id, symbol in orphans:
            for table in ("verdict_position_marks", "verdict_positions"):
                conn.execute(
                    f"DELETE FROM {table} WHERE run_id = ? AND symbol = ?",  # noqa: S608 - fixed table names
                    [str(run_id), symbol],
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return orphans


def get_verdict_positions(
    database: Database,
    status: str | None = None,
    recommendations: Sequence[str] | None = None,
) -> tuple[VerdictPosition, ...]:
    """Return tracked positions, optionally narrowed by status and verdict side.

    Args:
        database: Shared DuckDB connection owner.
        status: `"open"`, `"closed"`, or `None` for both.
        recommendations: Verdict sides to include, or `None` for every side.
            An empty sequence selects nothing. Matched against
            `COALESCE(recommendation, 'proceed')`, so a row written before
            Issue #190 added the column still answers to `"proceed"`.

    Returns:
        Rows ordered by `(entry_date, run_id, symbol)`.
    """
    if recommendations is not None and not recommendations:
        return ()
    clauses: list[str] = []
    parameters: list[object] = []
    if status is not None:
        clauses.append("status = ?")
        parameters.append(status)
    if recommendations is not None:
        placeholders = ", ".join("?" for _ in recommendations)
        clauses.append(f"COALESCE(recommendation, '{PROCEED}') IN ({placeholders})")
        parameters.extend(recommendations)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT {_POSITION_COLUMNS}
        FROM verdict_positions
        {where}
        ORDER BY entry_date, run_id, symbol
    """  # noqa: S608 - interpolation is a fixed column list and `?` placeholders
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
    """Rebuild a `VerdictPosition` from a `_POSITION_COLUMNS` row.

    A `NULL` `recommendation` reads as `proceed`: the column arrived with
    Issue #190, and every row that predates it could only have been opened
    from a `proceed` verdict.
    """
    return VerdictPosition(
        run_id=UUID(str(row[0])),
        symbol=str(row[1]),
        strategy_key=str(row[2]),
        recommendation=PROCEED if row[3] is None else str(row[3]),
        no_trade=bool(row[4]),
        entry_date=row[5],  # type: ignore[arg-type]
        entry_price=float(row[6]),  # type: ignore[arg-type]
        stop_price=None if row[7] is None else float(row[7]),  # type: ignore[arg-type]
        days_held=int(row[8]),  # type: ignore[call-overload]
        status=str(row[9]),
        exit_date=row[10],  # type: ignore[arg-type]
        exit_price=None if row[11] is None else float(row[11]),  # type: ignore[arg-type]
        exit_reason=None if row[12] is None else str(row[12]),
        realized_return_pct=(
            None if row[13] is None else float(row[13])  # type: ignore[arg-type]
        ),
        last_marked_date=row[14],  # type: ignore[arg-type]
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
                position.recommendation,
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


def get_earliest_verdict_position_marks(
    database: Database,
) -> dict[tuple[UUID, str], VerdictPositionMark]:
    """Return every tracked position's *first* mark, keyed by its identity.

    The mirror of `get_latest_verdict_position_marks`, and the ledger's only
    record of the stop a position was opened under: `verdict_positions.
    stop_price` ratchets upward as the trailing stop moves, while the seed
    mark written on the entry session keeps the original. R-multiples measure
    the risk taken at entry (Issue #190), so they have to read it from here.

    Args:
        database: Shared DuckDB connection owner.

    Returns:
        `{(run_id, symbol): mark}` over every position that has any mark.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, symbol, as_of_date, close, stop_price,
                   unrealized_return_pct
            FROM verdict_position_marks
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY run_id, symbol ORDER BY as_of_date ASC
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
