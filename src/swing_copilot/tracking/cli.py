"""`copilot-track`: the verdict-tracking ledger's command line.

Five subcommands, in the order a morning review uses them:

* `update` opens a virtual position for every new `proceed` verdict and
  carries the open ones forward to `--as-of`.
* `list` shows the ledger: unrealized P&L, the stop that would close each
  position, how many sessions are left before max-hold, and realized results.
* `show` pairs one position with the verdict's reasons, its daily marks, and
  its notes.
* `close` records a human overriding the mechanical exit rules.
* `note` records a dated judgement memo.

`close` and `note` are the only writes a skill may make here. Nothing in this
CLI rewrites configuration, code, or any deterministic screening/sizing value,
and none of it reaches the network.
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

from swing_copilot.clock import SystemClock
from swing_copilot.config import Settings, load_settings
from swing_copilot.exceptions import ConfigError
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.storage.tracking_records import CLOSED, OPEN
from swing_copilot.tracking.update import (
    TrackingError,
    close_manually,
    record_note,
    update_tracking,
)

if TYPE_CHECKING:
    from swing_copilot.storage.tracking_records import VerdictPosition

DEFAULT_SETTINGS_PATH = "config/settings.yaml"

# Same fixed width as `report/history_cli.py`: Rich must never ellipsize a
# UUID or a price because of the invoking terminal's actual size.
_CONSOLE_WIDTH = 200
_NOT_AVAILABLE = "—"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="copilot-track",
        description=(
            "proceed verdict の仮想ポジションを日次で追跡する。"
            "設定・コードの書き換えは一切行わない。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    update_parser = subparsers.add_parser(
        "update", help="新規 proceed を建玉し、保有中を as_of まで前進させる"
    )
    update_parser.add_argument("--as-of", type=date.fromisoformat)
    update_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    update_parser.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)

    list_parser = subparsers.add_parser("list", help="追跡中・手仕舞い済みを一覧する")
    list_parser.add_argument(
        "--status", choices=("open", "closed", "all"), default="all"
    )
    list_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    list_parser.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)

    show_parser = subparsers.add_parser(
        "show", help="1銘柄の verdict 理由・日次マーク・ノートを表示する"
    )
    show_parser.add_argument("--symbol", required=True)
    show_parser.add_argument("--run-id", type=UUID)
    show_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    close_parser = subparsers.add_parser("close", help="手動で手仕舞いを記録する")
    close_parser.add_argument("--run-id", type=UUID, required=True)
    close_parser.add_argument("--symbol", required=True)
    close_parser.add_argument("--as-of", type=date.fromisoformat)
    close_parser.add_argument("--note")
    close_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    note_parser = subparsers.add_parser("note", help="判断メモを記録する")
    note_parser.add_argument("--run-id", type=UUID, required=True)
    note_parser.add_argument("--symbol", required=True)
    note_parser.add_argument("--text", required=True)
    note_parser.add_argument("--date", dest="note_date", type=date.fromisoformat)
    note_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    return parser.parse_args(argv)


def _load_settings(path: str) -> Settings:
    try:
        return load_settings(path)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc


def _market_store(state_store: StateStore, db_path: Path) -> MarketStore:
    """Bars source for `--db`, pairing the DuckDB file with its Parquet root."""
    return MarketStore(state_store.database, parquet_root=db_path.parent / "bars")


def _resolve_as_of(value: date | None) -> date:
    """Default an omitted `--as-of` to today, the one wall-clock read allowed."""
    return value if value is not None else SystemClock().today()


def _run_update(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    settings = _load_settings(args.settings)
    result = update_tracking(
        state_store,
        _market_store(state_store, args.db),
        settings.backtest,
        as_of=_resolve_as_of(args.as_of),
    )
    console.print(
        f"新規 {result.opened_count} 件 / "
        f"更新 {result.advanced_count} 件 / "
        f"手仕舞い {result.closed_count} 件"
    )
    for note in result.notes:
        console.print(f"[yellow]{note}[/yellow]")


def _fmt_price(value: float | None) -> str:
    return _NOT_AVAILABLE if value is None else f"{value:.2f}"


def _fmt_pct(value: float | None) -> str:
    return _NOT_AVAILABLE if value is None else f"{value:+.2f}%"


def _fmt_date(value: date | None) -> str:
    return _NOT_AVAILABLE if value is None else value.isoformat()


def _sorted_for_display(
    positions: tuple[VerdictPosition, ...],
) -> list[VerdictPosition]:
    """Open positions first (newest entry first), then closed (newest exit first)."""
    open_rows = sorted(
        (row for row in positions if row.status == OPEN),
        key=lambda row: (row.entry_date, row.symbol),
        reverse=True,
    )
    closed_rows = sorted(
        (row for row in positions if row.status != OPEN),
        key=lambda row: (row.exit_date or row.entry_date, row.symbol),
        reverse=True,
    )
    return [*open_rows, *closed_rows]


def _run_list(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    settings = _load_settings(args.settings)
    max_hold_days = settings.backtest.max_hold_days
    status = None if args.status == "all" else args.status
    positions = state_store.get_verdict_positions(status)
    if not positions:
        console.print("追跡中の仮想ポジションはない")
        return

    latest_marks = state_store.get_latest_verdict_position_marks()
    table = Table(title="verdict 追跡台帳")
    for column in (
        "symbol",
        "run_id",
        "entry_date",
        "entry",
        "stop",
        "last close",
        "含み損益",
        "保有/上限",
        "残",
        "exit_date",
        "理由",
        "確定損益",
    ):
        table.add_column(column)

    for position in _sorted_for_display(positions):
        mark = latest_marks.get((position.run_id, position.symbol))
        is_open = position.status == OPEN
        table.add_row(
            position.symbol,
            str(position.run_id),
            position.entry_date.isoformat(),
            _fmt_price(position.entry_price),
            _fmt_price(position.stop_price),
            _fmt_price(None if mark is None else mark.close),
            _fmt_pct(
                None if mark is None or not is_open else mark.unrealized_return_pct
            ),
            f"{position.days_held}/{max_hold_days}",
            (
                str(max(max_hold_days - position.days_held, 0))
                if is_open
                else _NOT_AVAILABLE
            ),
            _fmt_date(position.exit_date),
            position.exit_reason or _NOT_AVAILABLE,
            _fmt_pct(position.realized_return_pct),
        )
    console.print(table)


def _run_show(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    positions = [
        position
        for position in state_store.get_verdict_positions()
        if position.symbol == args.symbol
        and (args.run_id is None or position.run_id == args.run_id)
    ]
    if not positions:
        console.print(f"{args.symbol} の追跡ポジションはない")
        return
    for position in positions:
        _print_position_detail(state_store, position, console)


def _print_position_detail(
    state_store: StateStore, position: VerdictPosition, console: Console
) -> None:
    console.print(
        f"[bold]{position.symbol}[/bold] run={position.run_id} "
        f"strategy={position.strategy_key} status={position.status} "
        f"entry={position.entry_date.isoformat()} @ {position.entry_price:.2f}"
    )
    if position.status == CLOSED:
        console.print(
            f"  手仕舞い: {_fmt_date(position.exit_date)} "
            f"@ {_fmt_price(position.exit_price)} "
            f"({position.exit_reason}) {_fmt_pct(position.realized_return_pct)}"
        )
    for reason in _verdict_reasons(state_store, position):
        console.print(f"  verdict: {reason}")

    marks = state_store.get_verdict_position_marks(position.run_id, position.symbol)
    mark_table = Table(title=f"{position.symbol} 日次マーク")
    for column in ("date", "close", "stop", "含み損益"):
        mark_table.add_column(column)
    for mark in marks:
        mark_table.add_row(
            mark.as_of_date.isoformat(),
            _fmt_price(mark.close),
            _fmt_price(mark.stop_price),
            _fmt_pct(mark.unrealized_return_pct),
        )
    console.print(mark_table)

    notes = state_store.get_verdict_position_notes(position.run_id, position.symbol)
    for note in notes:
        console.print(f"  note {note.note_date.isoformat()}: {note.note}")


def _verdict_reasons(
    state_store: StateStore, position: VerdictPosition
) -> tuple[str, ...]:
    """Return the verdict's reason texts, empty when the verdict row is gone."""
    raw = state_store.get_verdict_reasons_json(position.run_id, position.symbol)
    if raw is None:
        return ()
    reasons = json.loads(raw)
    return tuple(str(reason["text"]) for reason in reasons)


