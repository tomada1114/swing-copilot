"""CLI-first daily brief rendering and Markdown archival contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

from swing_copilot.config import ScoreWeights
from swing_copilot.models import DataTier, RunStatus
from swing_copilot.report.daily_brief import (
    BriefAnalysis,
    BriefCandidate,
    BriefExposure,
    BriefFilingAnalysis,
    BriefFundamentals,
    BriefMarketItem,
    BriefNewsSupply,
    BriefRegime,
    BriefRejectionCount,
    BriefRisk,
    BriefSource,
    DailyBrief,
    SignalPerformanceRow,
)
from swing_copilot.report.markdown_report import (
    _SCORE_BREAKDOWN_COMPONENTS,
    render_markdown,
    write_markdown_report,
)
from swing_copilot.report.terminal_report import (
    TerminalPaths,
    TerminalRunSummary,
    render_run_summary,
    render_terminal,
)

RUN_ID = UUID("11111111-2222-3333-4444-555555555555")


def _brief() -> DailyBrief:
    return DailyBrief(
        run_id=RUN_ID,
        run_date=date(2026, 7, 22),
        generated_at=datetime(2026, 7, 22, 12, tzinfo=UTC),
        market=(BriefMarketItem("SPY", 638.25, 0.006),),
        candidates=(
            BriefCandidate(
                rank=1,
                symbol="NVDA",
                company_name="NVIDIA Corporation",
                close=171.20,
                pct_change=0.012,
                rsi14=48.2,
                atr14=4.1,
                score=0.627,
                score_rsi_pullback=0.167,
                score_trend_quality=0.300,
                score_liquidity=0.160,
                score_atr_pct=0.000,
                score_pivot_proximity=0.000,
                score_rs_percentile=0.000,
                score_criteria_met=0.000,
                signals=("RSI押し目",),
                fundamentals=BriefFundamentals(
                    per="41.2x", fcf="$12,000", equity_ratio="52%", eps="$4.16"
                ),
                risk=BriefRisk(
                    status="approved",
                    entry_price=171.20,
                    limit_price=172.00,
                    stop_price=164.80,
                    atr14=4.1,
                    stop_distance_pct=(172.00 - 164.80) / 172.00,
                    reasons=(),
                    warnings=("AMDとの相関 0.82",),
                ),
                analysis=BriefAnalysis(
                    degraded=False,
                    conclusion="業績見通しは維持されているが規制リスクが残る",
                    facts=("売上高は前年同期比で増加した",),
                    risk_flags=("規制環境の不確実性",),
                    sources=(BriefSource("news:123", "https://example.com/news/123"),),
                ),
            ),
        ),
        notices=("FREDカレンダーを取得できませんでした",),
    )


def _brief_with_sizing(risk: BriefRisk) -> DailyBrief:
    base = _brief()
    candidate = replace(base.candidates[0], risk=risk)
    return replace(base, candidates=(candidate,))


def _brief_with_analysis(analysis: BriefAnalysis) -> DailyBrief:
    base = _brief()
    candidate = replace(base.candidates[0], analysis=analysis)
    return replace(base, candidates=(candidate,))


def test_terminal_and_markdown_show_market_regime_before_candidates() -> None:
    brief = replace(
        _brief(),
        regime=BriefRegime(
            gate="BULL",
            dd_level="NORMAL",
            spy_d25=1.0,
            qqq_d25=1.5,
            data_quality="OK",
            spy_ftd_state="FTD_CONFIRMED",
            spy_ftd_day_number=5,
            spy_ftd_quality_score=70,
            qqq_ftd_state="DAY2_3",
            qqq_ftd_day_number=3,
        ),
    )

    terminal = render_terminal(brief, RunStatus.SUCCESS, width=200)
    markdown = render_markdown(brief, RunStatus.SUCCESS)

    assert terminal.index("Market regime") < terminal.index("銘柄")
    assert "Gate: BULL" in terminal
    assert "FTD" in terminal
    assert "quality 70" in terminal
    assert markdown.index("## Market regime") < markdown.index("## Candidates")
    assert "Distribution Day level: `NORMAL`" in markdown
    assert "FTD SPY: `FTD_CONFIRMED`（Day5、品質スコア 70）" in markdown
    assert "成功率" not in markdown


def test_terminal_and_markdown_show_execution_buckets_and_distance() -> None:
    base = _brief()
    candidate = replace(
        base.candidates[0], execution_state="FAIR", execution_distance=1.0
    )
    brief = replace(base, candidates=(candidate,))

    terminal = render_terminal(brief, RunStatus.SUCCESS, width=200)
    markdown = render_markdown(brief, RunStatus.SUCCESS)

    assert "即検討可: NVDA" in terminal
    assert "様子見: 該当なし" in terminal
    assert "見送り: 該当なし" in terminal
    assert "FAIR (d=1.00)" not in terminal
    assert "### 即検討可" in markdown
    assert "### 様子見" in markdown
    assert "### 見送り" in markdown
    assert "FAIR (d=1.00)" in markdown


def test_markdown_candidates_table_is_self_contained_per_bucket() -> None:
    # P6-28 (a): each non-empty execution bucket renders its own complete
    # table (heading -> header row -> separator row -> data rows), not one
    # table interrupted midstream by a bucket heading -- the latter breaks
    # Markdown table rendering, since a heading between the separator and
    # the data rows ends the table.
    base = _brief()
    pullback = replace(
        base.candidates[0],
        symbol="NVDA",
        execution_state="PULLBACK_ZONE",
        execution_distance=0.1,
    )
    extended = replace(
        base.candidates[0],
        symbol="AMD",
        rank=2,
        execution_state="EXTENDED",
        execution_distance=2.0,
    )
    brief = replace(base, candidates=(pullback, extended))

    markdown = render_markdown(brief, RunStatus.SUCCESS)
    lines = markdown.splitlines()

    header = (
        "| Rank | Symbol | Close | Change | RSI14 | Score | Execution | "
        "Signals | Risk | 1R | Stop | Limit |"
    )
    separator = "|---:|---|---:|---:|---:|---:|---|---|---|---:|---:|---:|"
    header_indices = [index for index, line in enumerate(lines) if line == header]

    # One populated bucket (即検討可) gets one table; the other populated
    # bucket (様子見) gets its own table; 見送り stays empty.
    assert len(header_indices) == 2
    for index in header_indices:
        assert lines[index + 1] == separator
        data_row = lines[index + 2]
        assert data_row.startswith("| ")
        assert not data_row.startswith("###")
    assert "NVDA" in lines[header_indices[0] + 2]
    assert "AMD" in lines[header_indices[1] + 2]


def test_markdown_empty_bucket_keeps_no_match_placeholder() -> None:
    # P6-28 (b): a bucket with no candidates keeps the plain "該当なし"
    # placeholder (no empty/broken table).
    base = _brief()
    candidate = replace(
        base.candidates[0], execution_state="PULLBACK_ZONE", execution_distance=0.1
    )
    brief = replace(base, candidates=(candidate,))

    markdown = render_markdown(brief, RunStatus.SUCCESS)
    lines = markdown.splitlines()

    heading_index = lines.index("### 見送り")
    # Heading, blank line, then the placeholder immediately -- no header
    # row/separator for an empty bucket.
    assert lines[heading_index + 1] == ""
    assert lines[heading_index + 2] == "該当なし"


def test_terminal_and_markdown_show_exposure_before_candidates() -> None:
    brief = replace(
        _brief(),
        exposure=BriefExposure(
            verdict="CASH_PRIORITY",
            gate="BEAR",
            dd_level="SEVERE",
            data_quality="OK",
            is_conservatively_downgraded=False,
        ),
    )

    terminal = render_terminal(brief, RunStatus.SUCCESS, width=200)
    markdown = render_markdown(brief, RunStatus.SUCCESS)

    assert terminal.index("Exposure Ceiling") < terminal.index("銘柄")
    assert "CASH_PRIORITY" in terminal
    assert markdown.index("## Exposure Ceiling") < markdown.index("## Candidates")
    assert "Verdict: `CASH_PRIORITY`" in markdown


def test_terminal_and_markdown_show_earnings_warning_for_candidate() -> None:
    risk = BriefRisk(
        status="approved",
        entry_price=100.0,
        limit_price=100.0,
        stop_price=95.0,
        atr14=2.0,
        stop_distance_pct=0.05,
        reasons=(),
        warnings=("EARNINGS_PROXIMITY_WARN: 5 business days until 2026-07-28",),
    )

    terminal = render_terminal(_brief_with_sizing(risk), RunStatus.SUCCESS, width=200)
    markdown = render_markdown(_brief_with_sizing(risk), RunStatus.SUCCESS)

    assert "EARNINGS_PROXIMITY_WARN" in terminal
    assert "EARNINGS_PROXIMITY_WARN" in markdown


def test_terminal_and_markdown_show_one_r_without_account_sections() -> None:
    terminal = render_terminal(_brief(), RunStatus.SUCCESS, width=200)
    markdown = render_markdown(_brief(), RunStatus.SUCCESS)

    for output in (terminal, markdown):
        assert "4.19%" in output
        assert "Shares" not in output
        assert "株" not in output
        assert "Portfolio heat" not in output
        assert "Portfolio risk" not in output
        assert "Circuit Breaker" not in output
        assert "SMALL_ACCOUNT_FRICTION" not in output
        assert "REGIME_REDUCE_ONLY_RISK_HALVED" not in output


def test_reduce_only_keeps_candidates_and_explains_warning() -> None:
    brief = replace(
        _brief(),
        exposure=BriefExposure(
            verdict="REDUCE_ONLY",
            gate="CAUTION",
            dd_level="NORMAL",
            data_quality="OK",
            is_conservatively_downgraded=False,
        ),
    )

    terminal = render_terminal(brief, RunStatus.SUCCESS, width=200)
    markdown = render_markdown(brief, RunStatus.SUCCESS)

    for output in (terminal, markdown):
        assert "相場は警戒状態\uff1a新規は控えめに" in output
        assert "NVDA" in output


def test_cash_priority_keeps_candidates_as_market_skips() -> None:
    base = _brief()
    cash_risk = replace(
        base.candidates[0].risk,
        status="rejected",
        reasons=("REGIME_CASH_PRIORITY",),
        binding_constraint="regime",
    )
    brief = replace(
        base,
        candidates=(replace(base.candidates[0], risk=cash_risk),),
        exposure=BriefExposure(
            verdict="CASH_PRIORITY",
            gate="BEAR",
            dd_level="SEVERE",
            data_quality="OK",
            is_conservatively_downgraded=False,
        ),
    )

    terminal = render_terminal(brief, RunStatus.SUCCESS, width=200)
    markdown = render_markdown(brief, RunStatus.SUCCESS)

    for output in (terminal, markdown):
        assert "NVDA" in output
        assert "見送り（地合い）" in output


def test_terminal_output_is_a_compact_decision_brief() -> None:
    report_path = Path("reports/2026-07-22/report.md")
    output = render_terminal(
        _brief(),
        RunStatus.DEGRADED,
        width=120,
        color=False,
        paths=TerminalPaths(report=report_path),
    )

    assert "Swing Copilot" in output
    assert "2026-07-22" in output
    assert "DEGRADED" in output
    assert "NVDA" in output
    assert "171.20" in output
    assert "順位" not in output
    assert "銘柄" in output
    assert "終値" in output
    assert "前日比" in output
    assert "スコア" in output
    assert "1R" in output
    assert "株数" not in output
    assert "ストップ" in output
    assert "指値" in output
    assert "approved" not in output
    assert "Breakdown" not in output
    assert "Execution" not in output
    assert "PULLBACK_ZONE" not in output
    assert "落選サマリ" not in output
    assert "業績見通しは維持されているが規制リスクが残る" in output
    assert "FREDカレンダーを取得できませんでした" in output
    assert output.rstrip().endswith(f"詳細レポート: {report_path}")
    assert "<html" not in output


def test_terminal_shows_analysis_input_path_when_provided() -> None:
    report_path = Path("reports/2026-07-22/report.md")
    analysis_input_path = Path("reports/2026-07-22/analysis_input.json")
    output = render_terminal(
        _brief(),
        RunStatus.SUCCESS,
        width=120,
        paths=TerminalPaths(report=report_path, analysis_input=analysis_input_path),
    )

    assert f"詳細レポート: {report_path}" in output
    assert f"分析入力(analysis_input.json): {analysis_input_path}" in output


def test_terminal_omits_analysis_input_path_when_absent() -> None:
    output = render_terminal(_brief(), RunStatus.SUCCESS, width=120)

    assert "analysis_input.json" not in output


def test_markdown_contains_auditable_details_and_source_urls() -> None:
    output = render_markdown(_brief(), RunStatus.SUCCESS)

    assert (
        "<!-- Generated by swing-copilot; DuckDB is the source of truth. -->" in output
    )
    assert "# Swing Copilot — 2026-07-22" in output
    assert str(RUN_ID) in output
    assert "## NVDA" in output
    assert "売上高は前年同期比で増加した" in output
    assert "[news:123](https://example.com/news/123)" in output
    assert "本レポートは情報提供のみを目的とし、投資助言ではありません" in output


def _analysis_with_filing(**overrides: object) -> BriefAnalysis:
    filing = BriefFilingAnalysis(
        filing_type="10-Q",
        filed_at=date(2026, 7, 21),
        facts=("Revenue up 10%",),
        interpretation=("Growth appears steady",),
        red_flags=(),
        yoy_changes=(),
        sources=(BriefSource("filing:1", "https://example.com/filing/1"),),
    )
    base = BriefAnalysis(
        degraded=False,
        conclusion="conclusion",
        filings=(filing,),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_terminal_shows_each_filing_analysis_with_type_and_filed_date() -> None:
    # P6-27: previously only the first filing analysis per symbol ever
    # reached the report; now each is individually identified.
    output = render_terminal(
        _brief_with_analysis(_analysis_with_filing()), RunStatus.SUCCESS, width=200
    )

    assert "Filing [10-Q 2026-07-21]: Growth appears steady" in output


def test_markdown_shows_each_filing_analysis_in_its_own_labeled_section() -> None:
    output = render_markdown(
        _brief_with_analysis(_analysis_with_filing()), RunStatus.SUCCESS
    )

    assert "### 開示分析: 10-Q (2026-07-21)" in output
    assert "Revenue up 10%" in output
    assert "[filing:1](https://example.com/filing/1)" in output


def test_terminal_and_markdown_show_verdict_skip_line_with_reason() -> None:
    analysis = replace(
        _analysis_with_filing(), verdict="skip", verdict_summary="規制リスクが高い"
    )

    terminal = render_terminal(
        _brief_with_analysis(analysis), RunStatus.SUCCESS, width=200
    )
    markdown = render_markdown(_brief_with_analysis(analysis), RunStatus.SUCCESS)

    assert "⚠ 定性: 見送り推奨（規制リスクが高い）" in terminal
    assert "⚠ 定性: 見送り推奨（規制リスクが高い）" in markdown


def test_terminal_and_markdown_show_verdict_proceed_line() -> None:
    analysis = replace(_analysis_with_filing(), verdict="proceed")

    terminal = render_terminal(
        _brief_with_analysis(analysis), RunStatus.SUCCESS, width=200
    )
    markdown = render_markdown(_brief_with_analysis(analysis), RunStatus.SUCCESS)

    assert "✓ 定性: 懸念なし" in terminal
    assert "✓ 定性: 懸念なし" in markdown


def test_terminal_and_markdown_omit_verdict_line_when_analysis_degraded() -> None:
    # A pending/withheld analysis must never imply "懸念なし".
    analysis = replace(_analysis_with_filing(), degraded=True, verdict="proceed")

    terminal = render_terminal(
        _brief_with_analysis(analysis), RunStatus.SUCCESS, width=200
    )
    markdown = render_markdown(_brief_with_analysis(analysis), RunStatus.SUCCESS)

    assert "懸念なし" not in terminal
    assert "懸念なし" not in markdown


def test_terminal_and_markdown_omit_verdict_line_when_absent() -> None:
    terminal = render_terminal(
        _brief_with_analysis(_analysis_with_filing()), RunStatus.SUCCESS, width=200
    )
    markdown = render_markdown(
        _brief_with_analysis(_analysis_with_filing()), RunStatus.SUCCESS
    )

    assert "定性:" in terminal
    assert "懸念なし" not in terminal
    assert "見送り推奨" not in terminal
    assert "懸念なし" not in markdown
    assert "見送り推奨" not in markdown


def test_terminal_shows_each_concern_line() -> None:
    analysis = replace(_analysis_with_filing(), concerns=("規制強化の兆し", "在庫増加"))

    output = render_terminal(
        _brief_with_analysis(analysis), RunStatus.SUCCESS, width=200
    )

    assert "懸念: 規制強化の兆し" in output
    assert "懸念: 在庫増加" in output


def test_markdown_shows_qualitative_assessment_section_with_strengths_and_concerns() -> (
    None
):
    analysis = replace(
        _analysis_with_filing(),
        strengths=("トレンド継続",),
        concerns=("バリュエーション高め",),
    )

    output = render_markdown(_brief_with_analysis(analysis), RunStatus.SUCCESS)

    assert "### 定性評価" in output
    assert "- 強み: トレンド継続" in output
    assert "- 懸念: バリュエーション高め" in output


def test_markdown_omits_qualitative_assessment_section_when_empty() -> None:
    output = render_markdown(
        _brief_with_analysis(_analysis_with_filing()), RunStatus.SUCCESS
    )

    assert "### 定性評価" not in output


class TestMarkdownDistinguishesNewsSuppressionFromZero:
    """Issue #281: 「抑制された」と「そもそも0件だった」の区別をレポート上で固定する.

    AC14 and `analyze-news/SKILL.md` keep `news_summary: null` whenever
    `news[]` is empty, which is true in *both* cases below, so the report
    must draw the distinction itself from the code-owned `news_supply`
    counts -- not from anything the skill wrote.
    """

    def test_suppressed_news_is_labeled_differently_from_zero_collected(self) -> None:
        suppressed = replace(
            _analysis_with_filing(),
            news_supply=BriefNewsSupply(
                level="sparse",
                collected_items=8,
                exported_items=8,
                symbol_mention_items=1,
            ),
        )
        zero = replace(
            _analysis_with_filing(),
            news_supply=BriefNewsSupply(
                level="none",
                collected_items=0,
                exported_items=0,
                symbol_mention_items=0,
            ),
        )

        suppressed_output = render_markdown(
            _brief_with_analysis(suppressed), RunStatus.SUCCESS
        )
        zero_output = render_markdown(_brief_with_analysis(zero), RunStatus.SUCCESS)

        assert suppressed_output != zero_output
        assert "抑制" in suppressed_output
        assert "抑制" not in zero_output
        assert "そもそも" in zero_output
        assert "そもそも" not in suppressed_output
        assert "collected_items: 8" in suppressed_output
        assert "collected_items: 0" in zero_output

    def test_a_sufficient_news_supply_is_also_shown(self) -> None:
        analysis = replace(
            _analysis_with_filing(),
            news_supply=BriefNewsSupply(
                level="sufficient",
                collected_items=12,
                exported_items=10,
                symbol_mention_items=6,
            ),
        )

        output = render_markdown(_brief_with_analysis(analysis), RunStatus.SUCCESS)

        assert "抑制" not in output
        assert "そもそも" not in output
        assert "collected_items: 12" in output

    def test_news_supply_line_is_omitted_when_absent(self) -> None:
        output = render_markdown(_brief(), RunStatus.SUCCESS)

        assert "News supply" not in output


def test_markdown_shows_qualitative_risk_flags_heading_not_the_old_llm_heading() -> (
    None
):
    analysis = replace(_analysis_with_filing(), risk_flags=("規制環境の不確実性",))

    output = render_markdown(_brief_with_analysis(analysis), RunStatus.SUCCESS)

    assert "### 定性リスクフラグ" in output
    assert "LLM risk flags" not in output


def test_terminal_and_markdown_show_no_trade_banner_near_the_top() -> None:
    brief = replace(_brief(), no_trade=True, no_trade_reason="市場環境が悪化")

    terminal = render_terminal(brief, RunStatus.SUCCESS, width=200)
    markdown = render_markdown(brief, RunStatus.SUCCESS)

    assert "本日は取引なし（定性判断）（市場環境が悪化）" in terminal
    assert terminal.index("本日は取引なし") < terminal.index("銘柄")
    assert "> **本日は取引なし（定性判断）**（市場環境が悪化）" in markdown
    assert markdown.index("本日は取引なし") < markdown.index("## Candidates")


def test_terminal_and_markdown_omit_no_trade_banner_by_default() -> None:
    terminal = render_terminal(_brief(), RunStatus.SUCCESS, width=200)
    markdown = render_markdown(_brief(), RunStatus.SUCCESS)

    assert "本日は取引なし" not in terminal
    assert "本日は取引なし" not in markdown


def test_terminal_shows_score_column_without_breakdown() -> None:
    output = render_terminal(_brief(), RunStatus.SUCCESS, width=200, color=False)

    assert "0.627" in output
    assert "rsi 0.17 / trend 0.30 / liq 0.16" not in output


def test_markdown_shows_score_column_and_breakdown_table() -> None:
    # REQ-008
    output = render_markdown(_brief(), RunStatus.SUCCESS)

    assert "| 1 | NVDA |" in output
    assert "0.627" in output
    assert "### Score breakdown" in output
    assert "| rsi_pullback | 0.167 |" in output
    assert "| trend_quality | 0.300 |" in output
    assert "| liquidity | 0.160 |" in output
    assert "| atr_pct | 0.000 |" in output
    # Issue #251: the strategy-specific components are part of the same
    # all-or-nothing table, so a weighted one is never invisible to the
    # operator reading why a candidate ranked where it did.
    assert "| pivot_proximity | 0.000 |" in output
    assert "| rs_percentile | 0.000 |" in output
    assert "| criteria_met | 0.000 |" in output


def test_the_breakdown_rows_are_exactly_the_score_weights_fields() -> None:
    # `report/` must not import `config`, so the row list is hand-maintained.
    # Pin it against `ScoreWeights` instead: a component added there without a
    # row here is still summed into `score`, so the printed table would no
    # longer add up to the score printed beside it.
    assert tuple(ScoreWeights.model_fields) == _SCORE_BREAKDOWN_COMPONENTS


def _brief_without_candidates() -> DailyBrief:
    return replace(_brief(), candidates=())


def test_terminal_renders_empty_candidate_set_without_error() -> None:
    output = render_terminal(_brief_without_candidates(), RunStatus.SUCCESS, width=120)

    assert "Candidates: 0" in output
    assert "スコア" in output  # header still renders


def _brief_with_rejections() -> DailyBrief:
    return replace(
        _brief(),
        rejection_counts=(
            BriefRejectionCount("FILTER_LOW_LIQUIDITY", 3),
            BriefRejectionCount("SIGNAL_RSI_NOT_MET", 1),
        ),
    )


def test_terminal_omits_rejection_summary_counts() -> None:
    output = render_terminal(_brief_with_rejections(), RunStatus.SUCCESS, width=120)

    assert "落選サマリ" not in output
    assert "FILTER_LOW_LIQUIDITY" not in output
    assert "SIGNAL_RSI_NOT_MET" not in output


def test_markdown_shows_rejection_summary_table() -> None:
    output = render_markdown(_brief_with_rejections(), RunStatus.SUCCESS)

    assert "## 落選サマリ" in output
    assert "FILTER_LOW_LIQUIDITY" in output
    assert "| 3 |" in output
    assert "SIGNAL_RSI_NOT_MET" in output


def test_terminal_omits_empty_rejection_summary() -> None:
    output = render_terminal(_brief(), RunStatus.SUCCESS, width=120)

    assert "落選サマリ" not in output
    assert "0件" not in output


def test_markdown_renders_empty_rejection_summary_without_error() -> None:
    output = render_markdown(_brief(), RunStatus.SUCCESS)

    assert "## 落選サマリ" in output
    assert "0件" in output


def _brief_with_signal_performance() -> DailyBrief:
    return replace(
        _brief(),
        signal_performance=(
            SignalPerformanceRow(
                signal_name="trend_sma",
                true_positive_count=8,
                false_positive_count=3,
                neutral_count=2,
                hit_rate=0.625,
                n=13,
                is_preliminary=False,
            ),
            SignalPerformanceRow(
                signal_name="pullback_rsi",
                true_positive_count=4,
                false_positive_count=1,
                neutral_count=0,
                hit_rate=None,
                n=5,
                is_preliminary=True,
            ),
        ),
    )


def test_markdown_shows_signal_performance_table_with_preliminary_marker() -> None:
    # REQ-008/REQ-030: 的中率 rows, with "(暫定)" appended only for the
    # n < preliminary_sample_threshold row.
    output = render_markdown(_brief_with_signal_performance(), RunStatus.SUCCESS)

    assert "## シグナル成績（直近90日）" in output
    assert "| trend_sma | 8 | 3 | 2 | +62.50% | 13 |" in output
    assert "| pullback_rsi | 4 | 1 | 0 | N/A (暫定) | 5 |" in output


def test_markdown_renders_empty_signal_performance_without_error() -> None:
    output = render_markdown(_brief(), RunStatus.SUCCESS)

    assert "## シグナル成績（直近90日）" in output
    assert "該当なし(0件)" in output


def test_markdown_renders_empty_candidate_set_without_error() -> None:
    output = render_markdown(_brief_without_candidates(), RunStatus.SUCCESS)

    assert "## Candidates" in output
    assert "### Score breakdown" not in output


def _brief_with_missing_score() -> DailyBrief:
    brief = _brief()
    candidate = replace(
        brief.candidates[0],
        score=None,
        score_rsi_pullback=None,
        score_trend_quality=None,
        score_liquidity=None,
        score_atr_pct=None,
        score_pivot_proximity=None,
        score_rs_percentile=None,
        score_criteria_met=None,
    )
    return replace(brief, candidates=(candidate,))


def test_terminal_shows_na_when_score_is_missing() -> None:
    output = render_terminal(_brief_with_missing_score(), RunStatus.SUCCESS, width=200)

    assert "N/A" in output


def test_markdown_omits_breakdown_section_when_score_is_missing() -> None:
    output = render_markdown(_brief_with_missing_score(), RunStatus.SUCCESS)

    assert "### Score breakdown" not in output
    assert "N/A" in output


def test_markdown_is_written_per_run_and_latest_is_replaced(tmp_path: Path) -> None:
    path = write_markdown_report(_brief(), RunStatus.SUCCESS, tmp_path)

    assert path == tmp_path / "2026-07-22" / f"{RUN_ID}.md"
    assert path.is_file()
    assert (tmp_path / "latest.md").read_text(encoding="utf-8") == path.read_text(
        encoding="utf-8"
    )


def test_prototype_data_tier_is_disclosed_in_terminal_and_markdown() -> None:
    terminal = render_terminal(_brief(), RunStatus.SUCCESS, width=200)
    markdown = render_markdown(_brief(), RunStatus.SUCCESS)

    assert "Data tier: prototype（非公式データに基づく試作結果）" in terminal
    assert "Data provider: `yfinance`" in markdown
    assert "Data tier: `prototype`（非公式データに基づく試作結果）" in markdown


def test_run_summary_contains_every_operational_handoff_field() -> None:
    summary = TerminalRunSummary(
        run_id=RUN_ID,
        status=RunStatus.DEGRADED,
        exit_code=0,
        provider_name="yfinance",
        data_tier=DataTier.PROTOTYPE,
        missing_sources=("text",),
        paths=TerminalPaths(
            report=Path("reports/2026-07-22/run.md"),
            analysis_input=Path("reports/2026-07-22/analysis_input.json"),
        ),
    )

    output = render_run_summary(summary, width=200)

    assert "Run ID: 11111111-2222-3333-4444-555555555555" in output
    assert "Status: DEGRADED" in output
    assert "Exit code: 0" in output
    assert "Data: yfinance / prototype" in output
    assert "Missing sources: text" in output
    assert "詳細レポート: reports/2026-07-22/run.md" in output
    assert "analysis_input.json" in output
    assert "uv run copilot-history run --run-id" in output


def test_latest_replace_failure_preserves_previous_latest_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = tmp_path / "latest.md"
    latest.write_text("previous", encoding="utf-8")
    original_replace = Path.replace

    def fail_latest_replace(self: Path, target: Path) -> Path:
        if self.name == ".latest.md.tmp":
            message = "simulated latest replacement failure"
            raise OSError(message)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_latest_replace)

    with pytest.raises(OSError, match="simulated latest replacement failure"):
        write_markdown_report(_brief(), RunStatus.SUCCESS, tmp_path)

    assert latest.read_text(encoding="utf-8") == "previous"
    assert not (tmp_path / ".latest.md.tmp").exists()
