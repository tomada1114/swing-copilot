"""Read-only judgment-history CLI: `copilot-history` (P1-05, D6 read side).

`copilot-decision` (`paper/cli.py`) is write-only; this module is its
read-only counterpart. It lives under `report/` rather than `paper/` because
it never writes -- mirroring this repo's convention of keeping write CLIs in
the domain that owns the write (`paper/cli.py` for `copilot-decision`) and
read-only presentation elsewhere. Every subcommand here is backed exclusively
by `storage/history_queries.py`'s `SELECT`-only functions (REQ-007).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from rich.console import Console
from rich.table import Table

from swing_copilot.clock import SystemClock
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.paper.journal import PaperJournal
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.history_queries import (
    get_rejections,
    get_run_detail,
    get_symbol_timeline,
    list_runs,
    run_exists,
)
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore

if TYPE_CHECKING:
    from swing_copilot.paper.journal import PerformanceBreakdownRow, PerformanceSummary
    from swing_copilot.storage.history_queries import RunDetail, SymbolTimeline

# REQ-002: `runs` requests more rows than exist (issue's own worked example
# asks for 50 against 5 actual rows) without erroring -- 20 is simply a
# sensible display default for an interactive terminal, not a hard cap.
_DEFAULT_RUNS_LIMIT = 20
_NO_RECORDS_MESSAGE = "記録なし"
# Wide fixed width so Rich never truncates a long cell (e.g. a reason_code or
# UUID) with an ellipsis regardless of the invoking terminal's actual size --
# same rationale as `terminal_report.py`'s tests use `width=200` for.
_CONSOLE_WIDTH = 200


class HistoryCommandError(SwingCopilotError):
    """Raised when a subcommand's `--run-id` doesn't identify a recorded run."""


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
    table.add_column("判断数", justify="right")
    for run in runs:
        table.add_row(
            str(run.run_id),
            run.run_date.isoformat(),
            str(run.candidate_count),
            str(run.rejection_count),
            str(run.decision_count),
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

    if detail.decisions:
        table = Table(title="Decisions")
        table.add_column("Symbol")
        table.add_column("Strategy")
        table.add_column("Decision")
        table.add_column("Reason")
        table.add_column("Fill", justify="right")
        for decision in detail.decisions:
            table.add_row(
                decision.symbol,
                decision.strategy_key,
                decision.decision,
                decision.reason_memo or "-",
                _fmt_money(decision.virtual_fill_price),
            )
        console.print(table)
    else:
        console.print(f"Decisions: {_NO_RECORDS_MESSAGE}")


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

    if timeline.decisions:
        table = Table(title=f"{timeline.symbol} decisions")
        table.add_column("run_date")
        table.add_column("Strategy")
        table.add_column("Decision")
        table.add_column("Reason")
        table.add_column("実現損益率", justify="right")
        for decision in timeline.decisions:
            table.add_row(
                decision.run_date.isoformat(),
                decision.strategy_key,
                decision.decision,
                decision.reason_memo or "-",
                _fmt_ratio_pct(decision.realized_return_pct),
            )
        console.print(table)
    else:
        console.print(f"Decisions: {_NO_RECORDS_MESSAGE}")


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


def _render_breakdown(
    console: Console, title: str, rows: tuple[PerformanceBreakdownRow, ...]
) -> None:
    if not rows:
        return
    table = Table(title=title)
    table.add_column("Key")
    table.add_column("Trades", justify="right")
    table.add_column("Win rate", justify="right")
    table.add_column("Avg P&L", justify="right")
    for row in rows:
        table.add_row(
            row.key,
            str(row.trade_count),
            _fmt_ratio_pct(row.win_rate),
            _fmt_money(row.avg_pnl_usd),
        )
    console.print(table)


def _render_performance(console: Console, summary: PerformanceSummary) -> None:
    console.print(f"Closed trades: {summary.closed_trade_count}")
    console.print(f"Total P&L: {_fmt_money(summary.total_pnl_usd)}")
    console.print(f"Win rate: {_fmt_ratio_pct(summary.win_rate)}")
    console.print(f"Expectancy: {_fmt_money(summary.expectancy_usd)}")
    console.print(f"Profit factor: {_fmt_score(summary.profit_factor)}")
    console.print(f"Avg R-multiple: {_fmt_score(summary.avg_r_multiple)}")
    if summary.r_multiple_omitted_warning:
        console.print(f"[yellow]{summary.r_multiple_omitted_warning}[/yellow]")
    console.print(f"SPY buy-and-hold: {_fmt_percent_points(summary.spy_return_pct)}")
    _render_breakdown(console, "By exit reason", summary.by_exit_reason)
    _render_breakdown(console, "By strategy", summary.by_strategy)


def _run_performance(
    database: Database, state_store: StateStore, console: Console
) -> None:
    market_store = MarketStore(database)
    journal = PaperJournal(state_store)
    summary = journal.summarize_performance(market_store, SystemClock().today())
    if summary.closed_trade_count == 0:
        console.print(_NO_RECORDS_MESSAGE)
        return
    _render_performance(console, summary)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="copilot-history")
    subparsers = parser.add_subparsers(dest="command", required=True)

    runs_parser = subparsers.add_parser("runs", help="直近N件のrun一覧")
    runs_parser.add_argument("--limit", type=int, default=_DEFAULT_RUNS_LIMIT)
    runs_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    run_parser = subparsers.add_parser("run", help="1件のrunの候補・リスク・判断詳細")
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    symbol_parser = subparsers.add_parser(
        "symbol", help="1銘柄の候補化・判断・結果の時系列"
    )
    symbol_parser.add_argument("symbol")
    symbol_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    rejections_parser = subparsers.add_parser("rejections", help="1件のrunの落選台帳")
    rejections_parser.add_argument("--run-id", required=True)
    rejections_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    performance_parser = subparsers.add_parser(
        "performance", help="クローズ済みペーパートレードのパフォーマンス集計"
    )
    performance_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: dispatch to the requested read-only subcommand."""
    args = _parse_args(argv)
    database = Database(args.db)
    state_store = StateStore(database)
    state_store.init_schema()
    console = Console(file=sys.stdout, width=_CONSOLE_WIDTH)
    try:
        if args.command == "runs":
            _run_runs(database, console, args.limit)
        elif args.command == "run":
            _run_run_detail(database, console, args.run_id)
        elif args.command == "symbol":
            _run_symbol(database, console, args.symbol)
        elif args.command == "rejections":
            _run_rejections(database, console, args.run_id)
        else:
            _run_performance(database, state_store, console)
    except HistoryCommandError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
