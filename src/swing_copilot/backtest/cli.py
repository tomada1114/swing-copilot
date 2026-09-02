"""CLI entry point `copilot-backtest` (P2-08/P2-09/P2-10, roadmap §5 P2-08-10).

Wires the real `MarketStore`/S&P 500 universe into `backtest.runner.run_backtest`
and renders P2-07's risk-adjusted metrics to terminal (Rich) and an atomically
written markdown report, promoting the backtester from tests-only to a daily
tool (diagnosis D5's execution side). `--pessimistic` (P2-09) additionally runs
a higher-slippage scenario and renders a normal-vs-pessimistic comparison. The
`grid` subcommand (P2-10) runs a 25-cell ATR-stop x max-hold sensitivity grid
and classifies it as spike/plateau/inconclusive. `entry-grid` (Issue #357)
runs the fixed entry-limit ATR-multiple sensitivity values.
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

from swing_copilot.backtest.candidate_stream import (
    CandidateStreamError,
    compute_cache_key,
    generate_candidate_stream,
    load_candidate_stream,
    load_market_frame,
    save_candidate_stream,
)
from swing_copilot.backtest.earnings_history import (
    EARNINGS_FILING_FORMS,
    load_derived_earnings_calendar,
)
from swing_copilot.backtest.policy import (
    EntryPolicyArm,
    EntryPolicyError,
    build_entry_policy,
    parse_policy_arms,
)
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
    entry_limit_grid_values,
    grid_param_values,
    is_gray_cell,
    judge_grid,
)
from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.config import load_settings, load_strategies
from swing_copilot.exceptions import ConfigError, StorageSchemaError, SwingCopilotError
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    MarketStore,
    ParquetRootNotFoundError,
    resolve_parquet_root,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import (
    UniverseFetchOptions,
    get_sp500_universe,
    select_persisted_universe,
)
from swing_copilot.universe_sampling import select_universe_sample

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from swing_copilot.backtest.candidate_stream import CandidateStream, MarketFrame
    from swing_copilot.backtest.engine import BacktestResult
    from swing_copilot.backtest.sensitivity import SensitivityGridResult
    from swing_copilot.config import Settings, StrategiesConfig
    from swing_copilot.risk.checks import EarningsGuardInput
    from swing_copilot.universe_sampling import UniverseSample

_DEFAULT_OUTPUT_DIR = Path("reports/backtests")
# Overridable so a configuration variant can be compared against the baseline
# without editing the repository's own settings.yaml (`tracking/cli.py` sets
# the same precedent).
DEFAULT_SETTINGS_PATH = "config/settings.yaml"
# Ranking score_weights live here, not in settings.yaml, so comparing a
# weighting variant needs its own override alongside --settings.
DEFAULT_STRATEGIES_PATH = "config/strategies.yaml"
_CONSOLE_WIDTH = 200
#: `--policy` default: the pre-Issue-#184 behaviour, so an existing command
#: line keeps measuring what it used to measure.
_DEFAULT_POLICY = EntryPolicyArm.NONE.value
#: An unusable settings/strategies file: one line on stderr, exit 1.
_CONFIG_EXIT = ExitPolicy(errors=(ConfigError,), code=1)


class BacktestCliError(SwingCopilotError):
    """Raised for fail-fast argument/strategy errors, before any backtest runs."""


@dataclass(frozen=True, slots=True)
class ReportMeta:
    """Shared render context: what was backtested and any skipped symbols."""

    strategy: str
    start: date
    end: date
    missing_data_symbols: Sequence[str]
    universe_sample: UniverseSample


def _add_common_args(
    parser: argparse.ArgumentParser, *, is_subcommand: bool = False
) -> None:
    # Not `required=True`: with subparsers, argparse enforces a *parent*
    # parser's own required options even when a subcommand (e.g. `grid`)
    # consumes the actual values, since they're set on the shared Namespace
    # only after the parent's own requirements are checked. `_validate_args`
    # enforces presence explicitly instead, uniformly for both commands.
    #
    # The subcommand copy uses `SUPPRESS` for every default: argparse parses a
    # subcommand into a fresh namespace and then copies *all* of it onto the
    # shared one, so a real default here would overwrite a value the operator
    # already passed before the subcommand. `--strategy`/`--start`/`--end`
    # would merely be reset to `None` and caught by `_validate_args`, but
    # `--settings`/`--strategies` would silently snap back to the repository
    # defaults and the grid would measure the baseline while reporting the
    # variant. `SUPPRESS` leaves the key out of the sub-namespace entirely
    # unless it was actually given, so the parent's value survives.
    def default(value: object) -> object:
        return argparse.SUPPRESS if is_subcommand else value

    parser.add_argument("--strategy", default=default(None))
    parser.add_argument("--start", type=date.fromisoformat, default=default(None))
    parser.add_argument("--end", type=date.fromisoformat, default=default(None))
    parser.add_argument("--limit", type=int, default=default(None))
    parser.add_argument("--output", type=Path, default=default(None))
    parser.add_argument("--db", type=Path, default=default(DEFAULT_DB_PATH))
    parser.add_argument("--settings", default=default(DEFAULT_SETTINGS_PATH))
    parser.add_argument("--strategies", default=default(DEFAULT_STRATEGIES_PATH))
    parser.add_argument("--candidate-cache", type=Path, default=default(None))
    # Issue #184: a comma-separated list turns one invocation into an A/B over
    # the same candidate stream, which is the only way to answer "did the
    # regime gate improve the result?".
    parser.add_argument("--policy", default=default(_DEFAULT_POLICY))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="copilot-backtest")
    _add_common_args(parser)
    parser.add_argument("--pessimistic", action="store_true")

    subparsers = parser.add_subparsers(dest="command")
    grid_parser = subparsers.add_parser(
        "grid", help="パラメータ感応度グリッド（ATRストップ倍率 x 最大保有日数）"
    )
    _add_common_args(grid_parser, is_subcommand=True)
    entry_grid_parser = subparsers.add_parser(
        "entry-grid", help="指値エントリー倍率（k）の感応度グリッド"
    )
    _add_common_args(entry_grid_parser, is_subcommand=True)
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
    arms = _policy_arms(args)
    if args.pessimistic and len(arms) > 1:
        msg = (
            "--pessimistic と複数アームの --policy は同時に指定できません"
            "（比較軸が2つになり、どちらの効果か読めなくなるため）。"
        )
        raise BacktestCliError(msg)


def _policy_arms(args: argparse.Namespace) -> tuple[EntryPolicyArm, ...]:
    """Parse `--policy`, re-raising as the CLI's own fail-fast error."""
    try:
        return parse_policy_arms(args.policy)
    except EntryPolicyError as exc:
        raise BacktestCliError(str(exc)) from exc


