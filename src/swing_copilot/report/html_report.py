"""Daily HTML report rendering (FR-09, `docs/05_ui_design.md`, `docs/04_detailed_design.md` 3.18).

Builds the single Jinja2 template context for `templates/report.html.j2`:
market strip, risk warnings, the ranked candidate summary table, and one
detail card per candidate (chart data, fundamentals, risk, and a fail-soft
LLM summary block). `classify_change()` and the signal badge mapping are
implemented once here and reused by every section that needs an up/down/
neutral judgment or a Japanese extraction-reason label
(`docs/05_ui_design.md` 3.3, 6.1, 10.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jinja2 import Environment, FileSystemLoader

from swing_copilot import __version__
from swing_copilot.report.chart_data import build_chart_data

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from uuid import UUID

    from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary, SourcedFact
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore
    from swing_copilot.universe import UniverseMember

ChangeClass = Literal["up", "down", "neutral"]

_CHANGE_UP_THRESHOLD = 0.001
_CHANGE_DOWN_THRESHOLD = -0.001

_SIGNAL_BADGE_LABELS = {
    "trend_sma": "SMA200上抜け",
    "pullback_rsi": "RSI押し目",
}
_UNBADGED_SIGNALS = frozenset({"volume_min"})

_DEGRADED_LLM_MESSAGE = "本日はニュース・開示分析を取得できませんでした"
_NEUTRAL_CONCLUSION = "ニュース・開示分析からの追加情報は今回ありません"

# Public: pipeline/daily.py's price step also fetches these symbols so the
# market strip has bars to read (`docs/05_ui_design.md` 7.2).
MARKET_STRIP_SYMBOLS = ("SPY", "QQQ", "^VIX", "^TNX")
_MARKET_STRIP_LABELS = (
    ("SPY", "SPY"),
    ("QQQ", "QQQ"),
    ("^VIX", "VIX"),
    ("^TNX", "US10Y"),
)
_MARKET_STRIP_LOOKBACK_DAYS = 10
_MIN_BARS_FOR_CHANGE = 2

_SPARKLINE_LOOKBACK_DAYS = 20
_SPARKLINE_WIDTH = 96.0
_SPARKLINE_HEIGHT = 28.0
_SPARKLINE_MARGIN = 2.0
_SPARKLINE_COLORS = {"up": "#2EBD85", "down": "#EF5350", "neutral": "#8B949E"}


def classify_change(pct: float) -> ChangeClass:
    """Classify a day-over-day change into up/down/neutral.

    The one shared threshold function (`docs/05_ui_design.md` 3.3), reused
    by the market strip, summary table, sparklines, and detail cards.

    Args:
        pct: Change expressed as a fraction (``0.01`` == +1%).

    Returns:
        `"up"` if `pct >= +0.1%`, `"down"` if `pct <= -0.1%`, else `"neutral"`.
    """
    if pct >= _CHANGE_UP_THRESHOLD:
        return "up"
    if pct <= _CHANGE_DOWN_THRESHOLD:
        return "down"
    return "neutral"


def badge_label(signal_name: str) -> str:
    """Map a `signal_name` to its Japanese badge label.

    Args:
        signal_name: A `Candidate.signal_names` entry.

    Returns:
        The mapped label, or `signal_name` itself if unmapped — an unknown
        signal is always shown, never dropped (`docs/05_ui_design.md` 6.1).
    """
    return _SIGNAL_BADGE_LABELS.get(signal_name, signal_name)


def badge_names(signal_names: Sequence[str]) -> list[str]:
    """Return `signal_names` minus the ones that are never badged.

    Args:
        signal_names: A candidate's raw signal names.

    Returns:
        Signal names to render as badges, in their original order.
    """
    return [name for name in signal_names if name not in _UNBADGED_SIGNALS]


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Everything `render_report()` needs to build one day's report."""

    run_id: UUID
    run_date: date
    generated_at: datetime
    universe: tuple[UniverseMember, ...]
    candidates: list[Candidate]
    risk_assessments: list[RiskAssessment]
    news_summaries: list[NewsSummary] | None
    filing_analyses: list[FilingAnalysis] | None


