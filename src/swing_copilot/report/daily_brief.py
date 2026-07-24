"""Presentation-neutral daily decision brief construction."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from uuid import UUID

    from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary, SourcedFact
    from swing_copilot.pipeline.postmortem import SignalPerformanceRow
    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.regime.gate import RegimeSnapshot
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate, RejectionRecord
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
# REQ-008: "直近3件" -- mirrors `pipeline/daily.py`'s `_DECISION_HISTORY_LIMIT`
# (same value, used for the LLM prompt's decision history), kept as an
# independent constant here since `report/` must not depend on `pipeline/`.
_PAST_DECISIONS_LIMIT = 3


@dataclass(frozen=True, slots=True)
class BriefMarketItem:
    """One market benchmark shown above the candidate table."""

    label: str
    value: float | None
    pct_change: float | None


@dataclass(frozen=True, slots=True)
class BriefRegime:
    """Code-owned market state shown before any individual candidates."""

    gate: str
    dd_level: str
    spy_d25: float
    qqq_d25: float
    data_quality: str


@dataclass(frozen=True, slots=True)
class BriefExposure:
    """Code-owned new-entry ceiling displayed before candidates."""

    verdict: str
    gate: str
    dd_level: str
    data_quality: str
    is_conservatively_downgraded: bool


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


_CONSTRAINT_LABELS = {
    "sector": "セクター集中",
    "correlation": "相関",
}


@dataclass(frozen=True, slots=True)
class BriefRisk:
    """Position-sizing and portfolio-risk result for one candidate."""

    status: str
    max_shares: int | None
    stop_price: float | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    # P1-03 sizing breakdown (REQ-005/REQ-006).
    shares_by_risk: int | None = None
    shares_by_position_cap: int | None = None
    binding_constraint: str = "not_calculable"
    sizing_warnings: tuple[str, ...] = ()
    max_trade_risk_pct: float | None = None
    max_position_pct: float | None = None


def format_sizing(risk: BriefRisk) -> str:
    """REQ-006: a compact `"128株（制約: リスク1.0%）"`-style sizing summary.

    Returns `"-"` when `max_shares` is `None` (not calculable, the pre-P1-03
    fallback existing snapshots assert on). A final share count of `0`
    always renders with Example 4's friction wording regardless of which
    constraint was binding, since a floored-to-zero trade is unplaceable
    either way.
    """
    if risk.max_shares is None:
        return "-"
    if risk.max_shares == 0:
        return "0株（摩擦: 資金規模過小）"
    if risk.binding_constraint == "trade_risk" and risk.max_trade_risk_pct is not None:
        return (
            f"{risk.max_shares}株（制約: リスク{risk.max_trade_risk_pct * 100:.1f}%）"
        )
    if risk.binding_constraint == "position_cap" and risk.max_position_pct is not None:
        pct = risk.max_position_pct * 100
        return f"{risk.max_shares}株（制約: ポジション上限{pct:.1f}%）"
    label = _CONSTRAINT_LABELS.get(risk.binding_constraint)
    if label is not None:
        return f"{risk.max_shares}株（制約: {label}）"
    return f"{risk.max_shares}株"


@dataclass(frozen=True, slots=True)
class BriefFundamentals:
    """Human-readable point-in-time fundamental values."""

    per: str
    fcf: str
    equity_ratio: str
    eps: str


@dataclass(frozen=True, slots=True)
class BriefPastDecision:
    """One prior recorded decision for a candidate's "過去判断" section (REQ-008)."""

    run_date: date
    decision: str
    reason_memo: str | None
    realized_return_pct: float | None


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
    # REQ-008: newest-first, at most `_PAST_DECISIONS_LIMIT` entries -- see
    # `_candidate_brief`. Defaults to `()` for markdown/terminal-only tests
    # that don't exercise this section.
    past_decisions: tuple[BriefPastDecision, ...] = ()


@dataclass(frozen=True, slots=True)
class BriefRejectionCount:
    """One `reason_code`'s tally for the 落選サマリ section (P1-02)."""

    reason_code: str
    count: int


