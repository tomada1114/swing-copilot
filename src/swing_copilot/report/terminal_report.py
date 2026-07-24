"""Readable terminal rendering for one `DailyBrief`."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from swing_copilot.report.daily_brief import format_sizing

if TYPE_CHECKING:
    from swing_copilot.models import RunStatus
    from swing_copilot.report.daily_brief import BriefCandidate, DailyBrief


def render_terminal(
    brief: DailyBrief,
    status: RunStatus,
    *,
    width: int = 120,
    color: bool = False,
) -> str:
    """Render a compact summary suitable for stdout and snapshots."""
    buffer = StringIO()
    console = Console(
        file=buffer,
        width=width,
        force_terminal=color,
        color_system="standard" if color else None,
        highlight=False,
    )
    console.print(
        f"[bold]Swing Copilot[/bold] — {brief.run_date.isoformat()}  "
        f"Status: [bold]{status.value.upper()}[/bold]  "
        f"Candidates: {len(brief.candidates)}  Run: {brief.run_id}"
    )
    market = "  ".join(
        f"{item.label} {_number(item.value)} ({_percent(item.pct_change)})"
        for item in brief.market
    )
    if market:
        console.print(market)
    _render_regime(console, brief)
    if brief.exposure is not None:
        downgraded = (
            " (conservative downgrade)"
            if brief.exposure.is_conservatively_downgraded
            else ""
        )
        console.print(
            "[bold]Exposure Ceiling[/bold] "
            f"{brief.exposure.verdict}{downgraded} "
            f"(Gate: {brief.exposure.gate}, DD: {brief.exposure.dd_level}, "
            f"Data quality: {brief.exposure.data_quality})"
        )

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("#", justify="right")
    table.add_column("Symbol", justify="left")
    table.add_column("Close", justify="right")
    table.add_column("Chg", justify="right")
    table.add_column("RSI", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Breakdown", justify="left")
    table.add_column("Signal", justify="left")
    table.add_column("Risk", justify="left")
    table.add_column("Shares", justify="right")
    table.add_column("Stop", justify="right")
    for candidate in brief.candidates:
        table.add_row(
            str(candidate.rank),
            candidate.symbol,
            _money(candidate.close),
            _percent(candidate.pct_change),
            _number(candidate.rsi14, digits=1),
            _number(candidate.score, digits=3),
            _score_breakdown(candidate),
            ", ".join(candidate.signals) or "-",
            candidate.risk.status,
            format_sizing(candidate.risk),
            _money(candidate.risk.stop_price),
        )
    console.print(table)

    for candidate in brief.candidates:
        console.print(f"\n[bold]{candidate.symbol}[/bold]")
        console.print(f"  LLM: {candidate.llm.conclusion}")
        for warning in candidate.risk.warnings:
            console.print(f"  Risk: {warning}")
        if candidate.llm.sources:
            console.print(
                "  Sources: "
                + ", ".join(source.source_id for source in candidate.llm.sources)
            )
    console.print("\n[bold]落選サマリ[/bold]")
    if brief.rejection_counts:
        for item in brief.rejection_counts:
            console.print(f"  {item.reason_code}: {item.count}件")
    else:
        console.print("  該当なし(0件)")

    if brief.notices:
        console.print("\n[bold]Warnings[/bold]")
        for notice in brief.notices:
            console.print(f"  - {notice}")
    return buffer.getvalue().rstrip() + "\n"


def _render_regime(console: Console, brief: DailyBrief) -> None:
    if brief.regime is None:
        return
    console.print(
        "[bold]Market regime[/bold] "
        f"Gate: {brief.regime.gate} / DD: {brief.regime.dd_level} "
        f"(SPY d25={brief.regime.spy_d25:g}, QQQ d25={brief.regime.qqq_d25:g}) / "
        f"Data quality: {brief.regime.data_quality}"
    )
    if brief.regime.spy_ftd_state is not None:
        console.print(
            "[bold]FTD[/bold] "
            f"SPY {_ftd_description(brief.regime.spy_ftd_state, brief.regime.spy_ftd_day_number, brief.regime.spy_ftd_quality_score)} / "
            f"QQQ {_ftd_description(brief.regime.qqq_ftd_state, brief.regime.qqq_ftd_day_number, brief.regime.qqq_ftd_quality_score)}"
        )


def _score_breakdown(candidate: BriefCandidate) -> str:
    if (
        candidate.score_rsi_pullback is None
        or candidate.score_trend_quality is None
        or candidate.score_liquidity is None
    ):
        return "N/A"
    return (
        f"rsi {candidate.score_rsi_pullback:.2f} / "
        f"trend {candidate.score_trend_quality:.2f} / "
        f"liq {candidate.score_liquidity:.2f}"
    )


def _number(value: float | None, *, digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:,.{digits}f}"


def _money(value: float | None) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2%}"


def _ftd_description(
    state: str | None, day_number: int | None, score: int | None
) -> str:
    parts = [state or "UNKNOWN"]
    if day_number is not None:
        parts.append(f"Day{day_number}")
    if score is not None:
        parts.append(f"quality {score}")
    return " (" + ", ".join(parts) + ")"
