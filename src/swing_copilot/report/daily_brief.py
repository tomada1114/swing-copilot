"""Presentation-neutral daily decision brief construction."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# `date`/`datetime`/`UUID` are imported at runtime, not under TYPE_CHECKING:
# `analysis/snapshot.py` builds a pydantic `TypeAdapter(DailyBrief)` to archive
# and reload this module's dataclasses, and pydantic resolves their annotation
# strings against *this* module's globals. Hiding these three names would make
# that adapter unbuildable.
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID  # noqa: TC003

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from swing_copilot.analysis.schemas import NewsSupply, SourcedFact
    from swing_copilot.analysis.validate import (
        ResolvedFiling,
        SymbolOutcome,
        ValidatedAnalysis,
    )
    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.regime.ftd import FtdSnapshot
    from swing_copilot.regime.gate import RegimeSnapshot
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate, RejectionRecord
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.universe import UniverseMember

MARKET_STRIP_SYMBOLS = ("SPY", "QQQ", "^VIX", "^TNX")
_MARKET_LABELS = (("SPY", "SPY"), ("QQQ", "QQQ"), ("^VIX", "VIX"), ("^TNX", "US10Y"))
_LOOKBACK_DAYS = 10
_MIN_BARS_FOR_CHANGE = 2

_SIGNAL_LABELS = {
    "trend_sma": "SMA200上抜け",
    "pullback_rsi": "RSI押し目",
    "minervini_stage2": "Minervini Stage2",
    "vcp_breakout": "VCP",
}
_HIDDEN_SIGNALS = frozenset({"volume_min"})
#: Shown by `copilot-daily` itself: the deterministic pipeline is complete but
#: nobody has run the qualitative analysis skill over its exported input yet.
PENDING_ANALYSIS_MESSAGE = "分析待ち（swing-daily スキルで分析を実行してください）"
#: Defensive fallback for a hand-constructed analysis that lacks a candidate.
#: A successfully ingested result always has complete symbol coverage.
MISSING_ANALYSIS_MESSAGE = "定性分析なし"
#: Shown after ingest for a candidate whose analysis failed verification.
WITHHELD_ANALYSIS_MESSAGE = "検証不合格のため非表示"
_NEUTRAL_ANALYSIS_MESSAGE = "定性分析からの追加情報は今回ありません"
NO_TRADE_MESSAGE = "本日は取引なし（定性判断）"


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
    spy_close: float | None = None
    spy_sma200: float | None = None
    spy_trend_gap_pct: float | None = None
    spy_ftd_state: str | None = None
    spy_ftd_day_number: int | None = None
    spy_ftd_quality_score: int | None = None
    qqq_ftd_state: str | None = None
    qqq_ftd_day_number: int | None = None
    qqq_ftd_quality_score: int | None = None


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
    """Stable source reference for one or more analysis facts."""

    source_id: str
    url: str


@dataclass(frozen=True, slots=True)
class BriefFilingAnalysis:
    """One filing's analysis with code-owned identifying metadata.

    `filing_type`/`filed_at` let the report distinguish which of a symbol's
    (potentially several) filing analyses a fact/interpretation came from.
    Both are resolved from the exported `analysis_input.json` entry, never
    echoed back by the analysis itself.
    """

    filing_type: str
    filed_at: date
    facts: tuple[str, ...] = ()
    interpretation: tuple[str, ...] = ()
    red_flags: tuple[str, ...] = ()
    yoy_changes: tuple[str, ...] = ()
    sources: tuple[BriefSource, ...] = ()


@dataclass(frozen=True, slots=True)
class BriefNewsSupply:
    """Presentation copy of `analysis.schemas.NewsSupply` (Issue #281).

    AC14 keeps `news_summary` null whenever `news[]` is empty, which erases
    the distinction `news_supply` exists to record: "suppressed" (`level`
    none/sparse over a non-empty collected set -- the news exists but hardly
    any of it names the company) versus "genuinely zero" (`collected_items ==
    0` -- nothing was collected at all). Both counts and `level` are
    code-computed at export time (`analysis/news_supply.py`) and copied here
    verbatim, never written or judged by a skill.
    """

    level: str
    collected_items: int
    exported_items: int
    symbol_mention_items: int


@dataclass(frozen=True, slots=True)
class BriefAnalysis:
    """Compact qualitative analysis for one candidate.

    `degraded` means there is nothing verified to show -- analysis not run
    yet, not produced for this symbol, or withheld because it failed
    verification -- and `conclusion` then carries the reason. The
    deterministic screening figures beside it are never affected either way.
    """

    degraded: bool
    conclusion: str
    facts: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    sources: tuple[BriefSource, ...] = ()
    # Every filing analysis for this candidate, individually identified.
    filings: tuple[BriefFilingAnalysis, ...] = ()
    # Qualitative go/no-go: `"proceed"`, `"skip"`, or `None` when unavailable.
    # Display-only -- it never edits scores, sizing, or ranking.
    verdict: str | None = None
    verdict_summary: str | None = None
    strengths: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()
    # Code-owned news-supply measurement (Issue #281), `None` only when the
    # verified outcome carried none (a pre-#130 archived document, or a
    # withheld/pending/missing analysis where nothing verified exists at all).
    news_supply: BriefNewsSupply | None = None


@dataclass(frozen=True, slots=True)
class BriefRisk:
    """Account-independent trade-plan result for one candidate."""

    status: str
    entry_price: float | None
    limit_price: float | None
    stop_price: float | None
    atr14: float | None
    stop_distance_pct: float | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    binding_constraint: str | None = None


def format_verdict(analysis: BriefAnalysis) -> str | None:
    """Render the qualitative verdict line shared by both report renderers.

    Returns `None` (render nothing) when there is no verified verdict, so a
    pending or withheld analysis never implies "懸念なし". The line sits
    beside, and never rewrites, the deterministic screening figures.

    Args:
        analysis: The candidate's qualitative analysis section.

    Returns:
        `"⚠ 定性: 見送り推奨（…）"`, `"✓ 定性: 懸念なし"`, or `None`.
    """
    if analysis.degraded or analysis.verdict is None:
        return None
    if analysis.verdict == "skip":
        reason = (
            f"（{analysis.verdict_summary}）"
            if analysis.verdict_summary
            else "（理由の記載なし）"
        )
        return f"⚠ 定性: 見送り推奨{reason}"
    return "✓ 定性: 懸念なし"


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
    score_atr_pct: float | None
    score_pivot_proximity: float | None
    score_rs_percentile: float | None
    score_criteria_met: float | None
    signals: tuple[str, ...]
    fundamentals: BriefFundamentals
    risk: BriefRisk
    analysis: BriefAnalysis
    execution_state: str = "UNKNOWN"
    execution_distance: float | None = None


@dataclass(frozen=True, slots=True)
class BriefRejectionCount:
    """One `reason_code`'s tally for the 落選サマリ section (P1-02)."""

    reason_code: str
    count: int


@dataclass(frozen=True, slots=True)
class SignalPerformanceRow:
    """One signal's weighted hit-rate stats for the markdown aggregation (REQ-005/REQ-008).

    `true_positive_count`/`false_positive_count`/`neutral_count` are RAW
    (unweighted) occurrence tallies -- the issue's "TP/FP/NEUTRAL件数" reads
    as a literal count column, distinct from `hit_rate`, which alone is
    horizon-weighted. `n` is also raw and includes NEUTRAL occurrences: the
    issue's own "n=15" preliminary-sample example counts every occurrence of
    a signal, not just its TP/FP ones.
    """

    signal_name: str
    true_positive_count: int
    false_positive_count: int
    neutral_count: int
    hit_rate: float | None
    n: int
    is_preliminary: bool


@dataclass(frozen=True, slots=True)
class BriefTrackedRow:
    """One published proceed recommendation and its latest ledger mark."""

    symbol: str
    run_id: UUID
    entry_date: date
    entry_price: float
    last_close: float | None
    unrealized_return_pct: float | None
    stop_price: float | None
    status: str
    exit_date: date | None
    exit_reason: str | None
    days_held: int
    days_remaining: int | None


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
    # Run-level qualitative stand-down, set only by an ingested analysis. It
    # is displayed before anything else and never suppresses the screening
    # numbers, which remain the code's own deterministic output.
    no_trade: bool = False
    no_trade_reason: str | None = None
    provider_name: str = "yfinance"
    data_tier: str = "prototype"
    tracked: tuple[BriefTrackedRow, ...] = ()


@dataclass(frozen=True, slots=True)
class DailyBriefContext:
    """Inputs required to assemble a `DailyBrief`."""

    run_id: UUID
    run_date: date
    generated_at: datetime
    universe: tuple[UniverseMember, ...]
    candidates: list[Candidate]
    risk_assessments: list[RiskAssessment]
    # Verified skill output, or `None` for a plain `copilot-daily` run whose
    # qualitative sections are still pending analysis.
    analysis: ValidatedAnalysis | None
    # The single strategy this run screened with -- today's `Candidate`
    # objects don't carry a per-candidate strategy_key (one run == one strategy).
    strategy_key: str
    rejections: list[RejectionRecord] = field(default_factory=list)
    notices: tuple[str, ...] = ()
    # P2-11: computed by `pipeline/postmortem.py`'s `run_postmortem_step()`,
    # threaded straight through to `DailyBrief` (mirrors `notices` above).
    signal_performance: tuple[SignalPerformanceRow, ...] = ()
    regime_snapshot: RegimeSnapshot | None = None
    exposure_decision: ExposureDecision | None = None
    ftd_snapshot: FtdSnapshot | None = None
    provider_name: str = "yfinance"
    data_tier: str = "prototype"
    tracked: tuple[BriefTrackedRow, ...] = ()


@dataclass(frozen=True, slots=True)
class _CandidateContext:
    member: UniverseMember | None
    assessment: RiskAssessment | None
    brief: DailyBriefContext


def build_daily_brief(
    context: DailyBriefContext, market_store: MarketStore
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
        )
        for candidate in context.candidates
    )
    return DailyBrief(
        run_id=context.run_id,
        run_date=context.run_date,
        generated_at=context.generated_at,
        market=_market_items(market_store, context.run_date),
        candidates=candidates,
        regime=_regime_brief(context.regime_snapshot, context.ftd_snapshot),
        exposure=_exposure_brief(context.exposure_decision),
        rejection_counts=_rejection_counts(context.rejections),
        notices=context.notices,
        signal_performance=context.signal_performance,
        no_trade=context.analysis.no_trade if context.analysis else False,
        no_trade_reason=(
            context.analysis.no_trade_reason if context.analysis else None
        ),
        provider_name=context.provider_name,
        data_tier=context.data_tier,
        tracked=context.tracked,
    )