def _market_strip(market_store: MarketStore, as_of: date) -> list[dict[str, object]]:
    start = as_of - timedelta(days=_MARKET_STRIP_LOOKBACK_DAYS)
    items: list[dict[str, object]] = []
    for symbol, label in _MARKET_STRIP_LABELS:
        bars = market_store.read_bars([symbol], start, as_of, as_of).sort_values("date")
        if len(bars) < _MIN_BARS_FOR_CHANGE:
            items.append(
                {
                    "label": label,
                    "value": None,
                    "pct_change": None,
                    "change_class": None,
                }
            )
            continue
        latest_close = float(bars.iloc[-1]["close"])
        previous_close = float(bars.iloc[-2]["close"])
        pct_change = (latest_close - previous_close) / previous_close
        items.append(
            {
                "label": label,
                "value": latest_close,
                "pct_change": pct_change,
                "change_class": classify_change(pct_change),
            }
        )
    return items


def _risk_warnings(assessments: list[RiskAssessment]) -> list[dict[str, object]]:
    return [
        {
            "candidate_symbol": assessment.symbol,
            "correlated_symbol": warning.correlated_symbol,
            "correlation": warning.correlation,
            "warning_type": warning.warning_type,
        }
        for assessment in assessments
        for warning in assessment.warnings
    ]


def _sparkline(closes: Sequence[float]) -> dict[str, str]:
    if len(closes) < _MIN_BARS_FOR_CHANGE:
        return {"points": "", "color": _SPARKLINE_COLORS["neutral"]}
    low, high = min(closes), max(closes)
    span = high - low or 1.0
    usable_width = _SPARKLINE_WIDTH - 2 * _SPARKLINE_MARGIN
    usable_height = _SPARKLINE_HEIGHT - 2 * _SPARKLINE_MARGIN
    step = usable_width / (len(closes) - 1)
    points = []
    for index, close in enumerate(closes):
        x = _SPARKLINE_MARGIN + index * step
        normalized = (close - low) / span
        y = _SPARKLINE_MARGIN + (1 - normalized) * usable_height
        points.append(f"{x:.1f},{y:.1f}")
    change = (closes[-1] - closes[0]) / closes[0] if closes[0] else 0.0
    return {
        "points": " ".join(points),
        "color": _SPARKLINE_COLORS[classify_change(change)],
    }


def _previous_close(
    market_store: MarketStore, symbol: str, as_of: date
) -> float | None:
    start = as_of - timedelta(days=_MARKET_STRIP_LOOKBACK_DAYS)
    bars = market_store.read_bars([symbol], start, as_of, as_of).sort_values("date")
    if len(bars) < _MIN_BARS_FOR_CHANGE:
        return None
    return float(bars.iloc[-2]["close"])


def _summary_rows(
    context: ReportContext, market_store: MarketStore
) -> list[dict[str, object]]:
    company_by_symbol = {m.symbol: m for m in context.universe}
    rows = []
    for candidate in context.candidates:
        member = company_by_symbol.get(candidate.symbol)
        close = candidate.metrics.get("close")
        previous_close = _previous_close(
            market_store, candidate.symbol, context.run_date
        )
        pct_change = (
            (close - previous_close) / previous_close
            if close is not None and previous_close
            else None
        )
        spark_start = context.run_date - timedelta(days=_SPARKLINE_LOOKBACK_DAYS * 2)
        spark_bars = market_store.read_bars(
            [candidate.symbol], spark_start, context.run_date, context.run_date
        ).sort_values("date")
        closes = spark_bars["close"].tail(_SPARKLINE_LOOKBACK_DAYS).tolist()
        rows.append(
            {
                "rank": candidate.rank,
                "symbol": candidate.symbol,
                "company_name": member.company_name if member else None,
                "badges": [
                    badge_label(name) for name in badge_names(candidate.signal_names)
                ],
                "close": close,
                "pct_change": pct_change,
                "change_class": classify_change(pct_change)
                if pct_change is not None
                else None,
                "rsi14": candidate.metrics.get("rsi14"),
                "atr14": candidate.metrics.get("atr14"),
                "sparkline": _sparkline(closes),
            }
        )
    return rows