@dataclass(frozen=True, slots=True)
class DailyBrief:
    """One run's presentation-neutral decision-support result."""

    run_id: UUID
    run_date: date
    generated_at: datetime
    market: tuple[BriefMarketItem, ...]
    candidates: tuple[BriefCandidate, ...]
    regime: BriefRegime | None = None
    exposure: BriefExposure | None = None
    rejection_counts: tuple[BriefRejectionCount, ...] = ()
    notices: tuple[str, ...] = ()
    # P2-11: trailing-window per-signal hit-rate stats for the "シグナル成績" section.
    signal_performance: tuple[SignalPerformanceRow, ...] = ()


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
    # REQ-008: the single strategy this run screened with, used to scope
    # `state_store.get_decision_history()` per candidate -- today's `Candidate`
    # objects don't carry a per-candidate strategy_key (one run == one strategy).
    strategy_key: str
    rejections: list[RejectionRecord] = field(default_factory=list)
    notices: tuple[str, ...] = ()
    # P2-11: computed by `pipeline/postmortem.py`'s `run_postmortem_step()`,
    # threaded straight through to `DailyBrief` (mirrors `notices` above).
    signal_performance: tuple[SignalPerformanceRow, ...] = ()
    # REQ-006: baked into each candidate's `BriefRisk` for `format_sizing()`,
    # since `RiskAssessment` itself only carries computed outputs, not the
    # config percentages that produced them.
    max_trade_risk_pct: float = 0.01
    max_position_pct: float = 0.10
    regime_snapshot: RegimeSnapshot | None = None
    exposure_decision: ExposureDecision | None = None


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
        regime=_regime_brief(context.regime_snapshot),
        exposure=_exposure_brief(context.exposure_decision),
        rejection_counts=_rejection_counts(context.rejections),
        notices=context.notices,
        signal_performance=context.signal_performance,
    )


def _regime_brief(snapshot: RegimeSnapshot | None) -> BriefRegime | None:
    if snapshot is None:
        return None
    return BriefRegime(
        gate=snapshot.gate.verdict.value,
        dd_level=snapshot.dd_level.value,
        spy_d25=snapshot.spy_distribution.d25,
        qqq_d25=snapshot.qqq_distribution.d25,
        data_quality=snapshot.data_quality.value,
    )


def _exposure_brief(decision: ExposureDecision | None) -> BriefExposure | None:
    if decision is None:
        return None
    return BriefExposure(
        verdict=decision.verdict.value,
        gate=decision.gate.value,
        dd_level=decision.dd_level.value,
        data_quality=decision.data_quality.value,
        is_conservatively_downgraded=decision.is_conservatively_downgraded,
    )


def _rejection_counts(
    rejections: list[RejectionRecord],
) -> tuple[BriefRejectionCount, ...]:
    """Tally rejections by `reason_code`, alphabetically for a stable render."""
    counts = Counter(rejection.reason_code.value for rejection in rejections)
    return tuple(
        BriefRejectionCount(reason_code, count)
        for reason_code, count in sorted(counts.items())
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
        risk=_risk_brief(
            context.assessment,
            context.brief.max_trade_risk_pct,
            context.brief.max_position_pct,
        ),
        llm=_llm_brief(
            candidate.symbol,
            context.brief.news_summaries,
            context.brief.filing_analyses,
            state_store,
        ),
        past_decisions=_past_decisions(candidate.symbol, context.brief, state_store),
    )


def _past_decisions(
    symbol: str, brief: DailyBriefContext, state_store: StateStore
) -> tuple[BriefPastDecision, ...]:
    """REQ-008: at most `_PAST_DECISIONS_LIMIT` prior decisions, newest first.

    Delegates entirely to `state_store.get_decision_history()` -- already
    point-in-time-safe (`mode='live'` and `run_date < before_date`) and
    already ordered newest-first, so this is a pure field mapping.
    """
    history = state_store.get_decision_history(
        symbol, brief.strategy_key, brief.run_date, _PAST_DECISIONS_LIMIT
    )
    return tuple(
        BriefPastDecision(
            run_date=entry.run_date,
            decision=entry.decision,
            reason_memo=entry.reason_memo,
            realized_return_pct=entry.realized_return_pct,
        )
        for entry in history
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


def _risk_brief(
    assessment: RiskAssessment | None,
    max_trade_risk_pct: float,
    max_position_pct: float,
) -> BriefRisk:
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
        shares_by_risk=assessment.shares_by_risk,
        shares_by_position_cap=assessment.shares_by_position_cap,
        binding_constraint=assessment.binding_constraint,
        sizing_warnings=assessment.sizing_warnings,
        max_trade_risk_pct=max_trade_risk_pct,
        max_position_pct=max_position_pct,
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
