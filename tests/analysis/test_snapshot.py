"""Report-context archival round-trip (`analysis/snapshot.py`)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

from swing_copilot.analysis.schemas import canonical_json_digest
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
    BriefExposure,
    BriefFilingAnalysis,
    BriefFundamentals,
    BriefMarketItem,
    BriefRegime,
    BriefRejectionCount,
    BriefRisk,
    BriefSource,
    DailyBrief,
    SignalPerformanceRow,
)

RUN_ID = UUID("11111111-2222-3333-4444-555555555555")
RUN_DATE = date(2027, 3, 1)
INPUT_DIGEST = "a" * 64


def _context(status: RunStatus, output_dir: Path) -> ReportContext:
    """Build one valid v2 context with its immutable input binding."""
    return ReportContext(
        _populated_brief(), status, output_dir, "default", INPUT_DIGEST
    )


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
                score_atr_pct=0.0,
                score_pivot_proximity=0.0,
                score_rs_percentile=0.0,
                score_criteria_met=0.0,
                signals=("SMA200上抜け",),
                fundamentals=BriefFundamentals("21.0x", "$1,000", "60%", "$9.00"),
                risk=BriefRisk(
                    status="approved",
                    entry_price=190.0,
                    limit_price=191.0,
                    stop_price=180.0,
                    atr14=3.1,
                    stop_distance_pct=(191.0 - 180.0) / 191.0,
                    reasons=("ok",),
                    warnings=("WIDE_STOP",),
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
        rejection_counts=(BriefRejectionCount("FILTER_LOW_LIQUIDITY", 12),),
        notices=("text: partial failure",),
        signal_performance=(SignalPerformanceRow("trend_sma", 3, 1, 2, 0.75, 6, True),),
        no_trade=True,
        no_trade_reason="地合いが不安定。",
    )


class TestRoundTrip:
    def test_every_populated_section_survives_a_write_and_read(self, tmp_path):
        context = _context(RunStatus.DEGRADED, tmp_path / "reports")

        path = write_report_context(context, tmp_path / "reports" / "2027-03-01")
        reloaded = read_report_context(path)

        assert reloaded.brief == context.brief
        assert reloaded.status is RunStatus.DEGRADED
        assert reloaded.output_dir == tmp_path / "reports"

    def test_it_is_written_under_the_agreed_filename_and_version(self, tmp_path):
        destination_dir = tmp_path / "reports" / "2027-03-01"

        path = write_report_context(
            _context(RunStatus.SUCCESS, Path("reports")),
            destination_dir,
        )

        assert path == destination_dir / REPORT_CONTEXT_FILENAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == CONTEXT_SCHEMA_VERSION
        assert payload["run_id"] == str(RUN_ID)
        assert payload["as_of"] == RUN_DATE.isoformat()
        assert payload["input_digest"] == INPUT_DIGEST
        assert len(payload["context_digest"]) == 64
        assert payload["status"] == "success"

    def test_a_rerun_replaces_the_previous_archive(self, tmp_path):
        destination_dir = tmp_path / "reports" / "2027-03-01"
        write_report_context(
            _context(RunStatus.SUCCESS, Path("reports")),
            destination_dir,
        )

        path = write_report_context(
            _context(RunStatus.DEGRADED, Path("reports")),
            destination_dir,
        )

        assert read_report_context(path).status is RunStatus.DEGRADED


class TestReadFailures:
    def test_a_missing_file_is_a_hard_failure(self, tmp_path):
        with pytest.raises(AnalysisIngestError, match="could not be read"):
            read_report_context(tmp_path / "missing.json")

    def test_a_wrongly_encoded_context_is_a_hard_failure(self, tmp_path):
        """A non-UTF-8 context must arrive as `AnalysisIngestError` (Issue #164).

        `UnicodeDecodeError` is a `ValueError`, so the old `except OSError`
        never caught it and the unattended run that produced the mojibake
        surfaced as an unexpected fault instead of a broken artifact.
        """
        path = tmp_path / REPORT_CONTEXT_FILENAME
        path.write_bytes(b'{"schema_version": "\xff\xfe"}')

        with pytest.raises(AnalysisIngestError, match="could not be read"):
            read_report_context(path)

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

        with pytest.raises(AnalysisIngestError, match="but this build expects"):
            read_report_context(path)

    def test_an_unknown_run_status_is_a_hard_failure(self, tmp_path):
        path = write_report_context(_context(RunStatus.SUCCESS, tmp_path), tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "exploded"
        payload["context_digest"] = canonical_json_digest(
            payload, excluded_field="context_digest"
        )
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(AnalysisIngestError, match="failed validation"):
            read_report_context(path)

    def test_a_missing_output_dir_is_a_hard_failure(self, tmp_path):
        # Defaulting to the CWD would silently rewrite the report outside the
        # run's own archive directory.
        path = write_report_context(
            _context(RunStatus.SUCCESS, Path("reports")),
            tmp_path,
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["output_dir"]
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(AnalysisIngestError, match="failed validation"):
            read_report_context(path)

    def test_a_corrupt_brief_is_a_hard_failure(self, tmp_path):
        path = write_report_context(_context(RunStatus.SUCCESS, tmp_path), tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["brief"] = {"run_id": "not-a-uuid"}
        payload["context_digest"] = canonical_json_digest(
            payload, excluded_field="context_digest"
        )
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(AnalysisIngestError, match="failed validation"):
            read_report_context(path)


class TestSchemaVersionMismatch:
    """Issue #296: a schema generation bump must fail loudly and explicitly.

    `BriefCandidate` gained three required fields (`score_pivot_proximity`,
    `score_rs_percentile`, `score_criteria_met`) in Issue #251 without a
    matching `CONTEXT_SCHEMA_VERSION` bump. A `report_context.json` written by
    an older build would then fail deep inside `_BRIEF_ADAPTER.validate_python`
    with a raw pydantic "Field required" error instead of a message that names
    the actual cause (schema generation mismatch) and its recovery (re-run
    `copilot-daily`).
    """

    def test_a_prior_generation_context_fails_with_an_explicit_generation_mismatch(
        self, tmp_path
    ):
        """A v2 file with the three new fields missing must not reach pydantic.

        This fixture would ALSO fail `_BRIEF_ADAPTER.validate_python` on its
        own merits (the three fields are missing and undefaulted), so this
        pins that the generation check runs first and wins -- not that it
        merely happens to produce *some* `AnalysisIngestError`.
        """
        path = write_report_context(_context(RunStatus.SUCCESS, tmp_path), tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = "report-context-v2"
        candidate = payload["brief"]["candidates"][0]
        for field in (
            "score_pivot_proximity",
            "score_rs_percentile",
            "score_criteria_met",
        ):
            del candidate[field]
        payload["context_digest"] = canonical_json_digest(
            payload, excluded_field="context_digest"
        )
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(AnalysisIngestError) as exc_info:
            read_report_context(path)

        message = str(exc_info.value)
        assert "report-context-v2" in message
        assert "report-context-v4" in message
        assert "copilot-daily" in message
        assert "Field required" not in message
        assert "score_pivot_proximity" not in message

    def test_the_current_generation_still_round_trips(self, tmp_path):
        context = _context(RunStatus.SUCCESS, tmp_path / "reports")

        path = write_report_context(context, tmp_path / "reports" / "2027-03-01")
        reloaded = read_report_context(path)

        assert reloaded.brief == context.brief
        assert CONTEXT_SCHEMA_VERSION == "report-context-v4"
