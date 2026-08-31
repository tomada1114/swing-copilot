"""The dashboard's only data access: thin, read-only `research` wrappers.

Every function here opens nothing itself. It calls `swing_copilot.research`,
whose accessors open a `read_only=True` DuckDB connection for exactly one
query and close it before returning — the discipline that keeps the file lock
held for milliseconds, so a browser tab left open overnight can never block
the unattended 18:30 run. Consequences, all deliberate:

* No connection, cursor, or `Database` is stored on the app, cached, or held
  across a request. A page issues several short queries instead.
* No DataFrame cache either. Every request re-reads; the volumes are a few
  thousand rows.
* `research.ensure_views()` is never called from here. It opens a read-write
  connection to run DDL, which is exactly what this process must not do. A
  database predating the views surfaces as a `ResearchError` that the routes
  turn into an instruction to run `ensure_views()` from a separate shell.

Functions return DataFrames unchanged. Interpreting them — aggregating the
scorecard's (verdict x horizon) grain, resolving what each NULL means,
stratifying by `recommendation` — belongs to `dashboard/viewmodels/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from swing_copilot import research
from swing_copilot.report.incomplete_runs import (
    IncompleteRunKind,
    find_incomplete_runs,
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.verdict_records import (
    ACCOUNT_INDEPENDENT_EXPORT_SINCE,
    ACCOUNT_INDEPENDENT_VERDICT_CUTOFF,
    reason_text_visible_sql,
)

if TYPE_CHECKING:
    import pandas as pd

#: Explicit column projections keep a schema addition from silently widening
#: a page. Every name below is read from a `v_*` view in `storage/schema.py`.
_CANDIDATE_COLUMNS = (
    "symbol, strategy_key, rank, score, score_rsi_pullback, score_trend_quality, "
    "score_liquidity, score_atr_pct, score_pivot_proximity, score_rs_percentile, "
    "score_criteria_met, execution_state, execution_distance, "
    "rsi14, sma50, sma200, atr14, close, avg_volume, signal_names"
)
_SCORECARD_COLUMNS = (
    "symbol, strategy_key, recommendation, no_trade, news_supply_level, "
    "horizon_days, forward_return_pct, classification, rank, score, "
    "score_rsi_pullback, score_trend_quality, score_liquidity, score_atr_pct, "
    "score_pivot_proximity, score_rs_percentile, score_criteria_met, "
    "execution_state, execution_distance, rsi14, atr14, close, avg_volume, "
    "risk_status, binding_constraint, position_status, exit_reason, "
    "realized_return_pct, days_held, gics_sector"
)


def runs(db_path: Path) -> pd.DataFrame:
    """Every recorded run, newest `run_date` (then latest start) first."""
    frame = research.runs(db_path=db_path)
    if frame.empty:
        return frame
    return frame.iloc[::-1].reset_index(drop=True)


def regime_snapshots(db_path: Path) -> pd.DataFrame:
    """Every run's deterministic market-regime snapshot, oldest first."""
    return research.regime_snapshots(db_path=db_path)


def candidates_for_run(db_path: Path, run_id: str) -> pd.DataFrame:
    """`v_candidates` rows for one run, in rank order."""
    return research.query(
        f"SELECT {_CANDIDATE_COLUMNS} FROM v_candidates "  # noqa: S608 - fixed column list, no interpolated input
        "WHERE run_id = ? ORDER BY strategy_key, rank",
        [run_id],
        db_path=db_path,
    )


def scorecard_for_run(db_path: Path, run_id: str) -> pd.DataFrame:
    """`v_verdict_scorecard` rows for one run, at its (verdict x horizon) grain."""
    return research.query(
        f"SELECT {_SCORECARD_COLUMNS} FROM v_verdict_scorecard "  # noqa: S608 - fixed column list, no interpolated input
        "WHERE run_id = ? ORDER BY symbol, horizon_days",
        [run_id],
        db_path=db_path,
    )


def reasons_for_symbol(db_path: Path, run_id: str, symbol: str) -> pd.DataFrame:
    """`v_verdict_reasons` rows for one symbol, in the order they were written.

    Gated by `reason_text_visible_sql()` (Issue #389, a pure relaxation of
    #385's plain `run_date >= ACCOUNT_INDEPENDENT_VERDICT_CUTOFF` filter): a
    run whose `runs.started_at` is on or after `ACCOUNT_INDEPENDENT_EXPORT_SINCE`
    ran account-independent code and wrote an account-independent export no
    matter how early `--as-of` dated it, so an `--as-of` replay of an old
    date is no longer withheld. A verdict that genuinely ran before Issue
    #352 -- both `started_at` and `run_date` predate the two cutoffs -- is
    still withheld: `verdict_reasons.text` is never rewritten to remove a
    reader-account mention, so the dashboard withholds it here instead, the
    same predicate `get_prior_verdicts` gates re-injection on.
    """
    predicate = reason_text_visible_sql()
    return research.query(
        "SELECT reason_index, text, basis, source_id_count FROM v_verdict_reasons "  # noqa: S608 - fixed predicate, no interpolated input
        f"WHERE run_id = ? AND symbol = ? AND {predicate} "
        "ORDER BY reason_index",
        [
            run_id,
            symbol,
            ACCOUNT_INDEPENDENT_EXPORT_SINCE,
            ACCOUNT_INDEPENDENT_VERDICT_CUTOFF,
        ],
        db_path=db_path,
    )


def rejections_for_run(db_path: Path, run_id: str) -> pd.DataFrame:
    """Screening rejections recorded for one run.

    Read through the `screening_rejections` accessor and filtered in pandas
    rather than in SQL: `screening_rejections` is a plain table, and the
    dashboard's SQL escape hatch is reserved for the `v_*` views so that no
    join or as-of rule is ever restated here.
    """
    frame = research.screening_rejections(db_path=db_path)
    if frame.empty:
        return frame
    return frame[frame["run_id"].astype(str) == run_id].reset_index(drop=True)


def scorecard(db_path: Path) -> pd.DataFrame:
    """The whole verdict scorecard, for the history page."""
    return research.scorecard(db_path=db_path)


def tracked_positions(db_path: Path) -> pd.DataFrame:
    """Every virtual position the tracking ledger carries."""
    return research.tracked_positions(db_path=db_path)


def analysis_missing_run_ids(db_path: Path, reports_root: Path) -> frozenset[str]:
    """Run IDs whose deterministic pipeline finished but whose analysis did not.

    Delegates to `report/incomplete_runs.py`, whose primary signal is the
    filesystem (`verdicts` rows are archived a run late, so a row count
    cannot answer this). Only `ANALYSIS_MISSING` is returned: the other
    kinds are either already visible in `runs.status` or superseded by a
    sibling run, and neither is a banner the reader should act on.

    The `Database` handed over is `read_only=True`. `find_incomplete_runs`
    only ever issues one `SELECT` through it, so read-only is sufficient and
    the dashboard's no-write invariant survives the delegation.

    Args:
        db_path: DuckDB file to read.
        reports_root: The daily pipeline's output directory.

    Returns:
        Run IDs as lowercase strings; empty when the reports tree or the
        database is unreadable, since a missing banner is a better failure
        than a page that will not render at all.
    """
    if not reports_root.is_dir() or not Path(db_path).exists():
        return frozenset()
    try:
        incomplete = find_incomplete_runs(
            Database(db_path, read_only=True), reports_root
        )
    except duckdb.Error:
        return frozenset()
    return frozenset(
        str(run.run_id)
        for run in incomplete
        if run.kind is IncompleteRunKind.ANALYSIS_MISSING
    )
