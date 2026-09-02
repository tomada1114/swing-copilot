"""`copilot-track`: the verdict-tracking ledger's command line.

Five subcommands, the first four in the order a morning review uses them:

* `update` opens a virtual position for every new verdict and carries the open
  ones forward to `--as-of`.
* `list` shows the ledger: unrealized P&L, the stop that would close each
  position, how many sessions are left before max-hold, and realized results.
* `show` pairs one position with the verdict's reasons and its daily marks.
* `stats` reports the realized record -- win rate, profit factor, expectancy,
  average R, holding period, exit-reason mix -- stratified by verdict side.
* `rebuild` is the repair path, run by hand after a price correction: it
  deletes one symbol's tracked positions and replays them from entry, which
  `update` deliberately never does (Issue #413).

Since Issue #190 the ledger also shadow-tracks `skip` verdicts, so that
`stats` can put "buy only the proceeds" next to "buy every screened
candidate" under identical exit rules. `list` and `show` therefore default to
`--recommendation proceed`: the skip side is a research population, and a
morning review that suddenly listed every rejected candidate as a position
would read as a suggestion to buy them.

`update` and `rebuild` are the only writes here, and both write exactly what
replaying the backtest's exit rules produces against an unconditional entry at
the run day's reference close -- this ledger measures whether a judgement was right,
not what actually got traded (design decision #327). The human judgement
memos and manual closes this CLI once accepted were removed in 2026-08: the
ledger is mechanical, so that the record it holds can be published. Existing
`exit_reason = 'manual'` rows predate that removal and are still displayed.
Nothing in this CLI rewrites configuration, code, or any deterministic
screening/sizing value, and none of it reaches the network.
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
from swing_copilot.clock import SystemClock
from swing_copilot.config import Settings, load_settings
from swing_copilot.exceptions import ConfigError
from swing_copilot.retro.aggregate import (
    ALL_RECOMMENDATIONS,
    compute_tracked_performance,
)
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    MarketStore,
    ParquetRootNotFoundError,
    resolve_parquet_root,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.storage.tracking_records import CLOSED, OPEN, PROCEED, SKIP
from swing_copilot.tracking.board import (
    build_board,
    position_records,
)
from swing_copilot.tracking.update import (
    RebuildTarget,
    rebuild_positions,
    update_tracking,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from swing_copilot.storage.tracking_records import VerdictPosition
    from swing_copilot.tracking.update import PositionSnapshot, RebuiltPosition

DEFAULT_SETTINGS_PATH = "config/settings.yaml"

# Same fixed width as `report/history_cli.py`: Rich must never ellipsize a
# UUID or a price because of the invoking terminal's actual size.
_CONSOLE_WIDTH = 200
_NOT_AVAILABLE = "—"

#: Every failure this command converts follows the argparse convention: the
#: message itself is the exit status (printed to stderr, exit 1).
_CONFIG_EXIT = ExitPolicy(errors=(ConfigError,))
_BARS_EXIT = ExitPolicy(errors=(ParquetRootNotFoundError,))

#: What a bars-root-less ledger run produced instead of failing (Issue #221).
_MISSING_BARS_CONSEQUENCE = (
    "このまま実行しても価格が1本も読めず、"
    "1件も mark/advance しないまま正常終了してしまう。"
)


def _add_recommendation_argument(parser: argparse.ArgumentParser) -> None:
    """Attach the display filter that keeps `skip` shadows out of the default view.

    Defaulting to `proceed` is deliberate (Issue #190): the skip side exists
    to be measured, not to be read as a list of positions someone might act
    on, so seeing it has to be an explicit request.
    """
    parser.add_argument(
        "--recommendation",
        choices=(PROCEED, SKIP, ALL_RECOMMENDATIONS),
        default=PROCEED,
        help=f"表示する verdict 区分（既定 {PROCEED}）",
    )


def _selected_recommendations(value: str) -> Sequence[str] | None:
    """Translate the CLI's filter into the repository's `recommendations` argument."""
    return None if value == ALL_RECOMMENDATIONS else (value,)


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
        "--status", choices=("published", "open", "closed", "all"), default="published"
    )
    _add_recommendation_argument(list_parser)
    list_parser.add_argument("--as-of", type=date.fromisoformat)
    list_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    list_parser.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)

    show_parser = subparsers.add_parser(
        "show", help="1銘柄の verdict 理由・日次マーク・ノートを表示する"
    )
    show_parser.add_argument("--symbol", required=True)
    show_parser.add_argument("--run-id", type=UUID)
    _add_recommendation_argument(show_parser)
    show_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    stats_parser = subparsers.add_parser(
        "stats", help="勝率・PF・期待値などを verdict 区分別に集計する"
    )
    stats_parser.add_argument(
        "--recommendation",
        choices=(PROCEED, SKIP, ALL_RECOMMENDATIONS),
        default=None,
        help=f"1区分だけを表示する（既定は {PROCEED}/{SKIP}/{ALL_RECOMMENDATIONS} 全て）",
    )
    stats_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    rebuild_parser = subparsers.add_parser(
        "rebuild",
        help="1銘柄の建玉を削除してエントリーから再リプレイする（価格是正後の修復）",
    )
    rebuild_parser.add_argument("--symbol", required=True)
    rebuild_parser.add_argument(
        "--run-id",
        type=UUID,
        help="1建玉だけを対象にする（省略時はその銘柄の全建玉、open/closed を問わない）",
    )
    rebuild_parser.add_argument("--as-of", type=date.fromisoformat)
    rebuild_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    rebuild_parser.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)

    args = parser.parse_args(argv)
    if (
        args.command == "list"
        and args.status == "published"
        and args.recommendation == SKIP
    ):
        parser.error("--status published は --recommendation proceed と組み合わせる")
    return args


def _load_settings(path: str) -> Settings:
    return run_cli(lambda: load_settings(path), _CONFIG_EXIT)


def _market_store(state_store: StateStore, db_path: Path) -> MarketStore:
    """Bars source for `--db`, pairing the DuckDB file with its Parquet root.

    The root's absence is fatal here (Issue #221): with no bars at all,
    `update` marks nothing and advances nothing, then reports "新規 0 / 更新 0
    / 手仕舞い 0" as if the ledger were simply quiet.
    """
    parquet_root = run_cli(
        lambda: resolve_parquet_root(db_path, consequence=_MISSING_BARS_CONSEQUENCE),
        _BARS_EXIT,
    )
    return MarketStore(state_store.database, parquet_root=parquet_root)


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
        settings.trade_plan,
        as_of=_resolve_as_of(args.as_of),
    )
    console.print(
        f"新規 {result.opened_count} 件 / "
        f"更新 {result.advanced_count} 件 / "
        f"手仕舞い {result.closed_count} 件"
    )
    for note in result.notes:
        console.print(f"[yellow]{note}[/yellow]")


def _run_rebuild(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    """Rebuild one symbol's tracked positions and print before/after per position.

    The one destructive subcommand: the named positions and their marks are
    deleted, then reopened from their `verdicts` rows and replayed. It is not
    atomic end to end -- see `tracking.update.rebuild_positions` -- so an
    interruption leaves them deleted and the next `update` reopens them.
    """
    settings = _load_settings(args.settings)
    result = rebuild_positions(
        state_store,
        _market_store(state_store, args.db),
        settings.trade_plan,
        RebuildTarget(symbol=args.symbol, run_id=args.run_id),
        as_of=_resolve_as_of(args.as_of),
    )
    if not result.positions:
        console.print(f"rebuild: {args.symbol} に対象の建玉はない")
        return
    console.print(
        f"rebuild: {args.symbol} {len(result.positions)} 建玉を再構築した"
        f"（as_of {result.as_of.isoformat()}）"
    )
    for row in result.positions:
        console.print(_rebuild_line(row))
    for note in result.update.notes:
        console.print(f"[yellow]{note}[/yellow]")


def _rebuild_line(row: RebuiltPosition) -> str:
    """One rebuilt position as `<run> <entry> <side>  before: ...  after: ...`."""
    return (
        f"{row.run_id} {row.entry_date.isoformat()} {row.recommendation}  "
        f"before: {_fmt_snapshot(row.before)}  after: {_fmt_snapshot(row.after)}"
    )


def _fmt_snapshot(snapshot: PositionSnapshot | None) -> str:
    """Render one side of the comparison: exit (or `open`) plus the return.

    `None` is the position the replay did not reopen -- its entry price could
    not be resolved -- which the accompanying note explains.
    """
    if snapshot is None:
        return "建玉されず"
    if snapshot.status != OPEN:
        return (
            f"{snapshot.exit_reason or _NOT_AVAILABLE} "
            f"{_fmt_date(snapshot.exit_date)} {_fmt_pct(snapshot.return_pct)}"
        )
    return f"open {_fmt_pct(snapshot.return_pct)}"


def _fmt_price(value: float | None) -> str:
    return _NOT_AVAILABLE if value is None else f"{value:.2f}"


def _fmt_pct(value: float | None) -> str:
    return _NOT_AVAILABLE if value is None else f"{value:+.2f}%"


def _fmt_date(value: date | None) -> str:
    return _NOT_AVAILABLE if value is None else value.isoformat()


def _newest_first(
    positions: Iterable[VerdictPosition], key: Callable[[VerdictPosition], date]
) -> list[VerdictPosition]:
    """Sort by `key` descending, breaking ties by symbol *ascending*.

    Two passes rather than one reversed compound key: reversing a
    `(date, symbol)` tuple would also reverse the symbol order, which reads
    as arbitrary in a table someone scans by ticker.
    """
    by_symbol = sorted(positions, key=lambda row: row.symbol)
    return sorted(by_symbol, key=key, reverse=True)


def _sorted_for_display(
    positions: tuple[VerdictPosition, ...],
) -> list[VerdictPosition]:
    """Open positions first (newest entry first), then closed (newest exit first)."""
    return [
        *_newest_first(
            (row for row in positions if row.status == OPEN),
            lambda row: row.entry_date,
        ),
        *_newest_first(
            (row for row in positions if row.status != OPEN),
            lambda row: row.exit_date or row.entry_date,
        ),
    ]


def _run_list(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    settings = _load_settings(args.settings)
    if args.status == "published" and args.recommendation != ALL_RECOMMENDATIONS:
        _run_published_list(state_store, args, settings, console)
        return

    status = None if args.status in ("all", "published") else args.status
    positions = state_store.get_verdict_positions(
        status, _selected_recommendations(args.recommendation)
    )
    if not positions:
        console.print("追跡中の仮想ポジションはない")
        return

    latest_marks = state_store.get_latest_verdict_position_marks()
    table = Table(title="verdict 追跡台帳")
    for column in (
        "symbol",
        "区分",
        "⚠",
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
            position.recommendation,
            "no_trade" if position.no_trade else "",
            str(position.run_id),
            position.entry_date.isoformat(),
            _fmt_price(position.entry_price),
            _fmt_price(position.stop_price),
            _fmt_price(None if mark is None else mark.close),
            _fmt_pct(
                None if mark is None or not is_open else mark.unrealized_return_pct
            ),
            f"{position.days_held}/{position.max_hold_days}",
            (
                str(max(position.max_hold_days - position.days_held, 0))
                if is_open
                else _NOT_AVAILABLE
            ),
            _fmt_date(position.exit_date),
            position.exit_reason or _NOT_AVAILABLE,
            _fmt_pct(position.realized_return_pct),
        )
    console.print(table)


def _run_published_list(
    state_store: StateStore,
    args: argparse.Namespace,
    settings: Settings,
    console: Console,
) -> None:
    """Render the shared proceed-only board with the configured retention."""
    positions = state_store.get_verdict_positions()
    latest_marks = state_store.get_latest_verdict_position_marks()
    rows = build_board(
        position_records(positions, latest_marks),
        as_of=_resolve_as_of(args.as_of),
        retention_business_days=(settings.tracking.published_retention_business_days),
    )
    if not rows:
        console.print("追跡中の仮想ポジションはない")
        return
    position_by_key = {
        (position.run_id, position.symbol): position for position in positions
    }
    table = Table(title="verdict 追跡台帳（公開一覧）")
    for column in (
        "symbol",
        "区分",
        "⚠",
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
    for row in rows:
        position = position_by_key[(row.run_id, row.symbol)]
        table.add_row(
            row.symbol,
            position.recommendation,
            "no_trade" if position.no_trade else "",
            str(row.run_id),
            row.entry_date.isoformat(),
            _fmt_price(row.entry_price),
            _fmt_price(row.stop_price),
            _fmt_price(row.last_close),
            _fmt_pct(row.unrealized_return_pct),
            f"{row.days_held}/{position.max_hold_days}",
            _NOT_AVAILABLE if row.days_remaining is None else str(row.days_remaining),
            _fmt_date(row.exit_date),
            row.exit_reason or _NOT_AVAILABLE,
            _fmt_pct(position.realized_return_pct),
        )
    console.print(table)


def _run_show(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    positions = [
        position
        for position in state_store.get_verdict_positions(
            None, _selected_recommendations(args.recommendation)
        )
        if position.symbol == args.symbol
        and (args.run_id is None or position.run_id == args.run_id)
    ]
    if not positions:
        console.print(f"{args.symbol} の追跡ポジションはない")
        return
    for position in positions:
        _print_position_detail(state_store, position, console)


def _fmt_ratio(value: float | None) -> str:
    return _NOT_AVAILABLE if value is None else f"{value:.2f}"


def _run_stats(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    """Report the ledger's realized record per verdict side (Issue #190).

    Reads the whole ledger rather than a window: `copilot-retro export` owns
    the windowed version of this same computation, and a hand-run `stats` is
    asking "what has the layer done so far", not "what did it do in the last
    90 days".
    """
    rows = compute_tracked_performance(
        state_store.get_verdict_positions(),
        state_store.get_earliest_verdict_position_marks(),
    )
    if args.recommendation is not None:
        rows = tuple(row for row in rows if row.recommendation == args.recommendation)

    table = Table(title="verdict 追跡台帳 成績（損益は % 単位）")
    for column in (
        "区分",
        "手仕舞い",
        "保有中",
        "勝率",
        "PF",
        "期待値",
        "平均R",
        "保有日数(中央値)",
        "手仕舞い理由",
    ):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row.recommendation,
            str(row.closed_count),
            str(row.open_count),
            _fmt_pct(None if row.win_rate is None else row.win_rate * 100),
            _fmt_ratio(row.profit_factor),
            _fmt_pct(row.expectancy_pct),
            _fmt_ratio(row.avg_r_multiple),
            _fmt_ratio(row.avg_holding_days),
            " / ".join(
                f"{cell.reason}={cell.count}" for cell in row.exit_reason_counts
            ),
        )
    console.print(table)
    console.print(
        "[dim]skip 群は同一の出口ルールで仮想追跡した反実仮想であり、"
        "実際に提案された建玉ではない[/dim]"
    )


def _print_position_detail(
    state_store: StateStore, position: VerdictPosition, console: Console
) -> None:
    console.print(
        f"[bold]{position.symbol}[/bold] run={position.run_id} "
        f"strategy={position.strategy_key} status={position.status} "
        f"entry={position.entry_date.isoformat()} @ {position.entry_price:.2f}"
    )
    if position.no_trade:
        console.print(
            "  [yellow]⚠ no_trade run: 銘柄単体は proceed だが、"
            "run 全体は当日エントリー非推奨だった"
            "（実際に提案された買いとは区別して読む）[/yellow]"
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


def _verdict_reasons(
    state_store: StateStore, position: VerdictPosition
) -> tuple[str, ...]:
    """Return the verdict's reason texts, empty when the verdict row is gone."""
    raw = state_store.get_verdict_reasons_json(position.run_id, position.symbol)
    if raw is None:
        return ()
    reasons = json.loads(raw)
    return tuple(str(reason["text"]) for reason in reasons)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: dispatch to one of the five subcommands.

    Args:
        argv: Argument vector, defaulting to `sys.argv[1:]`.

    Raises:
        SystemExit: Argument parsing failed, or the settings file named by
            `--settings` is missing or invalid.
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
    elif args.command == "rebuild":
        _run_rebuild(state_store, args, console)
    else:
        _run_stats(state_store, args, console)


if __name__ == "__main__":  # pragma: no cover
    main()