def _reject_grid_policy(args: argparse.Namespace) -> None:
    """Refuse `grid --policy <non-default>` instead of ignoring it silently.

    Ignoring the flag would print a report labelled as one thing and measured
    as another.
    """
    if _policy_arms(args) != (EntryPolicyArm.NONE,):
        msg = (
            "grid サブコマンドは --policy に対応していません"
            f"（--policy {_DEFAULT_POLICY} のみ）。"
        )
        raise BacktestCliError(msg)


def _reject_entry_grid_policy(args: argparse.Namespace) -> None:
    """Refuse entry-grid with a non-default policy instead of ignoring it."""
    if _policy_arms(args) != (EntryPolicyArm.NONE,):
        msg = (
            "entry-grid サブコマンドは --policy に対応していません"
            f"（--policy {_DEFAULT_POLICY} のみ）。"
        )
        raise BacktestCliError(msg)


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


def _entry_grid_output_path(args: argparse.Namespace) -> Path:
    output: Path | None = args.output
    if output is not None:
        return output
    return _DEFAULT_OUTPUT_DIR / f"{args.end.isoformat()}-{args.strategy}-entry-grid.md"


def _missing_data_symbols(
    market_store: MarketStore, symbols: Sequence[str], start: date, end: date
) -> list[str]:
    """Symbols with zero bars anywhere in [start, end] (REQ-020's fail-soft note)."""
    if not symbols:
        return []
    bars = market_store.read_bars(list(symbols), start, end, as_of=end)
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
    ("avg_invested_pct", "avg_invested_pct"),
    ("max_concurrent_reached", "max_concurrent_reached"),
    ("final_equity", "final_equity"),
    ("benchmark_final_equity", "benchmark_final_equity"),
)
_PCT_FIELDS = frozenset({"max_drawdown_pct", "win_rate", "avg_invested_pct"})
_MONEY_FIELDS = frozenset(
    {"expectancy_per_trade", "final_equity", "benchmark_final_equity"}
)
_INT_FIELDS = frozenset({"trade_count", "max_concurrent_reached"})
_ENTRY_GRID_METRIC_ROWS: tuple[tuple[str, str], ...] = (
    ("trade_count", "trade_count"),
    ("expectancy_per_trade", "expectancy_per_trade"),
    ("avg_r_multiple", "avg_r_multiple"),
    ("avg_invested_pct", "avg_invested_pct"),
    ("final_equity", "final_equity"),
)


def _metric_value(result: BacktestResult, field: str) -> str:
    value = getattr(result, field)
    if field in _INT_FIELDS:
        return str(value)
    if field in _PCT_FIELDS:
        return _fmt_pct(value)
    if field in _MONEY_FIELDS:
        return _fmt_money(value)
    return _fmt_ratio(value)