def _regime_brief(
    snapshot: RegimeSnapshot | None, ftd_snapshot: FtdSnapshot | None
) -> BriefRegime | None:
    if snapshot is None:
        return None
    return BriefRegime(
        gate=snapshot.gate.verdict.value,
        dd_level=snapshot.dd_level.value,
        spy_d25=snapshot.spy_distribution.d25,
        qqq_d25=snapshot.qqq_distribution.d25,
        data_quality=snapshot.data_quality.value,
        spy_close=snapshot.gate.spy_close,
        spy_sma200=snapshot.gate.spy_sma200,
        spy_trend_gap_pct=_relative_gap(
            snapshot.gate.spy_close, snapshot.gate.spy_sma200
        ),
        spy_ftd_state=ftd_snapshot.spy.state.value if ftd_snapshot else None,
        spy_ftd_day_number=ftd_snapshot.spy.day_number if ftd_snapshot else None,
        spy_ftd_quality_score=ftd_snapshot.spy.quality_score if ftd_snapshot else None,
        qqq_ftd_state=ftd_snapshot.qqq.state.value if ftd_snapshot else None,
        qqq_ftd_day_number=ftd_snapshot.qqq.day_number if ftd_snapshot else None,
        qqq_ftd_quality_score=ftd_snapshot.qqq.quality_score if ftd_snapshot else None,
    )


