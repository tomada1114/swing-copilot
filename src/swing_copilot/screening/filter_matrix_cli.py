"""`copilot-filter-matrix`: per-check screening diagnostics.

Answers "how much does each configured filter/signal reject on its own, and
how much of that rejection overlaps" for one `--as-of`, which the rejection
ledger cannot answer (it stores only each symbol's first failure).

Strictly offline. The universe comes from the persisted snapshot visible at
`--as-of` (never a Wikipedia refetch), and bars and fundamentals come from the
existing repositories with their own point-in-time cutoffs. No screening row
is written: no schema migration is run, no snapshot is refreshed, and an
absent `--db` is an error rather than a freshly created database. It is not,
however, isolated from the daily run -- `MarketStore` opens the shared DuckDB
file read-write to ensure its own `fundamentals` table and `bars` view, so
DuckDB's single-writer lock still applies and this must not be run *while*
`copilot-daily` holds the file. `--json` is the only intended write, and it
goes through the same atomic replacement helper the analysis boundary uses.

It sits beside `filter_matrix.py` (the pure core there, composition and
rendering here -- the `backtest/runner.py` + `backtest/cli.py` pairing) rather
than
under `report/`, whose CLI is defined by reading the judgment history out of
`storage/history_queries.py`. This one composes screening components instead.
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
from rich.console import Console
from rich.table import Table

from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.config import load_settings, load_strategies
from swing_copilot.exceptions import ConfigError, SwingCopilotError
from swing_copilot.io_atomic import write_json_atomically
from swing_copilot.screening.base import ScreeningInput
from swing_copilot.screening.filter_matrix import (
    FilterMatrixResult,
    StrategySelection,
    evaluate_filter_matrix,
)
from swing_copilot.screening.pipeline import PRICE_HISTORY_LOOKBACK_DAYS
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    MarketStore,
    ParquetRootNotFoundError,
    resolve_parquet_root,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import UniverseFetchOptions, select_persisted_universe

if TYPE_CHECKING:
    import pandas as pd

    from swing_copilot.config import Settings, StrategiesConfig
    from swing_copilot.screening.filter_matrix import CheckStats

DEFAULT_SETTINGS_PATH = "config/settings.yaml"
DEFAULT_STRATEGIES_PATH = "config/strategies.yaml"
DEFAULT_STRATEGY_KEY = "default"

# Same fixed width as `report/history_cli.py`: Rich must never ellipsize a
# check name because of the invoking terminal's actual size.
_CONSOLE_WIDTH = 200


class FilterMatrixCliError(SwingCopilotError):
    """Raised for argument/strategy/universe errors, before anything is rendered."""


#: An unusable settings/strategies file: one line on stderr, exit 1.
_CONFIG_EXIT = ExitPolicy(errors=(ConfigError,), code=1)
#: A bad argument: the argparse convention (message as the exit status).
_ARGUMENT_EXIT = ExitPolicy(errors=(FilterMatrixCliError,))

#: What a bars-root-less matrix produced instead of failing (Issue #221).
_MISSING_BARS_CONSEQUENCE = (
    "このまま実行してもバー系チェックが全銘柄データ不足となり、"
    "設定した閾値ではなく手元の欠測を測った表を出してしまう。"
)


@dataclass(frozen=True, slots=True)
class MeasuredUniverse:
    """The screening input actually measured, plus what was left out of it."""

    data: ScreeningInput
    #: Members of the `--as-of` snapshot, before dropping the ones the local
    #: store holds neither bars nor filings for.
    snapshot_size: int

    @property
    def unstored_count(self) -> int:
        """Snapshot members excluded for having no locally stored data at all."""
        return self.snapshot_size - len(self.data.universe)


def _parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(
        prog="copilot-filter-matrix",
        description=(
            "設定済みのフィルタとシグナルを1つずつ全ユニバースへ独立に適用し、"
            "単独通過率・同時落選・単独ボトルネックを集計する"
            "（診断のみ。スクリーニング結果はDBに書かない）。"
        ),
    )
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY_KEY)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--strategies", default=DEFAULT_STRATEGIES_PATH)
    parser.add_argument(
        "--json", dest="json_path", type=Path, help="機械可読な集計の書き出し先"
    )
    return parser.parse_args(argv)


def _stored_symbols(frame: pd.DataFrame) -> set[str]:
    """Symbols the local store actually returned rows for."""
    if frame.empty or "symbol" not in frame.columns:
        return set()
    return set(frame["symbol"])


def _screening_input(args: Namespace, settings: Settings) -> MeasuredUniverse:
    """Read the persisted universe, bars, and fundamentals visible at `--as-of`.

    The measured population is narrowed to snapshot members the local store
    holds *some* data for, mirroring `pipeline/daily.py`'s scoping of
    `ScreeningInput.universe` to the symbols a run actually fetched. A
    `--limit`-ed or partially backfilled database otherwise reports hundreds
    of never-fetched symbols as data gaps in every bar-based check, which
    measures local coverage rather than the configured thresholds.

    Raises:
        FilterMatrixCliError: `--db` does not exist, its sibling `bars/` does
            not exist, `--db` cannot be read, or it holds no universe snapshot
            at or before `--as-of`. Refetching one would be a network call,
            and a live membership would not be the membership that `--as-of`
            saw.
    """
    as_of: date = args.as_of
    db_path: Path = args.db
    if not db_path.exists():
        msg = (
            f"データベース {db_path} がありません。"
            "先に copilot-daily を実行してください（この診断は作成しません）。"
        )
        raise FilterMatrixCliError(msg)

    # Parquet bars live alongside the DuckDB file, mirroring
    # `backtest/cli.py`: `--db` overrides both together, never just the DB.
    # Validated before `Database` is opened, so the mistake costs neither the
    # DuckDB write lock nor a matrix nobody can trust (Issue #221).
    try:
        parquet_root = resolve_parquet_root(
            db_path, consequence=_MISSING_BARS_CONSEQUENCE
        )
    except ParquetRootNotFoundError as exc:
        raise FilterMatrixCliError(str(exc)) from exc

    database = Database(db_path)
    market_store = MarketStore(database, parquet_root=parquet_root)
    state_store = StateStore(database)

    try:
        resolution = select_persisted_universe(
            as_of,
            state_store,
            options=UniverseFetchOptions(
                snapshot_path=settings.universe.snapshot_path,
                manual_include=settings.universe.manual_include,
                manual_exclude=settings.universe.manual_exclude,
            ),
        )
    except duckdb.Error as exc:
        # A locked file (copilot-daily is running) or a database that predates
        # the universe tables. Neither is worth a raw traceback, and neither
        # is something a read-only diagnostic may fix by migrating.
        msg = f"{db_path} を読めません: {exc}"
        raise FilterMatrixCliError(msg) from exc

    if resolution is None:
        msg = (
            f"{as_of.isoformat()} 以前のユニバーススナップショットがありません。"
            "先に copilot-daily を実行してスナップショットを保存してください。"
        )
        raise FilterMatrixCliError(msg)

    symbols = [member.symbol for member in resolution.members]
    try:
        fundamentals = market_store.read_fundamentals(as_of)
        # The same rolling window the daily screening step reads, so the
        # diagnostic measures the history the pipeline actually screens on.
        bars = market_store.read_bars(
            symbols,
            as_of - timedelta(days=PRICE_HISTORY_LOOKBACK_DAYS),
            as_of,
            as_of,
        )
    except duckdb.Error as exc:
        msg = f"{db_path} を読めません: {exc}"
        raise FilterMatrixCliError(msg) from exc

    stored = _stored_symbols(bars) | _stored_symbols(fundamentals)
    measured = tuple(member for member in resolution.members if member.symbol in stored)
    return MeasuredUniverse(
        data=ScreeningInput(
            as_of=as_of,
            universe=measured,
            fundamentals=fundamentals,
            bars=bars,
        ),
        snapshot_size=len(resolution.members),
    )


def _select_strategy(
    args: Namespace, strategies: StrategiesConfig
) -> StrategySelection:
    """Resolve `--strategy` against `strategies.yaml`.

    Raises:
        FilterMatrixCliError: The key is not configured.
    """
    spec = strategies.strategies.get(args.strategy)
    if spec is None:
        available = ", ".join(sorted(strategies.strategies))
        msg = f"戦略 '{args.strategy}' は見つかりません。利用可能: {available}"
        raise FilterMatrixCliError(msg)
    return StrategySelection(key=args.strategy, spec=spec)


def _fmt_rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1%}"


def _co_blocked(result: FilterMatrixResult, row: str, column: str) -> int:
    """Symbols blocked by both checks, whichever configured order they came in."""
    counts = result.co_blocked_counts
    return counts.get((row, column), counts.get((column, row), 0))


def _check_row(stats: CheckStats) -> tuple[str, ...]:
    return (
        stats.name,
        stats.kind.value,
        str(stats.pass_count),
        str(stats.fail_count),
        str(stats.no_data_count),
        _fmt_rate(stats.pass_rate),
        str(stats.sole_blocker_count),
    )


def _render_checks(console: Console, result: FilterMatrixResult) -> None:
    table = Table(title="チェック別 独立通過率", header_style="bold")
    table.add_column("チェック")
    table.add_column("種別")
    table.add_column("通過", justify="right")
    table.add_column("落選", justify="right")
    table.add_column("データ不足", justify="right")
    table.add_column("通過率", justify="right")
    table.add_column("単独ボトルネック", justify="right")
    for stats in result.checks:
        table.add_row(*_check_row(stats))
    console.print(table)
    for name in result.population_dependent_checks:
        console.print(
            f"注意: {name} は母集団依存（相対強度の順位）なので、"
            "全ユニバースで測ったこの通過数は日次runの通過数とは一致しない"
        )


def _render_distribution(console: Console, result: FilterMatrixResult) -> None:
    table = Table(title="落選チェック数の分布", header_style="bold")
    table.add_column("落選チェック数", justify="right")
    table.add_column("銘柄数", justify="right")
    table.add_column("構成比", justify="right")
    total = result.universe_size
    for blocked_checks, symbol_count in result.blocked_count_distribution:
        table.add_row(
            str(blocked_checks),
            str(symbol_count),
            _fmt_rate(None if total == 0 else symbol_count / total),
        )
    console.print(table)
    console.print("0 = 全チェック通過（ランキング指標と candidate_limit の適用前）")


def _render_matrix(console: Console, result: FilterMatrixResult) -> None:
    table = Table(title="同時落選マトリクス（対角は各チェックの落選合計）")
    table.add_column("チェック")
    for stats in result.checks:
        table.add_column(stats.name, justify="right")
    for row in result.checks:
        cells = [
            str(row.blocked_count)
            if column.name == row.name
            else str(_co_blocked(result, row.name, column.name))
            for column in result.checks
        ]
        table.add_row(row.name, *cells)
    console.print(table)


def render_terminal(result: FilterMatrixResult, measured: MeasuredUniverse) -> str:
    """Render the whole diagnostic as Rich terminal text."""
    buffer = StringIO()
    console = Console(file=buffer, width=_CONSOLE_WIDTH)
    console.print(
        f"[bold]copilot-filter-matrix[/bold] strategy={result.strategy_key} "
        f"as_of={result.as_of.isoformat()} universe={result.universe_size}"
    )
    if measured.unstored_count:
        console.print(
            f"スナップショット {measured.snapshot_size} 銘柄のうち "
            f"{measured.unstored_count} 銘柄はバーもファンダも未保存のため除外"
            "（未取得の銘柄を閾値の落選として数えないため）"
        )
    _render_checks(console, result)
    _render_distribution(console, result)
    _render_matrix(console, result)
    passed = ", ".join(result.unblocked_symbols) or "なし"
    console.print(f"全チェック通過: {passed}")
    equivalent = ", ".join(result.candidate_equivalent_symbols) or "なし"
    console.print(f"候補相当（candidate_limit 適用前）: {equivalent}")
    return buffer.getvalue()


def build_payload(
    result: FilterMatrixResult, measured: MeasuredUniverse
) -> dict[str, Any]:
    """Build the `--json` document (the machine-readable form of the tables)."""
    return {
        "as_of": result.as_of.isoformat(),
        "strategy": result.strategy_key,
        "universe_size": result.universe_size,
        "snapshot_size": measured.snapshot_size,
        "unstored_symbol_count": measured.unstored_count,
        "population_dependent_checks": list(result.population_dependent_checks),
        "checks": [
            {
                "name": stats.name,
                "kind": stats.kind.value,
                "pass_count": stats.pass_count,
                "fail_count": stats.fail_count,
                "no_data_count": stats.no_data_count,
                "blocked_count": stats.blocked_count,
                "pass_rate": stats.pass_rate,
                "sole_blocker_count": stats.sole_blocker_count,
            }
            for stats in result.checks
        ],
        "blocked_count_distribution": [
            {"blocked_checks": blocked_checks, "symbol_count": symbol_count}
            for blocked_checks, symbol_count in result.blocked_count_distribution
        ],
        "co_blocked_counts": [
            {"checks": [first, second], "symbol_count": count}
            for (first, second), count in result.co_blocked_counts.items()
        ],
        "unblocked_symbols": list(result.unblocked_symbols),
        "candidate_equivalent_symbols": list(result.candidate_equivalent_symbols),
    }


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: compute the matrix, print it, optionally write JSON."""
    args = _parse_args(argv)
    settings = run_cli(lambda: load_settings(args.settings), _CONFIG_EXIT)
    strategies = run_cli(lambda: load_strategies(args.strategies), _CONFIG_EXIT)
    strategy = run_cli(lambda: _select_strategy(args, strategies), _ARGUMENT_EXIT)
    measured = run_cli(lambda: _screening_input(args, settings), _ARGUMENT_EXIT)
    # An unregistered filter/signal key, or one `rejection_classifier` has no
    # mirror for. Both are `strategies.yaml` mistakes, so they get the same
    # one-line message an unknown `--strategy` gets rather than a traceback out
    # of the middle of the evaluation.
    result = run_cli(
        lambda: evaluate_filter_matrix(measured.data, settings, strategy),
        ExitPolicy(
            errors=(KeyError, NotImplementedError),
            format_message=lambda exc: (
                f"戦略 '{strategy.key}' のチェック構成を測定できません: {exc}。"
                "strategies.yaml の filters_all / signals_all を確認してください。"
            ),
        ),
    )

    sys.stdout.write(render_terminal(result, measured))

    json_path: Path | None = args.json_path
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomically(json_path, build_payload(result, measured))
        sys.stdout.write(f"\nJSON written to {json_path}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