def _exit_breakdown_rows(result: BacktestResult) -> list[tuple[str, str]]:
    """Label/value rows shared by the terminal and markdown exit sections."""
    rows = [(reason, str(count)) for reason, count in result.exit_reason_counts]
    rows.append(("max_hold binding rate", _fmt_pct(result.max_hold_binding_rate)))
    held = result.holding_days
    rows.append(
        ("holding days (median)", "N/A" if held is None else f"{held.median:.1f}")
    )
    rows.append(
        (
            "holding days (p25 / p75)",
            "N/A" if held is None else f"{held.p25:.1f} / {held.p75:.1f}",
        )
    )
    return rows


def _exit_breakdown_comparison_rows(
    results: Sequence[BacktestResult],
) -> list[tuple[str, list[str]]]:
    """`_exit_breakdown_rows` for every column, aligned on a shared label set.

    Both comparisons this serves (normal-vs-pessimistic and the policy A/B)
    move trades between exit reasons — a higher slippage assumption fires a
    stop that used to miss, a regime gate never opens the position at all —
    so the columns can carry different reason labels. Missing labels render as
    `0` rather than being dropped, keeping the comparison's rows one-to-one.

    Args:
        results: One result per column, in column order.

    Returns:
        `(label, [value per column])` rows, in first-seen label order.
    """
    per_column = [dict(_exit_breakdown_rows(result)) for result in results]
    labels: list[str] = []
    for rows in per_column:
        labels += [label for label in rows if label not in labels]
    return [(label, [rows.get(label, "0") for rows in per_column]) for label in labels]


def _entry_block_rows(result: BacktestResult) -> list[tuple[str, str]]:
    """Label/value rows for the "why an entry was not taken" instrumentation.

    Each row reads `<candidate-days> (<sessions>)`: the first number counts
    blocked candidates, the second the distinct sessions on which that reason
    fired at least once — a gate that blocks 40 candidates on one panicky day
    is a very different finding from one that blocks one candidate on 40 days.
    """
    days = dict(result.entry_block_days)
    return [
        (reason, f"{count} ({days.get(reason, 0)}d)")
        for reason, count in result.entry_block_counts
    ]


#: Row order of the multi-arm equity curve table, and the order
#: `_equity_curve_points` returns. `last` is deliberately absent from the
#: table: `final_equity` already carries it in `## Metrics`, and the window's
#: end date is in the report title.
_EQUITY_CURVE_POINTS: tuple[str, ...] = ("first", "peak", "trough")


def _equity_curve_points(result: BacktestResult) -> list[tuple[str, str]]:
    """First/peak/trough of the equity curve as `<date>=<equity>` cells.

    The single-arm prose block and the multi-arm table share this one
    definition of "peak" and "trough": the A/B exists to be compared against a
    single-arm run, so the two must not be able to drift apart.

    Args:
        result: One arm's result.

    Returns:
        `(point label, cell text)` rows in `_EQUITY_CURVE_POINTS` order; every
        cell is `N/A` when the arm has no trading days.
    """
    if not result.equity_curve:
        return [(label, "N/A") for label in _EQUITY_CURVE_POINTS]
    points = (
        result.equity_curve[0],
        max(result.equity_curve, key=lambda point: point[1]),
        min(result.equity_curve, key=lambda point: point[1]),
    )
    return [
        (label, f"{point_date.isoformat()}={equity:,.2f}")
        for label, (point_date, equity) in zip(
            _EQUITY_CURVE_POINTS, points, strict=True
        )
    ]


def _equity_curve_summary_lines(result: BacktestResult) -> list[str]:
    if not result.equity_curve:
        return ["Equity curve: (no trading days)"]
    first_cell, peak_cell, trough_cell = (
        cell for _, cell in _equity_curve_points(result)
    )
    last_date, last_equity = result.equity_curve[-1]
    return [
        f"Equity curve: {first_cell} -> {last_date.isoformat()}={last_equity:,.2f}",
        f"  Peak: {peak_cell}",
        f"  Trough: {trough_cell}",
    ]


def _equity_curve_comparison_rows(
    results: Sequence[BacktestResult],
) -> list[tuple[str, list[str]]]:
    """`_equity_curve_points` for every column, one row per point.

    Args:
        results: One result per column, in column order.

    Returns:
        `(point label, [cell per column])` rows.
    """
    per_column = [dict(_equity_curve_points(result)) for result in results]
    return [
        (label, [rows[label] for rows in per_column]) for label in _EQUITY_CURVE_POINTS
    ]


def _universe_console_lines(meta: ReportMeta) -> list[str]:
    """Sampling provenance, dimmed, for the terminal renderers."""
    return [f"[dim]{line}[/dim]" for line in meta.universe_sample.summary_lines()]


def _universe_markdown_lines(meta: ReportMeta) -> list[str]:
    """Sampling provenance for the top of a markdown report."""
    return [*meta.universe_sample.summary_lines(), ""]


