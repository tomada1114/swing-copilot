"""Readable terminal rendering for one `DailyBrief`."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console
from rich.table import Table

from swing_copilot.report.daily_brief import format_sizing

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.models import RunStatus
    from swing_copilot.report.daily_brief import (
        BriefCandidate,
        BriefPortfolioHeat,
        DailyBrief,
    )


def render_terminal(
    brief: DailyBrief,
    status: RunStatus,
    *,
    width: int = 120,
    color: bool = False,
    report_path: Path | None = None,
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
    if brief.circuit_breaker is not None:
        rules = ", ".join(brief.circuit_breaker.triggered_rules) or "none"
        console.print(
            "[bold]Circuit Breaker[/bold] "
            f"{brief.circuit_breaker.state} "
            f"(Data quality: {brief.circuit_breaker.data_quality}; "
            f"Triggered rules: {rules})"
        )
    if brief.portfolio_heat is not None:
        console.print(
            f"[bold]Portfolio heat[/bold]: {_portfolio_heat_text(brief.portfolio_heat)}"
        )
    console.print(
        "[bold]即検討可[/bold]: " + _bucket_symbols(brief.candidates, "即検討可")
    )
    console.print("[bold]様子見[/bold]: " + _bucket_symbols(brief.candidates, "様子見"))
    console.print("[bold]見送り[/bold]: " + _bucket_symbols(brief.candidates, "見送り"))

    table = Table(
        show_header=True,
        header_style="bold",
        box=box.HEAVY_HEAD,
        pad_edge=False,
    )
    table.add_column("#", justify="right")
    table.add_column("銘柄", justify="left")
    table.add_column("終値", justify="right")
    table.add_column("前日比", justify="right")
    table.add_column("スコア", justify="right")
    table.add_column("株数", justify="right")
    table.add_column("ストップ", justify="right")
    for candidate in brief.candidates:
        table.add_row(
            str(candidate.rank),
            candidate.symbol,
            _money(candidate.close),
            _percent(candidate.pct_change),
            _number(candidate.score, digits=3),
            format_sizing(candidate.risk),
            _money(candidate.risk.stop_price),
        )
    console.print(table)

    for candidate in brief.candidates:
        _render_candidate_details(console, candidate)

    if brief.notices:
        console.print("\n[bold]Warnings[/bold]")
        for notice in brief.notices:
            console.print(f"  - {notice}")
    if report_path is not None:
        console.print(f"\n詳細レポート: {report_path}")
    return buffer.getvalue().rstrip() + "\n"


def _render_candidate_details(console: Console, candidate: BriefCandidate) -> None:
    console.print(f"\n[bold]{candidate.symbol}[/bold]")
    console.print(f"  LLM: {candidate.llm.conclusion}")
    for warning in (*candidate.risk.warnings, *candidate.risk.sizing_warnings):
        console.print(f"  Risk: {warning}")
    if candidate.llm.sources:
        console.print(
            "  Sources: "
            + ", ".join(source.source_id for source in candidate.llm.sources)
        )
    # P6-27: identify each filing analysis (previously only the first one
    # per symbol ever reached the report at all).
    for filing in candidate.llm.filings:
        console.print(
            f"  Filing [{filing.filing_type} {filing.filed_at.isoformat()}]: "
            f"{filing.interpretation[0] if filing.interpretation else '-'}"
        )
    if candidate.llm.catalyst_quality is not None:
        console.print(f"  Catalyst quality: {candidate.llm.catalyst_quality}")
    if candidate.llm.is_news_near_stale or any(
        filing.is_near_stale for filing in candidate.llm.filings
    ):
        console.print(
            "  Warning: LLM分析キャッシュがTTL間近です。再実行を検討してください。"
        )


def _bucket_symbols(candidates: tuple[BriefCandidate, ...], bucket: str) -> str:
    """Return ranked bucket members, preserving the pipeline's state-cap order."""
    symbols = [
        candidate.symbol
        for candidate in candidates
        if _execution_bucket(candidate) == bucket
    ]
    return ", ".join(symbols) if symbols else "該当なし"


def _execution_bucket(candidate: BriefCandidate) -> str:
    if candidate.execution_state in {"PULLBACK_ZONE", "FAIR"}:
        return "即検討可"
    if candidate.execution_state == "EXTENDED":
        return "様子見"
    return "見送り"


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


def _portfolio_heat_text(heat: BriefPortfolioHeat) -> str:
    """Render the portfolio-heat value without adding report-level branches."""
    if heat.heat_pct is not None:
        return f"{heat.heat_pct:.2f}% / {heat.max_heat_pct:.2f}%"
    if heat.missing_stop_symbols:
        symbols = ", ".join(heat.missing_stop_symbols)
        return f"not_calculable (missing stop: {symbols})"
    return f"not_calculable ({heat.reason or 'unknown reason'})"


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