def _relative_gap(close: float | None, trend: float | None) -> float | None:
    """Return the code-owned close-to-trend gap for the regime display."""
    if close is None or trend is None or trend == 0.0:
        return None
    return close / trend - 1.0


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
    candidate: Candidate, context: _CandidateContext, market_store: MarketStore
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
        score_atr_pct=candidate.metrics.get("score_atr_pct"),
        score_pivot_proximity=candidate.metrics.get("score_pivot_proximity"),
        score_rs_percentile=candidate.metrics.get("score_rs_percentile"),
        score_criteria_met=candidate.metrics.get("score_criteria_met"),
        signals=tuple(
            _signal_label(name, candidate.metrics)
            for name in candidate.signal_names
            if name not in _HIDDEN_SIGNALS
        ),
        fundamentals=_fundamentals(
            market_store, candidate.symbol, context.brief.run_date, close
        ),
        risk=_risk_brief(context.assessment),
        analysis=build_analysis_brief(candidate.symbol, context.brief.analysis),
        execution_state=candidate.execution_state,
        execution_distance=candidate.execution_distance,
    )


def _signal_label(name: str, metrics: Mapping[str, float]) -> str:
    """Return an evidence-bearing signal label for terminal and Markdown."""
    label = _SIGNAL_LABELS.get(name, name)
    if name == "minervini_stage2":
        criteria = metrics.get("minervini_criteria_met")
        if criteria is not None:
            return f"{label} ({int(criteria)}/7条件)"
    if name == "vcp_breakout":
        count = metrics.get("vcp_contraction_count")
        dry_up = metrics.get("vcp_dry_up_ratio")
        pivot = metrics.get("vcp_pivot")
        if count is not None and dry_up is not None and pivot is not None:
            return (
                f"{label} ({int(count)}収縮 / dry-up {dry_up:.2f} / pivot {pivot:.2f})"
            )
    return label


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
        return BriefRisk("not_calculable", None, None, None, None, None, (), ())
    return BriefRisk(
        status=assessment.status,
        entry_price=assessment.entry_price,
        limit_price=assessment.limit_price,
        stop_price=assessment.stop_price,
        atr14=assessment.atr14,
        stop_distance_pct=assessment.stop_distance_pct,
        reasons=assessment.reasons,
        warnings=assessment.warnings,
        binding_constraint=assessment.binding_constraint,
    )


