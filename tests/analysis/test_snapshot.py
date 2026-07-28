"""Report-context archival round-trip (`analysis/snapshot.py`)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

from swing_copilot.analysis.snapshot import (
    CONTEXT_SCHEMA_VERSION,
    REPORT_CONTEXT_FILENAME,
    ReportContext,
    read_report_context,
    write_report_context,
)
from swing_copilot.analysis.validate import AnalysisIngestError
from swing_copilot.models import RunStatus
from swing_copilot.report.daily_brief import (
    BriefAnalysis,
    BriefCandidate,
    BriefCircuitBreaker,
    BriefExposure,
    BriefFilingAnalysis,
    BriefFundamentals,
    BriefMarketItem,
    BriefPastDecision,
    BriefPortfolioHeat,
    BriefRegime,
    BriefRejectionCount,
    BriefRisk,
    BriefSource,
    DailyBrief,
    SignalPerformanceRow,
)

RUN_ID = UUID("11111111-2222-3333-4444-555555555555")
RUN_DATE = date(2027, 3, 1)


def _populated_brief() -> DailyBrief:
    """A brief with every optional section filled, to catch dropped fields."""
    return DailyBrief(
        run_id=RUN_ID,
        run_date=RUN_DATE,
        generated_at=datetime(2027, 3, 1, 12, tzinfo=UTC),
        market=(
            BriefMarketItem("SPY", 500.0, 0.01),
            BriefMarketItem("VIX", None, None),
        ),
        candidates=(
            BriefCandidate(
                rank=1,
                symbol="AAPL",
                company_name="Apple Inc.",
                close=190.0,
                pct_change=0.012,
                rsi14=44.2,
                atr14=3.1,
                score=0.812,
                score_rsi_pullback=0.4,
                score_trend_quality=0.25,
                score_liquidity=0.16,
                signals=("SMA200上抜け",),
                fundamentals=BriefFundamentals("21.0x", "$1,000", "60%", "$9.00"),
                risk=BriefRisk(
                    status="approved",
                    max_shares=128,
                    stop_price=180.0,
                    reasons=("ok",),
                    warnings=("MSFTとの相関 0.80",),
                    shares_by_risk=128,
                    shares_by_position_cap=200,
                    binding_constraint="trade_risk",
                    sizing_warnings=("WIDE_STOP",),
                    max_trade_risk_pct=0.01,
                    max_position_pct=0.10,
                ),
                analysis=BriefAnalysis(
                    degraded=False,
                    conclusion="Survived on trend quality.",
                    facts=("Revenue rose.",),
                    risk_flags=("Execution risk.",),
                    sources=(BriefSource("finnhub:1", "https://example.com/news"),),
                    filings=(
                        BriefFilingAnalysis(
                            filing_type="10-Q",
                            filed_at=date(2027, 2, 20),
                            facts=("Revenue rose.",),
                            interpretation=("May indicate demand.",),
                            red_flags=("Margin pressure.",),
                            yoy_changes=("Revenue +8%",),
                            sources=(
                                BriefSource("edgar:1", "https://example.com/filing"),
                            ),
                        ),
                    ),
                    verdict="skip",
                    verdict_summary="Guidance was withdrawn.",
                    strengths=("Trend intact",),
                    concerns=("Extended",),
                ),
                past_decisions=(
                    BriefPastDecision(date(2027, 2, 1), "buy", "breakout", 0.05),
                ),
                execution_state="FAIR",
                execution_distance=1.2,
            ),
        ),
        regime=BriefRegime(
            gate="BULL",
            dd_level="NORMAL",
            spy_d25=1.0,
            qqq_d25=2.0,
            data_quality="OK",
            spy_ftd_state="CONFIRMED",
            spy_ftd_day_number=4,
            spy_ftd_quality_score=3,
            qqq_ftd_state="NONE",
            qqq_ftd_day_number=None,
            qqq_ftd_quality_score=None,
        ),
        exposure=BriefExposure("NEW_ENTRY_ALLOWED", "BULL", "NORMAL", "OK", False),
        circuit_breaker=BriefCircuitBreaker(
            "NORMAL", "OK", ("daily",), -1.0, None, None
        ),
        portfolio_heat=BriefPortfolioHeat("calculated", 2.5, 6.0, ("MSFT",), None),
        rejection_counts=(BriefRejectionCount("FILTER_LOW_LIQUIDITY", 12),),
        notices=("text: partial failure",),
        signal_performance=(SignalPerformanceRow("trend_sma", 3, 1, 2, 0.75, 6, True),),
        no_trade=True,
        no_trade_reason="地合いが不安定。",
    )


class TestRoundTrip:
    def test_every_populated_section_survives_a_write_and_read(self, tmp_path):
        context = ReportContext(
            _populated_brief(), RunStatus.DEGRADED, tmp_path / "reports"
        )

        path = write_report_context(context, tmp_path / "reports" / "2027-03-01")
        reloaded = read_report_context(path)

        assert reloaded.brief == context.brief
        assert reloaded.status is RunStatus.DEGRADED
        assert reloaded.output_dir == tmp_path / "reports"

    def test_it_is_written_under_the_agreed_filename_and_version(self, tmp_path):
        destination_dir = tmp_path / "reports" / "2027-03-01"

        path = write_report_context(
            ReportContext(_populated_brief(), RunStatus.SUCCESS, Path("reports")),
            destination_dir,
        )

        assert path == destination_dir / REPORT_CONTEXT_FILENAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == CONTEXT_SCHEMA_VERSION
        assert payload["status"] == "success"

    def test_a_rerun_replaces_the_previous_archive(self, tmp_path):
        destination_dir = tmp_path / "reports" / "2027-03-01"
        write_report_context(
            ReportContext(_populated_brief(), RunStatus.SUCCESS, Path("reports")),
            destination_dir,
        )

        path = write_report_context(
            ReportContext(_populated_brief(), RunStatus.DEGRADED, Path("reports")),
            destination_dir,
        )

        assert read_report_context(path).status is RunStatus.DEGRADED


class TestReadFailures:
    def test_a_missing_file_is_a_hard_failure(self, tmp_path):
        with pytest.raises(AnalysisIngestError, match="could not be read"):
            read_report_context(tmp_path / "missing.json")

    def test_malformed_json_is_a_hard_failure(self, tmp_path):
        path = tmp_path / REPORT_CONTEXT_FILENAME
        path.write_text("{oops", encoding="utf-8")

        with pytest.raises(AnalysisIngestError, match="not valid JSON"):
            read_report_context(path)

    def test_a_non_object_document_is_a_hard_failure(self, tmp_path):
        path = tmp_path / REPORT_CONTEXT_FILENAME
        path.write_text("[]", encoding="utf-8")

        with pytest.raises(AnalysisIngestError, match="must be a JSON object"):
            read_report_context(path)

    def test_an_unsupported_schema_version_is_a_hard_failure(self, tmp_path):
        path = tmp_path / REPORT_CONTEXT_FILENAME
        path.write_text(json.dumps({"schema_version": "v0"}), encoding="utf-8")

        with pytest.raises(AnalysisIngestError, match="Unsupported report context"):
            read_report_context(path)

    def test_an_unknown_run_status_is_a_hard_failure(self, tmp_path):
        path = tmp_path / REPORT_CONTEXT_FILENAME
        path.write_text(
            json.dumps(
                {
                    "schema_version": CONTEXT_SCHEMA_VERSION,
                    "status": "exploded",
                    "brief": {},
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(AnalysisIngestError, match="failed validation"):
            read_report_context(path)

    def test_a_corrupt_brief_is_a_hard_failure(self, tmp_path):
        path = tmp_path / REPORT_CONTEXT_FILENAME
        path.write_text(
            json.dumps(
                {
                    "schema_version": CONTEXT_SCHEMA_VERSION,
                    "status": "success",
                    "brief": {"run_id": "not-a-uuid"},
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(AnalysisIngestError, match="failed validation"):
            read_report_context(path)