def render_terminal(result: BacktestResult, meta: ReportMeta) -> str:
    """Render `result` as Rich terminal text (REQ-007/009)."""
    buffer = StringIO()
    console = Console(file=buffer, width=_CONSOLE_WIDTH)
    console.print(
        f"[bold]copilot-backtest[/bold] strategy={meta.strategy} "
        f"{meta.start.isoformat()}..{meta.end.isoformat()}"
    )
    for line in _universe_console_lines(meta):
        console.print(line)

    metrics_table = Table(title="Backtest metrics", header_style="bold")
    metrics_table.add_column("Metric")
    metrics_table.add_column("Value", justify="right")
    for label, field in _METRIC_ROWS:
        metrics_table.add_row(label, _metric_value(result, field))
    console.print(metrics_table)

    exit_table = Table(title="Exit breakdown", header_style="bold")
    exit_table.add_column("Exit")
    exit_table.add_column("Value", justify="right")
    for label, value in _exit_breakdown_rows(result):
        exit_table.add_row(label, value)
    console.print(exit_table)

    block_table = Table(
        title="Entry blocks: candidates (sessions)", header_style="bold"
    )
    block_table.add_column("Reason")
    block_table.add_column("Value", justify="right")
    for label, value in _entry_block_rows(result):
        block_table.add_row(label, value)
    console.print(block_table)

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
        *_universe_markdown_lines(meta),
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    lines += [
        f"| {label} | {_metric_value(result, field)} |" for label, field in _METRIC_ROWS
    ]
    lines.append("")

    lines += ["## Exit breakdown", "", "| Exit | Value |", "|---|---:|"]
    lines += [f"| {label} | {value} |" for label, value in _exit_breakdown_rows(result)]
    lines.append("")

    lines += [
        "## Entry blocks",
        "",
        "候補件数（発動セッション数）",
        "",
        "| Reason | Value |",
        "|---|---:|",
    ]
    lines += [f"| {label} | {value} |" for label, value in _entry_block_rows(result)]
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
    for line in _universe_console_lines(meta):
        console.print(line)

    table = Table(title="Backtest metrics: normal vs pessimistic", header_style="bold")
    table.add_column("Metric")
    table.add_column("Normal (x1.0)", justify="right")
    table.add_column("Pessimistic", justify="right")
    for label, field in _METRIC_ROWS:
        table.add_row(
            label, _metric_value(normal, field), _metric_value(pessimistic, field)
        )
    console.print(table)

    exit_table = Table(
        title="Exit breakdown: normal vs pessimistic", header_style="bold"
    )
    exit_table.add_column("Exit")
    exit_table.add_column("Normal (x1.0)", justify="right")
    exit_table.add_column("Pessimistic", justify="right")
    for label, values in _exit_breakdown_comparison_rows((normal, pessimistic)):
        exit_table.add_row(label, *values)
    console.print(exit_table)

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
        *_universe_markdown_lines(meta),
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

    lines += [
        "## Exit breakdown",
        "",
        "| Exit | Normal (x1.0) | Pessimistic |",
        "|---|---:|---:|",
    ]
    lines += [
        f"| {label} | " + " | ".join(values) + " |"
        for label, values in _exit_breakdown_comparison_rows((normal, pessimistic))
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


def render_policy_comparison_terminal(
    arms: Sequence[tuple[str, BacktestResult]], meta: ReportMeta
) -> str:
    """Render one metrics/gate table per policy arm, side by side (Issue #184).

    Every arm ran against the identical candidate stream, so a column-to-column
    difference is attributable to the gates alone.
    """
    buffer = StringIO()
    console = Console(file=buffer, width=_CONSOLE_WIDTH)
    labels = [label for label, _ in arms]
    results = [result for _, result in arms]
    console.print(
        f"[bold]copilot-backtest[/bold] strategy={meta.strategy} "
        f"{meta.start.isoformat()}..{meta.end.isoformat()} "
        f"(policy: {' vs '.join(labels)})"
    )
    for line in _universe_console_lines(meta):
        console.print(line)

    metrics_table = Table(title="Backtest metrics by policy", header_style="bold")
    metrics_table.add_column("Metric")
    for label in labels:
        metrics_table.add_column(label, justify="right")
    for label, field in _METRIC_ROWS:
        metrics_table.add_row(
            label, *[_metric_value(result, field) for result in results]
        )
    console.print(metrics_table)

    exit_table = Table(title="Exit breakdown by policy", header_style="bold")
    exit_table.add_column("Exit")
    for label in labels:
        exit_table.add_column(label, justify="right")
    for reason, values in _exit_breakdown_comparison_rows(results):
        exit_table.add_row(reason, *values)
    console.print(exit_table)

    block_table = Table(
        title="Entry blocks by policy: candidates (sessions)", header_style="bold"
    )
    block_table.add_column("Reason")
    for label in labels:
        block_table.add_column(label, justify="right")
    for reason, values in _entry_block_comparison_rows(arms):
        block_table.add_row(reason, *values)
    console.print(block_table)

    equity_table = Table(title="Equity curve summary by policy", header_style="bold")
    equity_table.add_column("Point")
    for label in labels:
        equity_table.add_column(label, justify="right")
    for point, values in _equity_curve_comparison_rows(results):
        equity_table.add_row(point, *values)
    console.print(equity_table)

    if meta.missing_data_symbols:
        console.print(
            "[yellow]データ不足のためスキップ: "
            f"{', '.join(meta.missing_data_symbols)}[/yellow]"
        )
    for label, result in arms:
        for warning in result.warnings:
            console.print(f"[yellow]{label}: {warning}[/yellow]")
    console.print(f"[dim]{arms[0][1].survivorship_bias_note}[/dim]")

    return buffer.getvalue()


def render_policy_comparison_markdown(
    arms: Sequence[tuple[str, BacktestResult]], meta: ReportMeta
) -> str:
    """Render the policy A/B as a markdown diff table (Issue #184)."""
    labels = [label for label, _ in arms]
    results = [result for _, result in arms]
    header = "| Metric | " + " | ".join(labels) + " |"
    separator = "|---|" + "---:|" * len(labels)
    lines = [
        f"# Backtest: {meta.strategy} ({meta.start.isoformat()} .. "
        f"{meta.end.isoformat()}) -- policy A/B",
        "",
        f"同一候補ストリームに対して {', '.join(labels)} を比較した。",
        "",
        *_universe_markdown_lines(meta),
        "## Metrics",
        "",
        header,
        separator,
    ]
    lines += [
        f"| {label} | "
        + " | ".join(_metric_value(result, field) for result in results)
        + " |"
        for label, field in _METRIC_ROWS
    ]
    lines.append("")

    lines += [
        "## Exit breakdown",
        "",
        "| Exit | " + " | ".join(labels) + " |",
        separator,
    ]
    lines += [
        f"| {reason} | " + " | ".join(values) + " |"
        for reason, values in _exit_breakdown_comparison_rows(results)
    ]
    lines.append("")

    lines += [
        "## Entry blocks",
        "",
        "候補件数（発動セッション数）",
        "",
        "| Reason | " + " | ".join(labels) + " |",
        separator,
    ]
    lines += [
        f"| {reason} | " + " | ".join(values) + " |"
        for reason, values in _entry_block_comparison_rows(arms)
    ]
    lines.append("")

    lines += [
        "## Equity curve summary",
        "",
        "| Point | " + " | ".join(labels) + " |",
        separator,
    ]
    lines += [
        f"| {point} | " + " | ".join(values) + " |"
        for point, values in _equity_curve_comparison_rows(results)
    ]
    lines.append("")

    if meta.missing_data_symbols:
        lines += [
            "## Data quality",
            "",
            f"データ不足のためスキップ: {', '.join(meta.missing_data_symbols)}",
            "",
        ]

    warning_lines = [
        f"- {label}: {warning}" for label, result in arms for warning in result.warnings
    ]
    if warning_lines:
        lines += ["## Warnings", "", *warning_lines, ""]

    lines += ["## Survivorship bias", "", arms[0][1].survivorship_bias_note, ""]
    return "\n".join(lines)


def _entry_block_comparison_rows(
    arms: Sequence[tuple[str, BacktestResult]],
) -> list[tuple[str, list[str]]]:
    """`_entry_block_rows` for every arm, aligned on a shared reason set."""
    per_arm = [dict(_entry_block_rows(result)) for _, result in arms]
    reasons: list[str] = []
    for rows in per_arm:
        reasons += [reason for reason in rows if reason not in reasons]
    return [
        (reason, [rows.get(reason, "0 (0d)") for rows in per_arm]) for reason in reasons
    ]


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
    for line in _universe_console_lines(meta):
        console.print(line)
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
        *_universe_markdown_lines(meta),
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


def render_entry_grid_terminal(
    results: Sequence[tuple[float, BacktestResult]], meta: ReportMeta
) -> str:
    """Render the entry-limit ATR-multiple sensitivity results."""
    buffer = StringIO()
    console = Console(file=buffer, width=_CONSOLE_WIDTH)
    console.print(
        f"[bold]copilot-backtest entry-grid[/bold] strategy={meta.strategy} "
        f"{meta.start.isoformat()}..{meta.end.isoformat()}"
    )
    for line in _universe_console_lines(meta):
        console.print(line)
    console.print("k = entry_limit_atr_multiple (ATR multiple)")

    table = Table(title="Entry-limit sensitivity grid")
    table.add_column("k", justify="right")
    for label, _field in _ENTRY_GRID_METRIC_ROWS:
        table.add_column(label, justify="right")
    for k_value, result in results:
        table.add_row(
            f"{k_value:.1f}",
            *[
                _metric_value(result, field)
                for _label, field in _ENTRY_GRID_METRIC_ROWS
            ],
        )
    console.print(table)

    missing_symbols = ", ".join(meta.missing_data_symbols)
    if meta.missing_data_symbols:
        console.print(f"[yellow]データ不足のためスキップ: {missing_symbols}[/yellow]")
    for k_value, result in results:
        for warning in result.warnings:
            console.print(f"[yellow]k={k_value:.1f}: {warning}[/yellow]")
    console.print(f"[dim]{results[0][1].survivorship_bias_note}[/dim]")
    return buffer.getvalue()


def render_entry_grid_markdown(
    results: Sequence[tuple[float, BacktestResult]], meta: ReportMeta
) -> str:
    """Render the entry-limit ATR-multiple sensitivity results as markdown."""
    header = (
        "| k (ATR multiple) | "
        + " | ".join(label for label, _field in _ENTRY_GRID_METRIC_ROWS)
        + " |"
    )
    separator = "|---:|" + "---:|" * len(_ENTRY_GRID_METRIC_ROWS)
    rows = [
        f"| {k_value:.1f} | "
        + " | ".join(
            _metric_value(result, field) for _label, field in _ENTRY_GRID_METRIC_ROWS
        )
        + " |"
        for k_value, result in results
    ]
    missing_symbols = ", ".join(meta.missing_data_symbols)
    lines = [
        f"# Backtest entry-limit sensitivity grid: {meta.strategy} "
        f"({meta.start.isoformat()} .. {meta.end.isoformat()})",
        "",
        *_universe_markdown_lines(meta),
        "k = entry_limit_atr_multiple (ATR multiple)",
        "",
        header,
        separator,
        *rows,
        "",
    ]
    if meta.missing_data_symbols:
        lines += [
            "## Data quality",
            "",
            f"データ不足のためスキップ: {missing_symbols}",
            "",
        ]
    warning_lines = [
        f"- k={k_value:.1f}: {warning}"
        for k_value, result in results
        for warning in result.warnings
    ]
    if warning_lines:
        lines += ["## Warnings", "", *warning_lines, ""]
    lines += ["## Survivorship bias", "", results[0][1].survivorship_bias_note, ""]
    return "\n".join(lines)


#: What a bars-root-less backtest produced instead of failing (Issue #217).
_MISSING_BARS_CONSEQUENCE = (
    "このまま実行すると全銘柄がデータ不足となり、取引ゼロのレポートを"
    "正常終了として書いてしまう。"
)


def _resolve_parquet_root(db_path: Path) -> Path:
    """Resolve `--db`'s sibling bars root, failing fast when it is absent (Issue #217).

    Thin adapter over `storage.market_store.resolve_parquet_root`, which the
    other `--db`-taking CLIs share since Issue #221: the check and its message
    are one implementation, and each command only supplies its own
    consequence sentence and converts to its own error type.

    Args:
        db_path: The `--db` value.

    Returns:
        The `bars/` directory next to `db_path`.

    Raises:
        BacktestCliError: The resolved `bars/` is not an existing directory.
    """
    try:
        return resolve_parquet_root(db_path, consequence=_MISSING_BARS_CONSEQUENCE)
    except ParquetRootNotFoundError as exc:
        raise BacktestCliError(str(exc)) from exc


def _compose_dependencies(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> tuple[BacktestDependencies, UniverseSample, list[str]]:
    """Wire real collaborators (composition root); returns deps, sample, missing data."""
    parquet_root = _resolve_parquet_root(Path(args.db))
    db_path = Path(args.db)
    if not db_path.is_file():
        msg = (
            f"バックテスト用DuckDBが見つかりません: {db_path}。"
            "先に data-pull またはデータ収集を実行して、初期化済みのDBを用意してください。"
        )
        raise BacktestCliError(msg)

    database = Database(args.db, read_only=True)
    market_store = MarketStore(database, parquet_root=parquet_root)
    state_store = StateStore(database)
    try:
        state_store.validate_read_only_schema()
        market_store.validate_read_only_schema()
    except StorageSchemaError as exc:
        msg = (
            f"バックテスト用DuckDBのスキーマが未初期化です: {db_path}。"
            f"{exc}。書き込み可能な実行（data-pull またはデータ収集）で"
            "スキーマを初期化してから再実行してください。"
        )
        raise BacktestCliError(msg) from exc
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
    sample = select_universe_sample(universe, args.limit)
    missing_data_symbols = _missing_data_symbols(
        market_store, sample.symbols, args.start, args.end
    )
    deps = BacktestDependencies(
        market_store=market_store,
        universe=universe,
        settings=settings,
        strategies_config=strategies,
    )
    return deps, sample, missing_data_symbols


def _resolve_candidate_stream(
    request: BacktestRequest,
    deps: BacktestDependencies,
    frame: MarketFrame,
    cache_path: Path | None,
) -> CandidateStream:
    """Reuse the persisted candidate stream when it matches, else screen anew.

    The cache key covers only what screening reads, so a cache written by a
    baseline run stays valid across an exit-parameter or cost sweep and is
    invalidated the moment the universe, window, strategy, screening settings,
    or price data move. A cache that cannot be read is a miss, not a failure.

    Args:
        request: What to backtest.
        deps: Real collaborators (store, universe, settings, strategies).
        frame: The already-loaded market frame.
        cache_path: `--candidate-cache`; `None` disables persistence.

    Returns:
        The stream to hand to every `run_backtest` call for this invocation.
    """
    expected_key = compute_cache_key(request, deps, frame)
    if cache_path is not None and cache_path.exists():
        try:
            cached = load_candidate_stream(cache_path)
        except CandidateStreamError as exc:
            sys.stdout.write(
                f"候補ストリームキャッシュを読めませんでした（{exc}）。再生成します。\n"
            )
        else:
            if cached.cache_key == expected_key:
                sys.stdout.write(f"候補ストリームキャッシュを再利用: {cache_path}\n")
                return cached
            sys.stdout.write(
                "候補ストリームキャッシュのキーが一致しません。再生成して上書きします: "
                f"{cache_path}\n"
            )

    stream = generate_candidate_stream(request, deps, frame)
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        save_candidate_stream(stream, cache_path)
        sys.stdout.write(f"候補ストリームキャッシュを保存: {cache_path}\n")
    return stream


def _earnings_guard_fn(
    deps: BacktestDependencies,
    sample: UniverseSample,
    arms: Sequence[EntryPolicyArm],
    *,
    as_of: date,
) -> Callable[[date, tuple[str, ...]], EarningsGuardInput] | None:
    """Build the point-in-time earnings lookup, when an arm can use one (#201).

    Only `regime+earnings` consults the earnings guard, so the filing history is
    read only for that arm — a `none`/`regime` run must not pay for a query
    whose answer it would discard, nor print a coverage line about a gate it
    never applies.

    Args:
        deps: Real collaborators; supplies the store and `risk.*` settings.
        sample: The symbols this run backtests.
        arms: The `--policy` arms about to run.
        as_of: The backtest window's end; the calendar re-applies the cutoff
            per simulated day.

    Returns:
        The lookup, or `None` when no arm applies the earnings gate.
    """
    if EntryPolicyArm.REGIME_EARNINGS not in arms:
        return None
    calendar = load_derived_earnings_calendar(
        deps.market_store,
        sample.symbols,
        as_of=as_of,
        lookahead_days=deps.settings.risk.earnings_lookahead_days,
    )
    projectable = len(calendar.projectable_symbols)
    sys.stdout.write(
        f"決算ゲート: 提出履歴（{'/'.join(EARNINGS_FILING_FORMS)}）から"
        f"{projectable}/{len(sample.symbols)} 銘柄の決算日を推定します"
        "（提出日は発表日より遅れる。docs/reference.md 参照）\n"
    )
    return calendar.lookup


def _run_backtest_command(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> None:
    try:
        _validate_args(args, strategies)
        deps, sample, missing_data_symbols = _compose_dependencies(
            args, settings, strategies
        )
        request = BacktestRequest(
            symbols=list(sample.symbols),
            start=args.start,
            end=args.end,
            initial_cash=settings.backtest.initial_cash_usd,
            strategy_key=args.strategy,
        )
        # Screening is identical across the normal/pessimistic pair -- only
        # slippage differs, and no Filter or Signal reads it -- so both
        # scenarios share one frame and one stream.
        frame = load_market_frame(request, deps)
        stream = _resolve_candidate_stream(request, deps, frame, args.candidate_cache)
        # One stream, one frame, N arms: the whole point of the A/B is that
        # nothing but the gates differs between the columns (Issue #184).
        arms = _policy_arms(args)
        earnings_guard_fn = _earnings_guard_fn(deps, sample, arms, as_of=args.end)
        policies = [
            build_entry_policy(
                arm,
                settings,
                frame.bars,
                earnings_guard_fn=earnings_guard_fn,
            )
            for arm in arms
        ]
        if args.pessimistic:
            normal_result = run_backtest(
                request,
                deps,
                BacktestCostOverrides(slippage_multiplier=1.0),
                candidate_stream=stream,
                market_frame=frame,
                entry_policy=policies[0],
            )
            pessimistic_result = run_backtest(
                request,
                deps,
                BacktestCostOverrides(
                    slippage_multiplier=settings.backtest.pessimistic_slippage_multiplier
                ),
                candidate_stream=stream,
                market_frame=frame,
                entry_policy=policies[0],
            )
        else:
            arm_results = [
                (
                    arm.value,
                    run_backtest(
                        request,
                        deps,
                        candidate_stream=stream,
                        market_frame=frame,
                        entry_policy=policy,
                    ),
                )
                for arm, policy in zip(arms, policies, strict=True)
            ]
    except (BacktestCliError, CandidateStreamError, EntryPolicyError) as exc:
        # Stays an inline `try` rather than a `run_cli()` call (Issue #193):
        # the block produces several locals the rest of the function reads,
        # and moving the conversion out to `main()` would widen the catch over
        # the rendering that follows. Same convention as everywhere else --
        # the message is the exit status.
        raise SystemExit(str(exc)) from exc

    meta = ReportMeta(
        strategy=args.strategy,
        start=args.start,
        end=args.end,
        missing_data_symbols=missing_data_symbols,
        universe_sample=sample,
    )
    if args.pessimistic:
        terminal_text = render_terminal_comparison(
            normal_result, pessimistic_result, meta
        )
        markdown_text = render_markdown_comparison(
            normal_result, pessimistic_result, meta
        )
    elif len(arm_results) > 1:
        terminal_text = render_policy_comparison_terminal(arm_results, meta)
        markdown_text = render_policy_comparison_markdown(arm_results, meta)
    else:
        terminal_text = render_terminal(arm_results[0][1], meta)
        markdown_text = render_markdown(arm_results[0][1], meta)

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
        _reject_grid_policy(args)
        deps, sample, missing_data_symbols = _compose_dependencies(
            args, settings, strategies
        )
        request = BacktestRequest(
            symbols=list(sample.symbols),
            start=args.start,
            end=args.end,
            initial_cash=settings.backtest.initial_cash_usd,
            strategy_key=args.strategy,
        )
        # The 25 cells vary only `exit_atr_multiple`/`max_hold_days`, which
        # the engine consumes and screening never reads: one frame and one
        # candidate stream serve the whole grid (Issue #185).
        frame = load_market_frame(request, deps)
        stream = _resolve_candidate_stream(request, deps, frame, args.candidate_cache)
    except (BacktestCliError, CandidateStreamError) as exc:
        # Inline for the same reason as `_run_backtest_command` above: the
        # prepared frame/stream feed the grid loop, and the stream is consumed
        # lazily there, so a wider catch would convert failures that reach the
        # operator as a traceback today.
        raise SystemExit(str(exc)) from exc

    cells: list[GridCell] = []
    for atr_pct, max_hold_pct, atr_value, max_hold_value in grid_param_values(
        settings.trade_plan.exit_atr_multiple, settings.trade_plan.max_hold_days
    ):
        cell_result = run_backtest(
            request,
            deps,
            BacktestCostOverrides(
                exit_atr_multiple=atr_value, max_hold_days=max_hold_value
            ),
            candidate_stream=stream,
            market_frame=frame,
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
        universe_sample=sample,
    )
    gray_threshold = settings.backtest.insufficient_trade_count_threshold

    sys.stdout.write(render_grid_terminal(grid_result, meta, gray_threshold))

    output_path = _grid_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_path, render_grid_markdown(grid_result, meta, gray_threshold))
    sys.stdout.write(f"\nReport written to {output_path}\n")


def _run_entry_grid_command(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> None:
    try:
        _validate_args(args, strategies)
        _reject_entry_grid_policy(args)
        deps, sample, missing_data_symbols = _compose_dependencies(
            args, settings, strategies
        )
        request = BacktestRequest(
            symbols=list(sample.symbols),
            start=args.start,
            end=args.end,
            initial_cash=settings.backtest.initial_cash_usd,
            strategy_key=args.strategy,
        )
        # The entry-limit multiplier is an engine-only input, so one frame and
        # one candidate stream serve every k value (Issue #357).
        frame = load_market_frame(request, deps)
        stream = _resolve_candidate_stream(request, deps, frame, args.candidate_cache)
    except (BacktestCliError, CandidateStreamError) as exc:
        raise SystemExit(str(exc)) from exc

    results = [
        (
            k_value,
            run_backtest(
                request,
                deps,
                BacktestCostOverrides(entry_limit_atr_multiple=k_value),
                candidate_stream=stream,
                market_frame=frame,
            ),
        )
        for k_value in entry_limit_grid_values()
    ]

    meta = ReportMeta(
        strategy=args.strategy,
        start=args.start,
        end=args.end,
        missing_data_symbols=missing_data_symbols,
        universe_sample=sample,
    )
    sys.stdout.write(render_entry_grid_terminal(results, meta))

    output_path = _entry_grid_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_path, render_entry_grid_markdown(results, meta))
    sys.stdout.write(f"\nReport written to {output_path}\n")


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: parse args, run the backtest or grid, print + write the report."""
    args = _parse_args(argv)
    settings = run_cli(lambda: load_settings(args.settings), _CONFIG_EXIT)
    strategies = run_cli(lambda: load_strategies(args.strategies), _CONFIG_EXIT)

    if args.command == "grid":
        _run_grid_command(args, settings, strategies)
    elif args.command == "entry-grid":
        _run_entry_grid_command(args, settings, strategies)
    else:
        _run_backtest_command(args, settings, strategies)


if __name__ == "__main__":  # pragma: no cover
    main()