def _run_close(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    try:
        closed = close_manually(
            state_store,
            _market_store(state_store, args.db),
            run_id=args.run_id,
            symbol=args.symbol,
            as_of=_resolve_as_of(args.as_of),
            note=args.note,
        )
    except TrackingError as exc:
        raise SystemExit(str(exc)) from exc
    console.print(
        f"{closed.symbol} を {_fmt_date(closed.exit_date)} "
        f"@ {_fmt_price(closed.exit_price)} で手仕舞い "
        f"({_fmt_pct(closed.realized_return_pct)})"
    )


def _run_note(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    note_date = _resolve_as_of(args.note_date)
    try:
        record_note(
            state_store,
            run_id=args.run_id,
            symbol=args.symbol,
            note_date=note_date,
            note=args.text,
        )
    except TrackingError as exc:
        raise SystemExit(str(exc)) from exc
    console.print(f"{args.symbol} に {note_date.isoformat()} 付けのノートを記録した")


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: dispatch to one of the five subcommands.

    Args:
        argv: Argument vector, defaulting to `sys.argv[1:]`.

    Raises:
        SystemExit: Argument parsing failed, the settings file named by
            `--settings` is missing or invalid, or a manual write named a
            position that cannot be acted on.
    """
    args = _parse_args(argv)
    console = Console(file=sys.stdout, width=_CONSOLE_WIDTH)
    state_store = StateStore(Database(args.db))
    state_store.init_schema()
    if args.command == "update":
        _run_update(state_store, args, console)
    elif args.command == "list":
        _run_list(state_store, args, console)
    elif args.command == "show":
        _run_show(state_store, args, console)
    elif args.command == "close":
        _run_close(state_store, args, console)
    else:
        _run_note(state_store, args, console)


if __name__ == "__main__":  # pragma: no cover
    main()
