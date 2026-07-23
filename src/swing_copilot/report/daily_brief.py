"""Presentation-neutral daily decision brief construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

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

MARKET_STRIP_SYMBOLS = ("SPY", "QQQ", "^VIX", "^TNX")
_MARKET_LABELS = (("SPY", "SPY"), ("QQQ", "QQQ"), ("^VIX", "VIX"), ("^TNX", "US10Y"))
_LOOKBACK_DAYS = 10
_MIN_BARS_FOR_CHANGE = 2

_SIGNAL_LABELS = {"trend_sma": "SMA200上抜け", "pullback_rsi": "RSI押し目"}
_HIDDEN_SIGNALS = frozenset({"volume_min"})
_DEGRADED_LLM_MESSAGE = "本日はニュース・開示分析を取得できませんでした"
_NEUTRAL_LLM_MESSAGE = "ニュース・開示分析からの追加情報は今回ありません"


@dataclass(frozen=True, slots=True)
class BriefMarketItem:
    """One market benchmark shown above the candidate table."""

    label: str
    value: float | None
    pct_change: float | None


@dataclass(frozen=True, slots=True)
class BriefSource:
    """Stable source reference for one or more LLM facts."""

    source_id: str
    url: str


@dataclass(frozen=True, slots=True)
class BriefLlm:
    """Compact LLM analysis for one candidate."""

    degraded: bool
    conclusion: str
    facts: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    sources: tuple[BriefSource, ...] = ()


@dataclass(frozen=True, slots=True)
class BriefRisk:
    """Position-sizing and portfolio-risk result for one candidate."""

    status: str
    max_shares: int | None
    stop_price: float | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BriefFundamentals:
    """Human-readable point-in-time fundamental values."""

    per: str
    fcf: str
    equity_ratio: str
    eps: str


@dataclass(frozen=True, slots=True)
class BriefCandidate:
    """All terminal/Markdown presentation data for one ranked candidate."""

    rank: int
    symbol: str
    company_name: str | None
    close: float | None
    pct_change: float | None
    rsi14: float | None
    atr14: float | None
    score: float | None
    score_rsi_pullback: float | None
    score_trend_quality: float | None
    score_liquidity: float | None
    signals: tuple[str, ...]
    fundamentals: BriefFundamentals
    risk: BriefRisk
    llm: BriefLlm


@dataclass(frozen=True, slots=True)
class DailyBrief:
    """One run's presentation-neutral decision-support result."""

    run_id: UUID
    run_date: date
    generated_at: datetime
    market: tuple[BriefMarketItem, ...]
    candidates: tuple[BriefCandidate, ...]
    notices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DailyBriefContext:
    """Inputs required to assemble a `DailyBrief`."""

    run_id: UUID
    run_date: date
    generated_at: datetime
    universe: tuple[UniverseMember, ...]
    candidates: list[Candidate]
    risk_assessments: list[RiskAssessment]
    news_summaries: list[NewsSummary] | None
    filing_analyses: list[FilingAnalysis] | None
    notices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _CandidateContext:
    member: UniverseMember | None
    assessment: RiskAssessment | None
    brief: DailyBriefContext


def build_daily_brief(
    context: DailyBriefContext,
    market_store: MarketStore,
    state_store: StateStore,
) -> DailyBrief:
    """Build the shared terminal/Markdown view using point-in-time reads."""
    companies = {member.symbol: member for member in context.universe}
    risks = {assessment.symbol: assessment for assessment in context.risk_assessments}
    candidates = tuple(
        _candidate_brief(
            candidate,
            _CandidateContext(
                member=companies.get(candidate.symbol),
                assessment=risks.get(candidate.symbol),
                brief=context,
            ),
            market_store,
            state_store,
        )
        for candidate in context.candidates
    )
    return DailyBrief(
        run_id=context.run_id,
        run_date=context.run_date,
        generated_at=context.generated_at,
        market=_market_items(market_store, context.run_date),
        candidates=candidates,
        notices=context.notices,
    )


def _market_items(
    market_store: MarketStore, as_of: date
) -> tuple[BriefMarketItem, ...]:
    items = []
    for symbol, label in _MARKET_LABELS:
        bars = market_store.read_bars(
            [symbol], as_of - timedelta(days=_LOOKBACK_DAYS), as_of, as_of
        ).sort_values("date")
        if len(bars) < _MIN_BARS_FOR_CHANGE:
            items.append(BriefMarketItem(label, None, None))
            continue
        current = float(bars.iloc[-1]["close"])
        previous = float(bars.iloc[-2]["close"])
        change = (current - previous) / previous if previous else None
        items.append(BriefMarketItem(label, current, change))
    return tuple(items)


