"""Read-only run-history CLI: `copilot-history` (P1-05, D6 read side).

Every subcommand here is backed exclusively by `storage/history_queries.py`'s
`SELECT`-only functions (REQ-007), plus the read-only `reports/` scan behind
`incomplete` (Issue #129). It lives under `report/` because it is presentation
over stored history and never writes.

The real-trade decision journal this CLI also used to read -- and its
`performance` subcommand -- went with the rest of the record feature in
2026-08; what remains is the deterministic run history.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from rich.console import Console
from rich.table import Table

from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.report.incomplete_runs import (
    ANALYSIS_INCOMPLETE_EXIT_CODE,
    IncompleteRunKind,
    find_incomplete_runs,
)
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.history_queries import (
    get_rejections,
    get_run_detail,
    get_symbol_timeline,
    list_runs,
    run_exists,
)
from swing_copilot.storage.state_store import StateStore

if TYPE_CHECKING:
    from swing_copilot.report.incomplete_runs import IncompleteRun
    from swing_copilot.storage.history_queries import RunDetail, SymbolTimeline

# REQ-002: `runs` requests more rows than exist (issue's own worked example
# asks for 50 against 5 actual rows) without erroring -- 20 is simply a
# sensible display default for an interactive terminal, not a hard cap.
_DEFAULT_RUNS_LIMIT = 20
_NO_RECORDS_MESSAGE = "記録なし"
# `pipeline/daily.py`'s default `output_dir`: where run archives are written.
_DEFAULT_REPORTS_DIR = Path("reports")
_NO_INCOMPLETE_RUNS_MESSAGE = "分析フェーズ未完のrunはありません"
_ONLY_NON_ACTIONABLE_MESSAGE = (
    "対処が必要な未完runはありません（同日重複・パイプライン未完のみ）"
)
_RESUME_HINT_MESSAGE = (
    "該当runのanalysis_input.jsonを対象に/swing-dailyの分析フェーズをやり直すこと"
)
_INCOMPLETE_KIND_LABELS = {
    IncompleteRunKind.ANALYSIS_MISSING: "分析未完",
    IncompleteRunKind.SAME_DAY_SUPERSEDED: "同日重複",
    IncompleteRunKind.PIPELINE_UNFINISHED: "パイプライン未完",
    IncompleteRunKind.RUN_ROW_MISSING: "runs行なし",
    IncompleteRunKind.HISTORICAL_REPLAY: "リプレイ",
}
# Wide fixed width so Rich never truncates a long cell (e.g. a reason_code or
# UUID) with an ellipsis regardless of the invoking terminal's actual size --
# same rationale as `terminal_report.py`'s tests use `width=200` for.
_CONSOLE_WIDTH = 200


class HistoryCommandError(SwingCopilotError):
    """Raised when a subcommand's `--run-id` doesn't identify a recorded run."""


#: The argparse convention: the message itself is the exit status (stderr, 1).
_EXIT_POLICY = ExitPolicy(errors=(HistoryCommandError,))


def _parse_run_id(value: str) -> UUID:
    """Parse `--run-id`, treating any non-UUID string as "not found" (Example 3).

    Args:
        value: Raw `--run-id` CLI argument.

    Returns:
        The parsed `UUID`.

    Raises:
        HistoryCommandError: `value` is not a syntactically valid UUID --
            handled identically to a syntactically valid but unrecorded
            UUID (both are "no such run"), so a caller never sees a raw
            `ValueError`/traceback for either case.
    """
    try:
        return UUID(value)
    except ValueError as exc:
        msg = f"指定されたrun_idは見つかりません: {value}"
        raise HistoryCommandError(msg) from exc


def _fmt_score(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def _fmt_money(value: float | None) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def _fmt_ratio_pct(value: float | None) -> str:
    """Format a 0..1 fraction (e.g. win_rate, realized_return_pct) as a percent."""
    return "N/A" if value is None else f"{value:+.2%}"


def _fmt_percent_points(value: float | None) -> str:
    """Format a value already expressed in percentage points (SPY return)."""
    return "N/A" if value is None else f"{value:+.2f}%"


def _run_runs(database: Database, console: Console, limit: int) -> None:
    runs = list_runs(database, limit)
    if not runs:
        console.print(_NO_RECORDS_MESSAGE)
        return
    table = Table(title="Runs", show_header=True, header_style="bold")
    table.add_column("run_id")
    table.add_column("run_date")
    table.add_column("候補数", justify="right")
    table.add_column("落選数", justify="right")
    for run in runs:
        table.add_row(
            str(run.run_id),
            run.run_date.isoformat(),
            str(run.candidate_count),
            str(run.rejection_count),
        )
    console.print(table)


def _render_run_detail(console: Console, detail: RunDetail) -> None:
    console.print(f"[bold]Run {detail.run_id}[/bold] ({detail.run_date.isoformat()})")

    if detail.candidates:
        table = Table(title="Candidates")
        table.add_column("Symbol")
        table.add_column("Strategy")
        table.add_column("Rank", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Signals")
        for candidate in detail.candidates:
            table.add_row(
                candidate.symbol,
                candidate.strategy_key,
                str(candidate.rank),
                _fmt_score(candidate.score),
                ", ".join(candidate.signal_names) or "-",
            )
        console.print(table)
    else:
        console.print(f"Candidates: {_NO_RECORDS_MESSAGE}")

    if detail.risk_assessments:
        table = Table(title="Risk")
        table.add_column("Symbol")
        table.add_column("Status")
        table.add_column("Shares", justify="right")
        table.add_column("Binding constraint")
        for risk in detail.risk_assessments:
            table.add_row(
                risk.symbol,
                risk.status,
                str(risk.max_shares) if risk.max_shares is not None else "-",
                risk.binding_constraint or "-",
            )
        console.print(table)
    else:
        console.print(f"Risk: {_NO_RECORDS_MESSAGE}")


def _run_run_detail(database: Database, console: Console, run_id_value: str) -> None:
    run_id = _parse_run_id(run_id_value)
    detail = get_run_detail(database, run_id)
    if detail is None:
        msg = f"指定されたrun_idは見つかりません: {run_id_value}"
        raise HistoryCommandError(msg)
    _render_run_detail(console, detail)


def _render_symbol_timeline(console: Console, timeline: SymbolTimeline) -> None:
    table = Table(title=f"{timeline.symbol} candidacy")
    table.add_column("run_date")
    table.add_column("run_id")
    table.add_column("Strategy")
    table.add_column("Rank", justify="right")
    table.add_column("Score", justify="right")
    for candidacy in timeline.candidacies:
        table.add_row(
            candidacy.run_date.isoformat(),
            str(candidacy.run_id),
            candidacy.strategy_key,
            str(candidacy.rank),
            _fmt_score(candidacy.score),
        )
    console.print(table)


def _run_symbol(database: Database, console: Console, symbol_value: str) -> None:
    symbol = symbol_value.strip().upper()
    timeline = get_symbol_timeline(database, symbol)
    if timeline is None:
        console.print(f"{symbol}の記録はありません")
        return
    _render_symbol_timeline(console, timeline)


def _run_rejections(database: Database, console: Console, run_id_value: str) -> None:
    run_id = _parse_run_id(run_id_value)
    if not run_exists(database, run_id):
        msg = f"指定されたrun_idは見つかりません: {run_id_value}"
        raise HistoryCommandError(msg)
    rejections = get_rejections(database, run_id)
    if not rejections:
        console.print(_NO_RECORDS_MESSAGE)
        return
    table = Table(title="Rejections")
    table.add_column("Symbol")
    table.add_column("Stage")
    table.add_column("Reason code")
    table.add_column("Detail")
    table.add_column("as_of")
    for rejection in rejections:
        table.add_row(
            rejection.symbol,
            rejection.stage,
            rejection.reason_code,
            json.dumps(rejection.detail, ensure_ascii=False),
            rejection.as_of.isoformat(),
        )
    console.print(table)


def _render_incomplete_runs(console: Console, runs: tuple[IncompleteRun, ...]) -> None:
    table = Table(title="分析フェーズ未完のrun")
    table.add_column("run_date")
    table.add_column("run_id")
    table.add_column("分類")
    table.add_column("runs.status")
    table.add_column("同日の完了run")
    table.add_column("パス")
    for run in runs:
        sibling = run.completed_sibling_run_id
        table.add_row(
            run.run_date.isoformat(),
            str(run.run_id),
            _INCOMPLETE_KIND_LABELS[run.kind],
            run.run_status or "-",
            "-" if sibling is None else str(sibling),
            str(run.path),
        )
    console.print(table)


def _run_incomplete(
    database: Database, console: Console, reports_dir: Path, since: date | None
) -> None:
    """Report runs whose analysis phase never finished (Issue #129).

    Raises:
        SystemExit: At least one *actionable* unfinished run was found, so
            the previous day's analysis is genuinely missing. Listing-only
            kinds (same-day duplicate, unfinished pipeline) print but exit 0.
    """
    runs = find_incomplete_runs(database, reports_dir, since=since)
    if not runs:
        console.print(_NO_INCOMPLETE_RUNS_MESSAGE)
        return
    _render_incomplete_runs(console, runs)
    actionable = tuple(run for run in runs if run.is_actionable)
    if not actionable:
        console.print(_ONLY_NON_ACTIONABLE_MESSAGE)
        return
    console.print(f"[yellow]対処が必要な未完run: {len(actionable)}件[/yellow]")
    console.print(_RESUME_HINT_MESSAGE)
    raise SystemExit(ANALYSIS_INCOMPLETE_EXIT_CODE)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="copilot-history")
    subparsers = parser.add_subparsers(dest="command", required=True)

    runs_parser = subparsers.add_parser("runs", help="直近N件のrun一覧")
    runs_parser.add_argument("--limit", type=int, default=_DEFAULT_RUNS_LIMIT)
    runs_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    run_parser = subparsers.add_parser("run", help="1件のrunの候補・リスク詳細")
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    symbol_parser = subparsers.add_parser("symbol", help="1銘柄の候補化の時系列")
    symbol_parser.add_argument("symbol")
    symbol_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    rejections_parser = subparsers.add_parser("rejections", help="1件のrunの落選台帳")
    rejections_parser.add_argument("--run-id", required=True)
    rejections_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    incomplete_parser = subparsers.add_parser(
        "incomplete",
        help=(
            "分析フェーズが完了していないrun"
            f"（要対処が1件でもあれば終了コード{ANALYSIS_INCOMPLETE_EXIT_CODE}）"
        ),
    )
    incomplete_parser.add_argument(
        "--reports-dir", type=Path, default=_DEFAULT_REPORTS_DIR
    )
    incomplete_parser.add_argument(
        "--since",
        type=date.fromisoformat,
        default=None,
        help="この日付以降のrun_dateだけを対象にする（YYYY-MM-DD）",
    )
    incomplete_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: dispatch to the requested read-only subcommand."""
    args = _parse_args(argv)
    database = Database(args.db)
    state_store = StateStore(database)
    state_store.init_schema()
    console = Console(file=sys.stdout, width=_CONSOLE_WIDTH)

    def _dispatch() -> None:
        if args.command == "runs":
            _run_runs(database, console, args.limit)
        elif args.command == "run":
            _run_run_detail(database, console, args.run_id)
        elif args.command == "symbol":
            _run_symbol(database, console, args.symbol)
        elif args.command == "rejections":
            _run_rejections(database, console, args.run_id)
        else:
            _run_incomplete(database, console, args.reports_dir, args.since)

    run_cli(_dispatch, _EXIT_POLICY)


if __name__ == "__main__":  # pragma: no cover
    main()
