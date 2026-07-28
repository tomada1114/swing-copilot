"""CLI entry point `copilot-backtest` (P2-08/P2-09/P2-10, roadmap §5 P2-08-10).

Wires the real `MarketStore`/S&P 500 universe into `backtest.runner.run_backtest`
and renders P2-07's risk-adjusted metrics to terminal (Rich) and an atomically
written markdown report, promoting the backtester from tests-only to a daily
tool (diagnosis D5's execution side). `--pessimistic` (P2-09) additionally runs
a higher-slippage scenario and renders a normal-vs-pessimistic comparison. The
`grid` subcommand (P2-10) runs a 25-cell ATR-stop x max-hold sensitivity grid
and classifies it as spike/plateau/inconclusive.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from swing_copilot.backtest.runner import (
    BacktestCostOverrides,
    BacktestDependencies,
    BacktestRequest,
    run_backtest,
)
from swing_copilot.backtest.sensitivity import (
    ATR_MULTIPLIER_PCT_GRID,
    MAX_HOLD_PCT_GRID,
    GridCell,
    grid_param_values,
    is_gray_cell,
    judge_grid,
)
from swing_copilot.config import load_settings, load_strategies
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import (
    UniverseFetchOptions,
    get_sp500_universe,
    select_persisted_universe,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.backtest.engine import BacktestResult
    from swing_copilot.backtest.sensitivity import SensitivityGridResult
    from swing_copilot.config import Settings, StrategiesConfig
    from swing_copilot.universe import UniverseMember

_DEFAULT_OUTPUT_DIR = Path("reports/backtests")
_CONSOLE_WIDTH = 200


class BacktestCliError(SwingCopilotError):
    """Raised for fail-fast argument/strategy errors, before any backtest runs."""


@dataclass(frozen=True, slots=True)
class ReportMeta:
    """Shared render context: what was backtested and any skipped symbols."""

    strategy: str
    start: date
    end: date
    missing_data_symbols: Sequence[str]


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    # Not `required=True`: with subparsers, argparse enforces a *parent*
    # parser's own required options even when a subcommand (e.g. `grid`)
    # consumes the actual values, since they're set on the shared Namespace
    # only after the parent's own requirements are checked. `_validate_args`
    # enforces presence explicitly instead, uniformly for both commands.
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="copilot-backtest")
    _add_common_args(parser)
    parser.add_argument("--pessimistic", action="store_true")

    subparsers = parser.add_subparsers(dest="command")
    grid_parser = subparsers.add_parser(
        "grid", help="パラメータ感応度グリッド（ATRストップ倍率 x 最大保有日数）"
    )
    _add_common_args(grid_parser)
    parser.set_defaults(command="run")

    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace, strategies: StrategiesConfig) -> None:
    """Fail-fast checks that must run before any I/O (REQ-021/022)."""
    missing = [
        flag
        for flag, value in (
            ("--strategy", args.strategy),
            ("--start", args.start),
            ("--end", args.end),
        )
        if value is None
    ]
    if missing:
        msg = f"必須引数が指定されていません: {', '.join(missing)}"
        raise BacktestCliError(msg)
    if args.start > args.end:
        msg = f"--start ({args.start}) は --end ({args.end}) より後ろにできません。"
        raise BacktestCliError(msg)
    if args.limit is not None and args.limit <= 0:
        msg = "--limit は1以上の整数で指定してください。"
        raise BacktestCliError(msg)
    if args.strategy not in strategies.strategies:
        available = ", ".join(sorted(strategies.strategies))
        msg = f"戦略 '{args.strategy}' は見つかりません。利用可能: {available}"
        raise BacktestCliError(msg)


def _select_symbols(universe: Sequence[UniverseMember], limit: int | None) -> list[str]:
    symbols = [member.symbol for member in universe]
    return symbols if limit is None else symbols[:limit]


def _output_path(args: argparse.Namespace) -> Path:
    output: Path | None = args.output
    if output is not None:
        return output
    return _DEFAULT_OUTPUT_DIR / f"{args.end.isoformat()}-{args.strategy}.md"


def _grid_output_path(args: argparse.Namespace) -> Path:
    output: Path | None = args.output
    if output is not None:
        return output
    return _DEFAULT_OUTPUT_DIR / f"{args.end.isoformat()}-{args.strategy}-grid.md"


def _missing_data_symbols(
    market_store: MarketStore, symbols: list[str], start: date, end: date
) -> list[str]:
    """Symbols with zero bars anywhere in [start, end] (REQ-020's fail-soft note)."""
    if not symbols:
        return []
    bars = market_store.read_bars(symbols, start, end, as_of=end)
    present = set(bars["symbol"].unique()) if not bars.empty else set()
    return sorted(set(symbols) - present)


def _atomic_write(path: Path, content: str) -> None:
    """Write `content` via a same-directory temp file + `os.replace` (REQ-008)."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(content)
        tmp_path.replace(path)
    except OSError:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _fmt_ratio(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def _fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _fmt_money(value: float | None) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


_METRIC_ROWS: tuple[tuple[str, str], ...] = (
    ("trade_count", "trade_count"),
    ("sharpe", "sharpe"),
    ("max_drawdown_pct", "max_drawdown_pct"),
    ("win_rate", "win_rate"),
    ("profit_factor", "profit_factor"),
    ("expectancy_per_trade", "expectancy_per_trade"),
    ("avg_r_multiple", "avg_r_multiple"),
    ("final_equity", "final_equity"),
    ("benchmark_final_equity", "benchmark_final_equity"),
)
_PCT_FIELDS = frozenset({"max_drawdown_pct", "win_rate"})
_MONEY_FIELDS = frozenset(
    {"expectancy_per_trade", "final_equity", "benchmark_final_equity"}
)


def _metric_value(result: BacktestResult, field: str) -> str:
    value = getattr(result, field)
    if field == "trade_count":
        return str(value)
    if field in _PCT_FIELDS:
        return _fmt_pct(value)
    if field in _MONEY_FIELDS:
        return _fmt_money(value)
    return _fmt_ratio(value)


def _equity_curve_summary_lines(result: BacktestResult) -> list[str]:
    if not result.equity_curve:
        return ["Equity curve: (no trading days)"]
    first_date, first_equity = result.equity_curve[0]
    last_date, last_equity = result.equity_curve[-1]
    peak_date, peak_equity = max(result.equity_curve, key=lambda point: point[1])
    trough_date, trough_equity = min(result.equity_curve, key=lambda point: point[1])
    return [
        f"Equity curve: {first_date.isoformat()}={first_equity:,.2f} -> "
        f"{last_date.isoformat()}={last_equity:,.2f}",
        f"  Peak: {peak_date.isoformat()}={peak_equity:,.2f}",
        f"  Trough: {trough_date.isoformat()}={trough_equity:,.2f}",
    ]


def render_terminal(result: BacktestResult, meta: ReportMeta) -> str:
    """Render `result` as Rich terminal text (REQ-007/009)."""
    buffer = StringIO()
    console = Console(file=buffer, width=_CONSOLE_WIDTH)
    console.print(
        f"[bold]copilot-backtest[/bold] strategy={meta.strategy} "
        f"{meta.start.isoformat()}..{meta.end.isoformat()}"
    )

    metrics_table = Table(title="Backtest metrics", header_style="bold")
    metrics_table.add_column("Metric")
    metrics_table.add_column("Value", justify="right")
    for label, field in _METRIC_ROWS:
        metrics_table.add_row(label, _metric_value(result, field))
    console.print(metrics_table)

    for warning in result.warnings:
        console.print(f"[yellow]{warning}[/yellow]")
    if meta.missing_data_symbols:
        console.print(
            "[yellow]データ不足のためスキップ: "
            f"{', '.join(meta.missing_data_symbols)}[/yellow]"
        )

    if result.trades:
        trades_table = Table(title="Trades")
        for column in (
            "Symbol",
            "Entry date",
            "Entry",
            "Exit date",
            "Exit",
            "Shares",
            "PnL",
            "Reason",
        ):
            trades_table.add_column(column)
        for trade in result.trades:
            trades_table.add_row(
                trade.symbol,
                trade.entry_date.isoformat(),
                f"{trade.entry_price:.2f}",
                trade.exit_date.isoformat(),
                f"{trade.exit_price:.2f}",
                str(trade.shares),
                f"{trade.pnl:,.2f}",
                trade.exit_reason,
            )
        console.print(trades_table)
    else:
        console.print("Trades: (none)")

    for line in _equity_curve_summary_lines(result):
        console.print(line)
    console.print(f"[dim]{result.survivorship_bias_note}[/dim]")

    return buffer.getvalue()


def render_markdown(result: BacktestResult, meta: ReportMeta) -> str:
    """Render `result` as a markdown report (REQ-007/009)."""
    lines = [
        f"# Backtest: {meta.strategy} ({meta.start.isoformat()} .. {meta.end.isoformat()})",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    lines += [
        f"| {label} | {_metric_value(result, field)} |" for label, field in _METRIC_ROWS
    ]
    lines.append("")

    if result.warnings:
        lines += ["## Warnings", ""]
        lines += [f"- {warning}" for warning in result.warnings]
        lines.append("")

    if meta.missing_data_symbols:
        lines += [
            "## Data quality",
            "",
            f"データ不足のためスキップ: {', '.join(meta.missing_data_symbols)}",
            "",
        ]

    lines += ["## Equity curve summary", "", *_equity_curve_summary_lines(result), ""]

    lines += ["## Trades", ""]
    if result.trades:
        lines += [
            "| Symbol | Entry date | Entry | Exit date | Exit | Shares | PnL | Reason |",
            "|---|---|---:|---|---:|---:|---:|---|",
        ]
        lines += [
            f"| {trade.symbol} | {trade.entry_date.isoformat()} | "
            f"{trade.entry_price:.2f} | {trade.exit_date.isoformat()} | "
            f"{trade.exit_price:.2f} | {trade.shares} | {trade.pnl:,.2f} | "
            f"{trade.exit_reason} |"
            for trade in result.trades
        ]
    else:
        lines.append("(no trades)")
    lines.append("")

    lines += ["## Survivorship bias", "", result.survivorship_bias_note, ""]
    return "\n".join(lines)


def render_terminal_comparison(
    normal: BacktestResult, pessimistic: BacktestResult, meta: ReportMeta
) -> str:
    """Render normal-vs-pessimistic metrics side by side (P2-09 REQ-004)."""
    buffer = StringIO()
    console = Console(file=buffer, width=_CONSOLE_WIDTH)
    console.print(
        f"[bold]copilot-backtest[/bold] strategy={meta.strategy} "
        f"{meta.start.isoformat()}..{meta.end.isoformat()} (normal vs pessimistic)"
    )

    table = Table(title="Backtest metrics: normal vs pessimistic", header_style="bold")
    table.add_column("Metric")
    table.add_column("Normal (x1.0)", justify="right")
    table.add_column("Pessimistic", justify="right")
    for label, field in _METRIC_ROWS:
        table.add_row(
            label, _metric_value(normal, field), _metric_value(pessimistic, field)
        )
    console.print(table)

    if meta.missing_data_symbols:
        console.print(
            "[yellow]データ不足のためスキップ: "
            f"{', '.join(meta.missing_data_symbols)}[/yellow]"
        )
    for warning in normal.warnings:
        console.print(f"[yellow]normal: {warning}[/yellow]")
    for warning in pessimistic.warnings:
        console.print(f"[yellow]pessimistic: {warning}[/yellow]")
    console.print(f"[dim]{normal.survivorship_bias_note}[/dim]")

    return buffer.getvalue()


def render_markdown_comparison(
    normal: BacktestResult, pessimistic: BacktestResult, meta: ReportMeta
) -> str:
    """Render normal-vs-pessimistic metrics as a markdown diff table (P2-09 REQ-004)."""
    lines = [
        f"# Backtest: {meta.strategy} ({meta.start.isoformat()} .. "
        f"{meta.end.isoformat()}) -- normal vs pessimistic",
        "",
        "## Metrics",
        "",
        "| Metric | Normal (x1.0) | Pessimistic |",
        "|---|---:|---:|",
    ]
    lines += [
        f"| {label} | {_metric_value(normal, field)} | "
        f"{_metric_value(pessimistic, field)} |"
        for label, field in _METRIC_ROWS
    ]
    lines.append("")

    if meta.missing_data_symbols:
        lines += [
            "## Data quality",
            "",
            f"データ不足のためスキップ: {', '.join(meta.missing_data_symbols)}",
            "",
        ]

    if normal.warnings or pessimistic.warnings:
        lines += ["## Warnings", ""]
        lines += [f"- normal: {warning}" for warning in normal.warnings]
        lines += [f"- pessimistic: {warning}" for warning in pessimistic.warnings]
        lines.append("")

    lines += ["## Survivorship bias", "", normal.survivorship_bias_note, ""]
    return "\n".join(lines)


def _cell_text(cell: GridCell, gray_threshold: int) -> str:
    value_text = (
        "N/A"
        if cell.expectancy_per_trade is None
        else f"${cell.expectancy_per_trade:,.2f}"
    )
    marker = " *" if is_gray_cell(cell, gray_threshold) else ""
    return f"{value_text} (n={cell.trade_count}){marker}"


def render_grid_terminal(
    grid: SensitivityGridResult, meta: ReportMeta, gray_threshold: int
) -> str:
    """Render the 5x5 sensitivity grid and its verdict as Rich terminal text (REQ-005)."""
    buffer = StringIO()
    console = Console(file=buffer, width=_CONSOLE_WIDTH)
    console.print(
        f"[bold]copilot-backtest grid[/bold] strategy={meta.strategy} "
        f"{meta.start.isoformat()}..{meta.end.isoformat()}"
    )
    console.print(f"Verdict: {grid.verdict_label}")

    table = Table(title="Sensitivity grid: expectancy_per_trade (n=trade_count)")
    table.add_column("ATR% \\ MaxHold%")
    for max_hold_pct in MAX_HOLD_PCT_GRID:
        table.add_column(str(max_hold_pct), justify="right")
    cells_by_position = {(c.atr_multiplier_pct, c.max_hold_pct): c for c in grid.cells}
    for atr_pct in ATR_MULTIPLIER_PCT_GRID:
        row = [str(atr_pct)]
        row += [
            _cell_text(cells_by_position[atr_pct, max_hold_pct], gray_threshold)
            for max_hold_pct in MAX_HOLD_PCT_GRID
        ]
        table.add_row(*row)
    console.print(table)
    console.print(f"* trade_count < {gray_threshold}: 灰色扱い（結論に使わない）")

    if meta.missing_data_symbols:
        console.print(
            "[yellow]データ不足のためスキップ: "
            f"{', '.join(meta.missing_data_symbols)}[/yellow]"
        )

    return buffer.getvalue()


def render_grid_markdown(
    grid: SensitivityGridResult, meta: ReportMeta, gray_threshold: int
) -> str:
    """Render the 5x5 sensitivity grid and its verdict as markdown (REQ-005)."""
    cells_by_position = {(c.atr_multiplier_pct, c.max_hold_pct): c for c in grid.cells}
    header = (
        "| ATR% \\ MaxHold% | "
        + " | ".join(str(pct) for pct in MAX_HOLD_PCT_GRID)
        + " |"
    )
    separator = "|---|" + "---:|" * len(MAX_HOLD_PCT_GRID)
    rows = [
        f"| {atr_pct} | "
        + " | ".join(
            _cell_text(cells_by_position[atr_pct, max_hold_pct], gray_threshold)
            for max_hold_pct in MAX_HOLD_PCT_GRID
        )
        + " |"
        for atr_pct in ATR_MULTIPLIER_PCT_GRID
    ]

    lines = [
        f"# Backtest sensitivity grid: {meta.strategy} "
        f"({meta.start.isoformat()} .. {meta.end.isoformat()})",
        "",
        f"Verdict: {grid.verdict_label}",
        "",
        header,
        separator,
        *rows,
        "",
        f"\\* trade_count < {gray_threshold}: 灰色扱い（結論に使わない）",
        "",
    ]

    if meta.missing_data_symbols:
        lines += [
            "## Data quality",
            "",
            f"データ不足のためスキップ: {', '.join(meta.missing_data_symbols)}",
            "",
        ]

    return "\n".join(lines)


def _compose_dependencies(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> tuple[BacktestDependencies, list[str], list[str]]:
    """Wire real collaborators (composition root); returns deps, symbols, missing data."""
    database = Database(args.db)
    # Parquet bars live alongside the DuckDB file, mirroring the
    # DEFAULT_DB_PATH/DEFAULT_PARQUET_ROOT pairing ("data/copilot.duckdb" +
    # "data/bars") -- `--db` overrides both together, never just the DB.
    market_store = MarketStore(database, parquet_root=Path(args.db).parent / "bars")
    state_store = StateStore(database)
    state_store.init_schema()
    universe_options = UniverseFetchOptions(
        snapshot_path=settings.universe.snapshot_path,
        manual_include=settings.universe.manual_include,
        manual_exclude=settings.universe.manual_exclude,
    )
    persisted_universe = select_persisted_universe(
        args.end, state_store, options=universe_options
    )
    universe = (
        persisted_universe.members
        if persisted_universe is not None
        else tuple(
            get_sp500_universe(
                args.end,
                options=universe_options,
            )
        )
    )
    symbols = _select_symbols(universe, args.limit)
    missing_data_symbols = _missing_data_symbols(
        market_store, symbols, args.start, args.end
    )
    deps = BacktestDependencies(
        market_store=market_store,
        universe=universe,
        settings=settings,
        strategies_config=strategies.model_dump(),
    )
    return deps, symbols, missing_data_symbols


def _run_backtest_command(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> None:
    try:
        _validate_args(args, strategies)
        deps, symbols, missing_data_symbols = _compose_dependencies(
            args, settings, strategies
        )
        request = BacktestRequest(
            symbols=symbols,
            start=args.start,
            end=args.end,
            initial_cash=settings.backtest.initial_cash_usd,
            strategy_key=args.strategy,
        )
        if args.pessimistic:
            normal_result = run_backtest(
                request, deps, BacktestCostOverrides(slippage_multiplier=1.0)
            )
            pessimistic_result = run_backtest(
                request,
                deps,
                BacktestCostOverrides(
                    slippage_multiplier=settings.backtest.pessimistic_slippage_multiplier
                ),
            )
        else:
            result = run_backtest(request, deps)
    except BacktestCliError as exc:
        raise SystemExit(str(exc)) from exc

    meta = ReportMeta(
        strategy=args.strategy,
        start=args.start,
        end=args.end,
        missing_data_symbols=missing_data_symbols,
    )
    if args.pessimistic:
        terminal_text = render_terminal_comparison(
            normal_result, pessimistic_result, meta
        )
        markdown_text = render_markdown_comparison(
            normal_result, pessimistic_result, meta
        )
    else:
        terminal_text = render_terminal(result, meta)
        markdown_text = render_markdown(result, meta)

    sys.stdout.write(terminal_text)

    output_path = _output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_path, markdown_text)
    sys.stdout.write(f"\nReport written to {output_path}\n")


def _run_grid_command(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> None:
    try:
        _validate_args(args, strategies)
        deps, symbols, missing_data_symbols = _compose_dependencies(
            args, settings, strategies
        )
    except BacktestCliError as exc:
        raise SystemExit(str(exc)) from exc

    request = BacktestRequest(
        symbols=symbols,
        start=args.start,
        end=args.end,
        initial_cash=settings.backtest.initial_cash_usd,
        strategy_key=args.strategy,
    )
    cells: list[GridCell] = []
    for atr_pct, max_hold_pct, atr_value, max_hold_value in grid_param_values(
        settings.backtest.exit_atr_multiple, settings.backtest.max_hold_days
    ):
        cell_result = run_backtest(
            request,
            deps,
            BacktestCostOverrides(
                exit_atr_multiple=atr_value, max_hold_days=max_hold_value
            ),
        )
        cells.append(
            GridCell(
                atr_multiplier_pct=atr_pct,
                max_hold_pct=max_hold_pct,
                expectancy_per_trade=cell_result.expectancy_per_trade,
                trade_count=cell_result.trade_count,
            )
        )
    grid_result = judge_grid(cells, settings.backtest)

    meta = ReportMeta(
        strategy=args.strategy,
        start=args.start,
        end=args.end,
        missing_data_symbols=missing_data_symbols,
    )
    gray_threshold = settings.backtest.insufficient_trade_count_threshold

    sys.stdout.write(render_grid_terminal(grid_result, meta, gray_threshold))

    output_path = _grid_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_path, render_grid_markdown(grid_result, meta, gray_threshold))
    sys.stdout.write(f"\nReport written to {output_path}\n")


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: parse args, run the backtest or grid, print + write the report."""
    args = _parse_args(argv)
    settings = load_settings()
    strategies = load_strategies()

    if args.command == "grid":
        _run_grid_command(args, settings, strategies)
    else:
        _run_backtest_command(args, settings, strategies)


if __name__ == "__main__":  # pragma: no cover
    main()
