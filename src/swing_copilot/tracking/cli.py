"""`copilot-track`: the verdict-tracking ledger's command line.

Four subcommands, in the order a morning review uses them:

* `update` opens a virtual position for every new verdict and carries the open
  ones forward to `--as-of`.
* `list` shows the ledger: unrealized P&L, the stop that would close each
  position, how many sessions are left before max-hold, and realized results.
* `show` pairs one position with the verdict's reasons and its daily marks.
* `stats` reports the realized record -- win rate, profit factor, expectancy,
  average R, holding period, exit-reason mix -- stratified by verdict side.

Since Issue #190 the ledger also shadow-tracks `skip` verdicts, so that
`stats` can put "buy only the proceeds" next to "buy every screened
candidate" under identical exit rules. `list` and `show` therefore default to
`--recommendation proceed`: the skip side is a research population, and a
morning review that suddenly listed every rejected candidate as a position
would read as a suggestion to buy them.

`update` is the only write here, and it writes exactly what replaying the
backtest's exit rules produces. The human judgement memos and manual closes
this CLI once accepted were removed in 2026-08: the ledger is mechanical, so
that the record it holds can be published. Existing `exit_reason = 'manual'`
rows predate that removal and are still displayed. Nothing in this CLI
rewrites configuration, code, or any deterministic screening/sizing value,
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
from swing_copilot.tracking.update import update_tracking

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from swing_copilot.storage.tracking_records import VerdictPosition

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
        "--status", choices=("open", "closed", "all"), default="all"
    )
    _add_recommendation_argument(list_parser)
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

    return parser.parse_args(argv)


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
    max_hold_days = settings.backtest.max_hold_days
    status = None if args.status == "all" else args.status
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
    """CLI entry point: dispatch to one of the four subcommands.

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
    else:
        _run_stats(state_store, args, console)


if __name__ == "__main__":  # pragma: no cover
    main()
