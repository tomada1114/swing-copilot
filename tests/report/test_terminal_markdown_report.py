"""CLI-first daily brief rendering and Markdown archival contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

from swing_copilot.models import RunStatus
from swing_copilot.pipeline.postmortem import SignalPerformanceRow
from swing_copilot.report.daily_brief import (
    BriefCandidate,
    BriefCircuitBreaker,
    BriefExposure,
    BriefFilingAnalysis,
    BriefFundamentals,
    BriefLlm,
    BriefMarketItem,
    BriefPastDecision,
    BriefPortfolioHeat,
    BriefRegime,
    BriefRejectionCount,
    BriefRisk,
    BriefSource,
    DailyBrief,
)
from swing_copilot.report.markdown_report import render_markdown, write_markdown_report
from swing_copilot.report.terminal_report import render_terminal

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
                signals=("RSI押し目",),
                fundamentals=BriefFundamentals(
                    per="41.2x", fcf="$12,000", equity_ratio="52%", eps="$4.16"
                ),
                risk=BriefRisk(
                    status="approved",
                    max_shares=12,
                    stop_price=164.80,
                    reasons=(),
                    warnings=("AMDとの相関 0.82",),
                ),
                llm=BriefLlm(
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


def _brief_with_llm(llm: BriefLlm) -> DailyBrief:
    base = _brief()
    candidate = replace(base.candidates[0], llm=llm)
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

    assert terminal.index("Market regime") < terminal.index("Symbol")
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
    assert "FAIR (d=1.00)" in terminal
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
        "Signals | Risk | Shares | Stop |"
    )
    separator = "|---:|---|---:|---:|---:|---:|---|---|---|---:|---:|"
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

    assert terminal.index("Exposure Ceiling") < terminal.index("Symbol")
    assert "CASH_PRIORITY" in terminal
    assert markdown.index("## Exposure Ceiling") < markdown.index("## Candidates")
    assert "Verdict: `CASH_PRIORITY`" in markdown


def test_circuit_breaker_banner_is_alongside_exposure_before_candidates() -> None:
    brief = replace(
        _brief(),
        exposure=BriefExposure(
            verdict="NEW_ENTRY_ALLOWED",
            gate="BULL",
            dd_level="NORMAL",
            data_quality="OK",
            is_conservatively_downgraded=False,
        ),
        circuit_breaker=BriefCircuitBreaker(
            state="HALTED",
            data_quality="OK",
            triggered_rules=("DAILY_LOSS",),
        ),
    )

    terminal = render_terminal(brief, RunStatus.SUCCESS, width=200)
    markdown = render_markdown(brief, RunStatus.SUCCESS)

    assert terminal.index("Exposure Ceiling") < terminal.index("Circuit Breaker")
    assert terminal.index("Circuit Breaker") < terminal.index("Symbol")
    assert markdown.index("## Exposure Ceiling") < markdown.index("## Circuit Breaker")
    assert markdown.index("## Circuit Breaker") < markdown.index("## Candidates")
    assert "HALTED" in terminal
    assert "Triggered rules: `DAILY_LOSS`" in markdown


def test_terminal_and_markdown_always_show_portfolio_heat_before_candidates() -> None:
    brief = replace(
        _brief(),
        portfolio_heat=BriefPortfolioHeat(
            status="calculated",
            heat_pct=4.4,
            max_heat_pct=6.0,
        ),
    )

    terminal = render_terminal(brief, RunStatus.SUCCESS, width=200)
    markdown = render_markdown(brief, RunStatus.SUCCESS)

    assert terminal.index("Portfolio heat: 4.40% / 6.00%") < terminal.index("Symbol")
    assert markdown.index("## Portfolio risk") < markdown.index("## Candidates")
    assert "Portfolio heat: `4.40% / 6.00%`" in markdown


def test_terminal_and_markdown_explain_missing_stop_heat_failure() -> None:
    brief = replace(
        _brief(),
        portfolio_heat=BriefPortfolioHeat(
            status="not_calculable",
            heat_pct=None,
            max_heat_pct=6.0,
            missing_stop_symbols=("ABC",),
        ),
    )

    terminal = render_terminal(brief, RunStatus.SUCCESS, width=200)
    markdown = render_markdown(brief, RunStatus.SUCCESS)

    assert "Portfolio heat: not_calculable (missing stop: ABC)" in terminal
    assert "Portfolio heat: `not_calculable` (missing stop: ABC)" in markdown


def test_terminal_and_markdown_show_earnings_warning_for_candidate() -> None:
    risk = BriefRisk(
        status="approved",
        max_shares=10,
        stop_price=95.0,
        reasons=(),
        warnings=(),
        sizing_warnings=("EARNINGS_PROXIMITY_WARN: 5 business days until 2026-07-28",),
    )

    terminal = render_terminal(_brief_with_sizing(risk), RunStatus.SUCCESS, width=200)
    markdown = render_markdown(_brief_with_sizing(risk), RunStatus.SUCCESS)

    assert "EARNINGS_PROXIMITY_WARN" in terminal
    assert "EARNINGS_PROXIMITY_WARN" in markdown


def test_terminal_shows_the_binding_constraint_sizing_string() -> None:
    # REQ-006 worked example: trade_risk binds.
    risk = BriefRisk(
        status="approved",
        max_shares=200,
        stop_price=45.0,
        reasons=(),
        warnings=(),
        shares_by_risk=200,
        shares_by_position_cap=500,
        binding_constraint="trade_risk",
        max_trade_risk_pct=0.01,
        max_position_pct=0.25,
    )
    # A wide console avoids Rich wrapping the cell across two lines, which
    # would otherwise split this literal substring apart.
    output = render_terminal(_brief_with_sizing(risk), RunStatus.SUCCESS, width=200)
    assert "200株（制約: リスク1.0%）" in output


def test_markdown_shows_the_binding_constraint_sizing_string() -> None:
    # REQ-006 worked example: position_cap binds.
    risk = BriefRisk(
        status="approved",
        max_shares=40,
        stop_price=45.0,
        reasons=(),
        warnings=(),
        shares_by_risk=200,
        shares_by_position_cap=40,
        binding_constraint="position_cap",
        max_trade_risk_pct=0.01,
        max_position_pct=0.02,
    )
    output = render_markdown(_brief_with_sizing(risk), RunStatus.SUCCESS)
    assert "40株（制約: ポジション上限2.0%）" in output


def test_terminal_shows_zero_shares_without_exception() -> None:
    # REQ-020 boundary: a floored-to-zero trade renders Example 4's wording,
    # not an exception or a bare "0".
    risk = BriefRisk(
        status="approved",
        max_shares=0,
        stop_price=45.0,
        reasons=(),
        warnings=(),
        shares_by_risk=1,
        shares_by_position_cap=0,
        binding_constraint="position_cap",
        sizing_warnings=("SMALL_ACCOUNT_FRICTION",),
        max_trade_risk_pct=0.01,
        max_position_pct=0.001,
    )
    output = render_terminal(_brief_with_sizing(risk), RunStatus.SUCCESS, width=200)
    assert "0株（摩擦: 資金規模過小）" in output


def test_markdown_shows_regime_wording_not_friction_for_regime_zero_shares() -> None:
    # P6-28: a regime-driven zero-share candidate (e.g. Exposure Ceiling
    # CASH_PRIORITY) must not be shown with the small-account-friction
    # wording, since the account was never the binding constraint.
    risk = BriefRisk(
        status="rejected",
        max_shares=0,
        stop_price=None,
        reasons=("REGIME_CASH_PRIORITY",),
        warnings=(),
        binding_constraint="regime",
        max_trade_risk_pct=0.0,
    )
    output = render_markdown(_brief_with_sizing(risk), RunStatus.SUCCESS)
    assert "0株（レジーム: 新規建て停止）" in output
    assert "資金規模過小" not in output


def test_markdown_still_shows_dash_for_not_calculable_max_shares() -> None:
    risk = BriefRisk(
        status="not_calculable",
        max_shares=None,
        stop_price=None,
        reasons=("missing candidate price/ATR data",),
        warnings=(),
    )
    output = render_markdown(_brief_with_sizing(risk), RunStatus.SUCCESS)
    assert "| not_calculable | - |" in output


def test_terminal_output_is_a_compact_decision_brief() -> None:
    output = render_terminal(_brief(), RunStatus.DEGRADED, width=120, color=False)

    assert "Swing Copilot" in output
    assert "2026-07-22" in output
    assert "DEGRADED" in output
    assert "NVDA" in output
    assert "171.20" in output
    assert "approved" in output
    assert "業績見通しは維持されているが規制リスクが残る" in output
    assert "FREDカレンダーを取得できませんでした" in output
    assert "<html" not in output


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


def _llm_with_filing(**overrides: object) -> BriefLlm:
    filing = BriefFilingAnalysis(
        filing_type="10-Q",
        filed_at=date(2026, 7, 21),
        facts=("Revenue up 10%",),
        interpretation=("Growth appears steady",),
        red_flags=(),
        yoy_changes=(),
        guidance_direction="positive",
        sources=(BriefSource("filing:1", "https://example.com/filing/1"),),
    )
    base = BriefLlm(
        degraded=False,
        conclusion="conclusion",
        filings=(filing,),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_terminal_shows_each_filing_analysis_with_type_and_filed_date() -> None:
    # P6-27: previously only the first filing analysis per symbol ever
    # reached the report; now each is individually identified.
    output = render_terminal(
        _brief_with_llm(_llm_with_filing()), RunStatus.SUCCESS, width=200
    )

    assert "Filing [10-Q 2026-07-21]: Growth appears steady" in output


def test_markdown_shows_each_filing_analysis_in_its_own_labeled_section() -> None:
    output = render_markdown(_brief_with_llm(_llm_with_filing()), RunStatus.SUCCESS)

    assert "### 開示分析: 10-Q (2026-07-21)" in output
    assert "Revenue up 10%" in output
    assert "Guidance direction: positive" in output
    assert "[filing:1](https://example.com/filing/1)" in output


def test_terminal_and_markdown_show_catalyst_quality_display_only() -> None:
    llm = replace(_llm_with_filing(), catalyst_quality="high")

    terminal = render_terminal(_brief_with_llm(llm), RunStatus.SUCCESS, width=200)
    markdown = render_markdown(_brief_with_llm(llm), RunStatus.SUCCESS)

    assert "Catalyst quality: high" in terminal
    assert "Catalyst quality: high" in markdown


def test_terminal_and_markdown_omit_catalyst_quality_when_absent() -> None:
    output_terminal = render_terminal(_brief(), RunStatus.SUCCESS, width=200)
    output_markdown = render_markdown(_brief(), RunStatus.SUCCESS)

    assert "Catalyst quality" not in output_terminal
    assert "Catalyst quality" not in output_markdown


def test_terminal_and_markdown_show_a_near_stale_warning_for_news() -> None:
    llm = replace(_llm_with_filing(), is_news_near_stale=True)

    terminal = render_terminal(_brief_with_llm(llm), RunStatus.SUCCESS, width=200)
    markdown = render_markdown(_brief_with_llm(llm), RunStatus.SUCCESS)

    assert "TTL" in terminal
    assert "ニュース分析のキャッシュがTTL間近です" in markdown


def test_terminal_and_markdown_show_a_near_stale_warning_for_a_filing() -> None:
    stale_filing = replace(_llm_with_filing().filings[0], is_near_stale=True)
    llm = replace(_llm_with_filing(), filings=(stale_filing,))

    terminal = render_terminal(_brief_with_llm(llm), RunStatus.SUCCESS, width=200)
    markdown = render_markdown(_brief_with_llm(llm), RunStatus.SUCCESS)

    assert "TTL" in terminal
    assert "このキャッシュ済み分析はTTL間近です" in markdown


def test_terminal_and_markdown_omit_near_stale_warning_when_fresh() -> None:
    output_terminal = render_terminal(
        _brief_with_llm(_llm_with_filing()), RunStatus.SUCCESS, width=200
    )
    output_markdown = render_markdown(
        _brief_with_llm(_llm_with_filing()), RunStatus.SUCCESS
    )

    assert "TTL" not in output_terminal
    assert "TTL" not in output_markdown


def test_terminal_shows_score_column_and_breakdown() -> None:
    # REQ-007
    output = render_terminal(_brief(), RunStatus.SUCCESS, width=200, color=False)

    assert "0.627" in output
    assert "rsi 0.17 / trend 0.30 / liq 0.16" in output


def test_markdown_shows_score_column_and_breakdown_table() -> None:
    # REQ-008
    output = render_markdown(_brief(), RunStatus.SUCCESS)

    assert "| 1 | NVDA |" in output
    assert "0.627" in output
    assert "### Score breakdown" in output
    assert "| rsi_pullback | 0.167 |" in output
    assert "| trend_quality | 0.300 |" in output
    assert "| liquidity | 0.160 |" in output


def _brief_with_past_decisions() -> DailyBrief:
    base = _brief()
    candidate = replace(
        base.candidates[0],
        past_decisions=(
            BriefPastDecision(date(2026, 7, 19), "followed", "出来高増加", 0.05),
            BriefPastDecision(date(2026, 7, 12), "ignored", None, None),
        ),
    )
    return replace(base, candidates=(candidate,))


def test_markdown_shows_past_decisions_section_newest_first() -> None:
    # P1-05 REQ-008: 過去判断 subsection, rendered in the given order (the
    # caller -- `get_decision_history` -- is responsible for newest-first).
    output = render_markdown(_brief_with_past_decisions(), RunStatus.SUCCESS)

    assert "### 過去判断" in output
    assert output.index("2026-07-19") < output.index("2026-07-12")
    assert "followed" in output
    assert "出来高増加" in output
    assert "+5.00%" in output
    assert "ignored" in output


def test_markdown_omits_past_decisions_section_when_empty() -> None:
    # P1-05 boundary: zero past decisions omits the whole subsection, no
    # stray heading -- matching Facts/LLM risk flags/Sources' own style.
    output = render_markdown(_brief(), RunStatus.SUCCESS)

    assert "### 過去判断" not in output


def _brief_without_candidates() -> DailyBrief:
    return replace(_brief(), candidates=())


def test_terminal_renders_empty_candidate_set_without_error() -> None:
    output = render_terminal(_brief_without_candidates(), RunStatus.SUCCESS, width=120)

    assert "Candidates: 0" in output
    assert "Score" in output  # header still renders


def _brief_with_rejections() -> DailyBrief:
    return replace(
        _brief(),
        rejection_counts=(
            BriefRejectionCount("FILTER_LOW_LIQUIDITY", 3),
            BriefRejectionCount("SIGNAL_RSI_NOT_MET", 1),
        ),
    )


def test_terminal_shows_rejection_summary_counts() -> None:
    # REQ-005: 落選サマリ, reason_code別件数.
    output = render_terminal(_brief_with_rejections(), RunStatus.SUCCESS, width=120)

    assert "落選サマリ" in output
    assert "FILTER_LOW_LIQUIDITY" in output
    assert "3" in output
    assert "SIGNAL_RSI_NOT_MET" in output


def test_markdown_shows_rejection_summary_table() -> None:
    output = render_markdown(_brief_with_rejections(), RunStatus.SUCCESS)

    assert "## 落選サマリ" in output
    assert "FILTER_LOW_LIQUIDITY" in output
    assert "| 3 |" in output
    assert "SIGNAL_RSI_NOT_MET" in output


def test_terminal_renders_empty_rejection_summary_without_error() -> None:
    # REQ-010 boundary: zero rejections renders a "0件" style message, no
    # exception.
    output = render_terminal(_brief(), RunStatus.SUCCESS, width=120)

    assert "落選サマリ" in output
    assert "0件" in output


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
