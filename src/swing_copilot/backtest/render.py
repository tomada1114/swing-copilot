"""Pure rendering for `copilot-backtest`: terminal/markdown text, no I/O.

Split out of `backtest/cli.py` (Issue #399): this module owns every
`render_*` function plus their shared helpers (`ReportMeta`, the metric/exit/
entry-block/equity-curve row builders) -- none of it touches argparse or the
filesystem. `cli.py` keeps the CLI wiring (`main`, `_run_*_command`,
`_compose_dependencies`) and imports `render_*`/`ReportMeta` from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from swing_copilot.backtest.sensitivity import (
    ATR_MULTIPLIER_PCT_GRID,
    MAX_HOLD_PCT_GRID,
    is_gray_cell,
)
from swing_copilot.report.formatting import (
    format_fraction_pct,
    format_money,
    format_ratio,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from swing_copilot.backtest.engine import BacktestResult
    from swing_copilot.backtest.sensitivity import GridCell, SensitivityGridResult
    from swing_copilot.universe_sampling import UniverseSample

_CONSOLE_WIDTH = 200


@dataclass(frozen=True, slots=True)
class ReportMeta:
    """Shared render context: what was backtested and any skipped symbols."""

    strategy: str
    start: date
    end: date
    missing_data_symbols: Sequence[str]
    universe_sample: UniverseSample


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
        return format_fraction_pct(value)
    if field in _MONEY_FIELDS:
        return format_money(value)
    return format_ratio(value)


def _exit_breakdown_rows(result: BacktestResult) -> list[tuple[str, str]]:
    """Label/value rows shared by the terminal and markdown exit sections."""
    rows = [(reason, str(count)) for reason, count in result.exit_reason_counts]
    rows.append(
        ("max_hold binding rate", format_fraction_pct(result.max_hold_binding_rate))
    )
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