def _fundamentals_block(
    market_store: MarketStore, symbol: str, as_of: date, close: float | None
) -> dict[str, str]:
    record = market_store.get_latest_fundamentals(symbol, as_of)
    if record is None:
        return {"per": "N/A", "fcf": "N/A", "equity_ratio": "N/A", "eps": "N/A"}

    # EPS depends only on the filing (net_income/shares); it must render even
    # when `close` is unavailable (`design.md` 2.1). PER additionally needs a
    # valid close and a positive EPS.
    eps_value: float | None = None
    if record.net_income and record.shares:
        eps_value = record.net_income / record.shares

    per = "N/A"
    if eps_value is not None and eps_value > 0 and close is not None and close > 0:
        per = f"{close / eps_value:.1f}x"

    fcf = f"${record.fcf:,.0f}" if record.fcf is not None else "N/A"

    equity_ratio = "N/A"
    if record.equity is not None and record.assets:
        equity_ratio = f"{record.equity / record.assets:.0%}"

    eps = f"${eps_value:.2f}" if eps_value is not None else "N/A"

    return {"per": per, "fcf": fcf, "equity_ratio": equity_ratio, "eps": eps}


def _resolve_fact_sources(
    facts: Sequence[SourcedFact], url_by_id: dict[str, str]
) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for fact in facts:
        for source_id in fact.source_ids:
            seen.setdefault(source_id, url_by_id.get(source_id, source_id))
    return [{"source_id": sid, "url": url} for sid, url in seen.items()]


def _llm_block(
    symbol: str,
    news_summaries: list[NewsSummary] | None,
    filing_analyses: list[FilingAnalysis] | None,
    state_store: StateStore,
) -> dict[str, object]:
    if news_summaries is None and filing_analyses is None:
        return {"degraded": True, "message": _DEGRADED_LLM_MESSAGE}

    news = next((n for n in news_summaries or [] if n.symbol == symbol), None)
    filing = next((f for f in filing_analyses or [] if f.symbol == symbol), None)
    if news is None and filing is None:
        return {"degraded": True, "message": _NEUTRAL_CONCLUSION}

    conclusion = _NEUTRAL_CONCLUSION
    if news is not None and news.interpretation:
        conclusion = news.interpretation[0]
    elif filing is not None and filing.interpretation:
        conclusion = filing.interpretation[0]

    interpretation = [
        *(news.interpretation if news else []),
        *(filing.interpretation if filing else []),
    ]
    risk_flags = [
        *(news.risk_flags if news else []),
        *(filing.red_flags if filing else []),
    ]
    facts = [*(news.facts if news else []), *(filing.facts if filing else [])]
    source_ids = [sid for fact in facts for sid in fact.source_ids]
    url_by_id = state_store.get_source_urls(source_ids)

    return {
        "degraded": False,
        "conclusion": conclusion,
        "reasons": interpretation[1:],
        "risk_flags": risk_flags,
        "facts": [fact.statement for fact in facts],
        "sources": _resolve_fact_sources(facts, url_by_id),
    }


def _risk_block(assessment: RiskAssessment | None) -> dict[str, object]:
    if assessment is None:
        return {
            "status": "not_calculable",
            "max_shares": None,
            "stop_price": None,
            "reasons": [],
        }
    return {
        "status": assessment.status,
        "max_shares": assessment.max_shares,
        "entry_price": assessment.entry_price,
        "stop_price": assessment.stop_price,
        "reasons": list(assessment.reasons),
    }