def _candidate_brief(
    candidate: Candidate,
    context: _CandidateContext,
    market_store: MarketStore,
    state_store: StateStore,
) -> BriefCandidate:
    close = candidate.metrics.get("close")
    previous = _previous_close(market_store, candidate.symbol, context.brief.run_date)
    pct_change = (
        (close - previous) / previous if close is not None and previous else None
    )
    return BriefCandidate(
        rank=candidate.rank,
        symbol=candidate.symbol,
        company_name=context.member.company_name if context.member else None,
        close=close,
        pct_change=pct_change,
        rsi14=candidate.metrics.get("rsi14"),
        atr14=candidate.metrics.get("atr14"),
        score=candidate.metrics.get("score"),
        score_rsi_pullback=candidate.metrics.get("score_rsi_pullback"),
        score_trend_quality=candidate.metrics.get("score_trend_quality"),
        score_liquidity=candidate.metrics.get("score_liquidity"),
        signals=tuple(
            _SIGNAL_LABELS.get(name, name)
            for name in candidate.signal_names
            if name not in _HIDDEN_SIGNALS
        ),
        fundamentals=_fundamentals(
            market_store, candidate.symbol, context.brief.run_date, close
        ),
        risk=_risk_brief(context.assessment),
        llm=_llm_brief(
            candidate.symbol,
            context.brief.news_summaries,
            context.brief.filing_analyses,
            state_store,
        ),
    )


def _previous_close(
    market_store: MarketStore, symbol: str, as_of: date
) -> float | None:
    bars = market_store.read_bars(
        [symbol], as_of - timedelta(days=_LOOKBACK_DAYS), as_of, as_of
    ).sort_values("date")
    return float(bars.iloc[-2]["close"]) if len(bars) >= _MIN_BARS_FOR_CHANGE else None


def _fundamentals(
    market_store: MarketStore, symbol: str, as_of: date, close: float | None
) -> BriefFundamentals:
    record = market_store.get_latest_fundamentals(symbol, as_of)
    if record is None:
        return BriefFundamentals("N/A", "N/A", "N/A", "N/A")
    eps_value = (
        record.net_income / record.shares
        if record.net_income is not None and record.shares
        else None
    )
    per = (
        f"{close / eps_value:.1f}x"
        if eps_value is not None and eps_value > 0 and close is not None and close > 0
        else "N/A"
    )
    fcf = f"${record.fcf:,.0f}" if record.fcf is not None else "N/A"
    equity_ratio = (
        f"{record.equity / record.assets:.0%}"
        if record.equity is not None and record.assets
        else "N/A"
    )
    eps = f"${eps_value:.2f}" if eps_value is not None else "N/A"
    return BriefFundamentals(per, fcf, equity_ratio, eps)


def _risk_brief(assessment: RiskAssessment | None) -> BriefRisk:
    if assessment is None:
        return BriefRisk("not_calculable", None, None, (), ())
    warnings = tuple(
        (
            f"{warning.correlated_symbol}との相関 {warning.correlation:.2f}"
            if warning.warning_type == "high_correlation"
            else f"{warning.correlated_symbol}: {warning.warning_type}"
        )
        for warning in assessment.warnings
    )
    return BriefRisk(
        assessment.status,
        assessment.max_shares,
        assessment.stop_price,
        assessment.reasons,
        warnings,
    )


def _llm_brief(
    symbol: str,
    news_summaries: list[NewsSummary] | None,
    filing_analyses: list[FilingAnalysis] | None,
    state_store: StateStore,
) -> BriefLlm:
    if news_summaries is None and filing_analyses is None:
        return BriefLlm(True, _DEGRADED_LLM_MESSAGE)
    news = next((item for item in news_summaries or [] if item.symbol == symbol), None)
    filing = next(
        (item for item in filing_analyses or [] if item.symbol == symbol), None
    )
    if news is None and filing is None:
        return BriefLlm(True, _NEUTRAL_LLM_MESSAGE)
    facts = (*tuple(news.facts if news else []), *tuple(filing.facts if filing else []))
    interpretations = (
        *tuple(news.interpretation if news else []),
        *tuple(filing.interpretation if filing else []),
    )
    flags = (
        *tuple(news.risk_flags if news else []),
        *tuple(filing.red_flags if filing else []),
    )
    return BriefLlm(
        degraded=False,
        conclusion=interpretations[0] if interpretations else _NEUTRAL_LLM_MESSAGE,
        facts=tuple(fact.statement for fact in facts),
        risk_flags=flags,
        sources=_sources(facts, state_store),
    )


def _sources(
    facts: Sequence[SourcedFact], state_store: StateStore
) -> tuple[BriefSource, ...]:
    source_ids = list(
        dict.fromkeys(source_id for fact in facts for source_id in fact.source_ids)
    )
    urls = state_store.get_source_urls(source_ids)
    return tuple(
        BriefSource(source_id, urls.get(source_id, source_id))
        for source_id in source_ids
    )