def build_analysis_brief(
    symbol: str, analysis: ValidatedAnalysis | None
) -> BriefAnalysis:
    """Map one symbol's verified analysis into presentation data, fail-closed.

    Every unhappy path collapses to `degraded=True` with an explanatory
    `conclusion` rather than a partially rendered section: analysis not run
    yet (`analysis is None`), a defensive hand-constructed analysis lacks the
    symbol, or the symbol's analysis is withheld by `analysis/validate.py`.
    """
    if analysis is None:
        return BriefAnalysis(True, PENDING_ANALYSIS_MESSAGE)
    outcome = analysis.for_symbol(symbol)
    if outcome is None:
        return BriefAnalysis(True, MISSING_ANALYSIS_MESSAGE)
    if outcome.error is not None:
        return BriefAnalysis(True, WITHHELD_ANALYSIS_MESSAGE)
    return _build_verified_analysis_brief(outcome, analysis.source_urls)


def _build_verified_analysis_brief(
    outcome: SymbolOutcome, urls: Mapping[str, str]
) -> BriefAnalysis:
    news = outcome.news_summary
    assessment = outcome.screening_assessment
    facts = (
        *tuple(news.facts if news else ()),
        *(fact for filing in outcome.filings for fact in filing.analysis.facts),
    )
    flags = (
        *tuple(news.risk_flags if news else ()),
        *(flag for filing in outcome.filings for flag in filing.analysis.red_flags),
    )
    return BriefAnalysis(
        degraded=False,
        conclusion=_conclusion(outcome),
        facts=tuple(fact.text for fact in facts),
        risk_flags=flags,
        sources=_sources(facts, urls),
        filings=tuple(_filing_brief(filing, urls) for filing in outcome.filings),
        verdict=outcome.verdict.recommendation if outcome.verdict else None,
        verdict_summary=_verdict_summary(outcome),
        strengths=tuple(assessment.strengths) if assessment else (),
        concerns=tuple(assessment.concerns) if assessment else (),
        news_supply=_news_supply_brief(outcome.news_supply),
    )


def _news_supply_brief(supply: NewsSupply | None) -> BriefNewsSupply | None:
    """Copy the code-owned news-supply measurement into presentation data."""
    if supply is None:
        return None
    return BriefNewsSupply(
        level=supply.level,
        collected_items=supply.collected_items,
        exported_items=supply.exported_items,
        symbol_mention_items=supply.symbol_mention_items,
    )


def _conclusion(outcome: SymbolOutcome) -> str:
    """Prefer the screening assessment; fall back to the first interpretation."""
    if outcome.screening_assessment is not None:
        summary = outcome.screening_assessment.summary.strip()
        if summary:
            return summary
    interpretations = (
        *tuple(outcome.news_summary.interpretation if outcome.news_summary else ()),
        *(
            text
            for filing in outcome.filings
            for text in filing.analysis.interpretation
        ),
    )
    return interpretations[0] if interpretations else _NEUTRAL_ANALYSIS_MESSAGE


def _verdict_summary(outcome: SymbolOutcome) -> str | None:
    """The leading verdict reason, used as the inline "（理由要約）"."""
    if outcome.verdict is None or not outcome.verdict.reasons:
        return None
    return outcome.verdict.reasons[0].text


def _filing_brief(
    filing: ResolvedFiling, urls: Mapping[str, str]
) -> BriefFilingAnalysis:
    analysis = filing.analysis
    facts = tuple(analysis.facts)
    return BriefFilingAnalysis(
        filing_type=filing.form_type,
        filed_at=filing.filed_at,
        facts=tuple(fact.text for fact in facts),
        interpretation=tuple(analysis.interpretation),
        red_flags=tuple(analysis.red_flags),
        yoy_changes=tuple(analysis.yoy_changes),
        sources=_sources(facts, urls),
    )


def _sources(
    facts: Sequence[SourcedFact], urls: Mapping[str, str]
) -> tuple[BriefSource, ...]:
    source_ids = [source_id for fact in facts for source_id in fact.source_ids]
    return _sources_for_ids(source_ids, urls)


def _sources_for_ids(
    source_ids: Sequence[str], urls: Mapping[str, str]
) -> tuple[BriefSource, ...]:
    """Resolve cited IDs to links using the exported input's own URLs.

    Deliberately not a database read: ingest must work from the archived JSON
    alone, and a URL the pipeline itself collected is the only trustworthy one.
    """
    unique_ids = list(dict.fromkeys(source_ids))
    return tuple(
        BriefSource(source_id, url)
        for source_id in unique_ids
        if (url := urls.get(source_id)) is not None
    )
