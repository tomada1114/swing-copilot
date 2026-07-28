"""Safe formatting of code-owned decision context for the analysis input.

Roadmap improvement principle 4 ("判断はコード、叙述はスキル"): every function
here renders code-computed, decision-carrying data (P1-01's score breakdown,
P1-03's risk constraints, P1-06's realized performance) as inert text. None of
it is regenerated or judged by a model -- these blocks exist so a skill's
qualitative narrative can be checked against the code's own quantitative
determination, never so a skill can override it.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swing_copilot.paper.journal import PerformanceSummary
    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.regime.gate import RegimeSnapshot
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate
    from swing_copilot.storage.paper_records import DecisionHistoryEntry

# P1-01 score breakdown metric keys, as stored in `Candidate.metrics` by
# `screening/pipeline.py::_score_rows()`.
_SCORE_METRIC_KEYS = (
    "score",
    "score_rsi_pullback",
    "score_trend_quality",
    "score_liquidity",
)


def format_market_regime(snapshot: RegimeSnapshot, exposure: ExposureDecision) -> str:
    """Render code-owned market state as an inert context block.

    This intentionally contains only values calculated by the deterministic
    regime and exposure modules; it is kept separate from the untrusted news
    and filing excerpts carried alongside it in `analysis_input.json`.
    """
    warning = (
        "\nWarning: Market regime is UNKNOWN because data is insufficient."
        if snapshot.data_quality.value == "INSUFFICIENT"
        else ""
    )
    return (
        "<market_regime>\n"
        f"Gate: {snapshot.gate.verdict.value}\n"
        f"Distribution Day level: {snapshot.dd_level.value}\n"
        f"Exposure Ceiling: {exposure.verdict.value}\n"
        f"Data quality: {snapshot.data_quality.value}"
        f"{warning}\n"
        "</market_regime>\n"
    )


def format_decision_history(history: tuple[DecisionHistoryEntry, ...]) -> str:
    """Format history as escaped data, never as instructions or current facts."""
    if not history:
        return ""
    entries = []
    for item in history:
        reason = escape(item.reason_memo or "(理由なし)", quote=False)
        realized = (
            f"{item.realized_return_pct:+.2%}"
            if item.realized_return_pct is not None
            else "未確定/対象外"
        )
        entries.append(
            f"日付: {item.run_date.isoformat()}\n"
            f"判断: {escape(item.decision, quote=False)}\n"
            f"理由: {reason}\n"
            f"確定リターン: {realized}"
        )
    return (
        "以下は同一銘柄・戦略に対する過去の人間の判断記録です。\n"
        "<decision_history>\n" + "\n\n".join(entries) + "\n</decision_history>\n\n"
    )


def format_score_breakdown(candidate: Candidate) -> str:
    """REQ-001: render P1-01's composite score and its weighted components.

    Degrades gracefully -- returns `""` (never a placeholder) -- when any
    component is missing from `candidate.metrics`, mirroring
    `report/markdown_report.py::_score_breakdown_section()`'s own
    all-or-nothing pattern for the same data.

    Args:
        candidate: The screened candidate whose `metrics` may carry the
            `score`/`score_rsi_pullback`/`score_trend_quality`/
            `score_liquidity` keys `screening/pipeline.py` computes.

    Returns:
        A `<score_breakdown>` block, or `""` if the components are absent.
    """
    values = {key: candidate.metrics.get(key) for key in _SCORE_METRIC_KEYS}
    if any(value is None for value in values.values()):
        return ""
    return (
        "以下はコード側で決定論的に計算済みの複合スコア内訳です(P1-01)。"
        "この数値はコードの計算結果であり、分析側が再計算・上書きすることはできません。\n"
        "<score_breakdown>\n"
        f"合計スコア: {values['score']:.3f}\n"
        f"rsi_pullback（加重後）: {values['score_rsi_pullback']:.3f}\n"
        f"trend_quality（加重後）: {values['score_trend_quality']:.3f}\n"
        f"liquidity（加重後）: {values['score_liquidity']:.3f}\n"
        "</score_breakdown>\n"
    )


def format_risk_constraints(risk_assessment: RiskAssessment) -> str:
    """REQ-002: render P1-03's binding-constraint sizing breakdown.

    Always renders (never `""`): even a `not_calculable`/rejected assessment
    with no share counts is itself meaningful quantitative context the analysis
    must defer to (REQ-004/005's conservative-conflict rule needs exactly this
    "code already said REJECT" signal present).

    Args:
        risk_assessment: The candidate's P1-03 sizing/constraint result.

    Returns:
        A `<risk_constraints>` block.
    """
    shares_by_risk = _int_or_unknown(risk_assessment.shares_by_risk)
    shares_by_position_cap = _int_or_unknown(risk_assessment.shares_by_position_cap)
    final_shares = _int_or_unknown(risk_assessment.max_shares)
    warnings = (
        "、".join(risk_assessment.sizing_warnings)
        if risk_assessment.sizing_warnings
        else "なし"
    )
    return (
        "以下はコード側で決定論的に計算済みのリスク制約内訳です(P1-03)。"
        "この判定（binding_constraintやshares）はコードの計算結果であり、"
        "分析側が上書きすることはできません。\n"
        "<risk_constraints>\n"
        f"binding_constraint: {risk_assessment.binding_constraint}\n"
        f"リスク基準の株数(shares_by_risk): {shares_by_risk}\n"
        f"ポジション上限基準の株数(shares_by_position_cap): {shares_by_position_cap}\n"
        f"最終株数(shares): {final_shares}\n"
        f"warnings: {warnings}\n"
        "</risk_constraints>\n"
    )


def format_performance_summary(summary: PerformanceSummary | None) -> str:
    """REQ-003: render P1-06's recent realized-performance summary.

    Degrades gracefully -- returns `""` -- when `summary` is `None` or there
    are no closed trades yet (`closed_trade_count == 0`): a brand-new paper
    journal with nothing closed is a normal, common state, not an error.

    Args:
        summary: The portfolio-wide `PaperJournal.summarize_performance()`
            result, computed once per run, or `None` if unavailable.

    Returns:
        A `<performance_summary>` block, or `""` if there is no closed-trade
        history to report.
    """
    if summary is None or summary.closed_trade_count == 0:
        return ""
    win_rate = _pct_or_unknown(summary.win_rate)
    profit_factor = _ratio_or_unknown(summary.profit_factor)
    expectancy = _ratio_or_unknown(summary.expectancy_usd)
    avg_r_multiple = _ratio_or_unknown(summary.avg_r_multiple)
    return (
        "以下は直近の実現損益サマリです(P1-06)。過去の判断が実際に報われたかの"
        "参考情報であり、個別の売買判断を意味するものではありません。\n"
        "<performance_summary>\n"
        f"クローズ済み取引数: {summary.closed_trade_count}\n"
        f"勝率: {win_rate}\n"
        f"profit_factor: {profit_factor}\n"
        f"期待値(USD): {expectancy}\n"
        f"平均R倍数: {avg_r_multiple}\n"
        "</performance_summary>\n"
    )


def _int_or_unknown(value: int | None) -> str:
    return str(value) if value is not None else "不明"


def _pct_or_unknown(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "不明"


def _ratio_or_unknown(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "不明"