def _detail_cards(
    context: ReportContext, market_store: MarketStore, state_store: StateStore
) -> list[dict[str, object]]:
    company_by_symbol = {m.symbol: m for m in context.universe}
    risk_by_symbol = {a.symbol: a for a in context.risk_assessments}
    cards: list[dict[str, object]] = []
    for candidate in context.candidates:
        member = company_by_symbol.get(candidate.symbol)
        close = candidate.metrics.get("close")
        chart_data = build_chart_data(candidate.symbol, market_store, context.run_date)
        technical = {
            "close": close,
            "sma50": chart_data.sma50[-1].value if chart_data.sma50 else None,
            "sma200": chart_data.sma200[-1].value if chart_data.sma200 else None,
            "rsi14": candidate.metrics.get("rsi14"),
            "atr14": candidate.metrics.get("atr14"),
            "avg_volume": candidate.metrics.get("avg_volume"),
        }
        cards.append(
            {
                "symbol": candidate.symbol,
                "company_name": member.company_name if member else None,
                "sector": member.gics_sector if member else None,
                "badges": [
                    badge_label(name) for name in badge_names(candidate.signal_names)
                ],
                "chart_data": chart_data.model_dump(),
                "technical": technical,
                "fundamentals": _fundamentals_block(
                    market_store, candidate.symbol, context.run_date, close
                ),
                "risk": _risk_block(risk_by_symbol.get(candidate.symbol)),
                "llm": _llm_block(
                    candidate.symbol,
                    context.news_summaries,
                    context.filing_analyses,
                    state_store,
                ),
            }
        )
    return cards


def _adjacent_reports(output_dir: Path, run_date: date) -> dict[str, str | None]:
    existing = sorted(p.stem for p in output_dir.glob("*.html") if p.stem != "latest")
    previous = max((d for d in existing if d < run_date.isoformat()), default=None)
    following = min((d for d in existing if d > run_date.isoformat()), default=None)
    return {"previous": previous, "next": following}


def _atomic_write(path: Path, content: str) -> None:
    """Write `content` to `path` via a same-directory temp file + `os.replace`.

    On any failure, the previous `path` contents are left untouched and the
    temp file is removed rather than left behind.

    Args:
        path: Destination file to replace.
        content: Full file content to write.
    """
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def render_report(
    context: ReportContext,
    market_store: MarketStore,
    state_store: StateStore,
    templates_dir: str = "templates",
    output_dir: str = "reports",
) -> Path:
    """Render one day's report and write it atomically.

    Args:
        context: Everything the report needs (candidates, risk, LLM output).
        market_store: Source for chart data, fundamentals, and price history.
        state_store: Source for resolving LLM fact `source_ids` to URLs.
        templates_dir: Directory containing `report.html.j2`, resolved
            relative to the current working directory (same convention as
            `config.load_settings`).
        output_dir: Directory to write `{run_date}.html` and `latest.html`
            into, resolved relative to the current working directory.

    Returns:
        Path to the dated report file just written.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=True)
    template = env.get_template("report.html.j2")
    html = template.render(
        run_id=context.run_id,
        run_date=context.run_date,
        generated_at=context.generated_at,
        universe_count=len(context.universe),
        market_strip=_market_strip(market_store, context.run_date),
        risk_warnings=_risk_warnings(context.risk_assessments),
        summary_rows=_summary_rows(context, market_store),
        cards=_detail_cards(context, market_store, state_store),
        adjacent_reports=_adjacent_reports(output_path, context.run_date),
        version=__version__,
    )

    dated_path = output_path / f"{context.run_date.isoformat()}.html"
    _atomic_write(dated_path, html)
    _atomic_write(output_path / "latest.html", html)
    return dated_path
