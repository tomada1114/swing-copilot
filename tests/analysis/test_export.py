"""Analysis-input assembly and atomic export (`analysis/export.py`)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ExportCandidate,
    ExportRequest,
    TextExportLimits,
    build_analysis_input,
    write_analysis_input,
    write_json_atomically,
)
from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    DistributionResult,
)
from swing_copilot.regime.exposure import ExposureDecision, ExposureVerdict
from swing_copilot.regime.gate import GateVerdict, MarketGate, RegimeSnapshot
from swing_copilot.risk.checks import RiskAssessment
from swing_copilot.screening.base import Candidate
from swing_copilot.text.base import TextItem

AS_OF = date(2027, 3, 1)
GENERATED_AT = datetime(2027, 3, 1, 12, tzinfo=UTC)
LIMITS = TextExportLimits(max_news_items=2, max_news_chars=20, max_filing_chars=15)


def _snapshot() -> RegimeSnapshot:
    distribution = DistributionResult(
        d25=1.0,
        d15=0.0,
        d5=0.0,
        level=DistributionLevel.NORMAL,
        data_quality=DataQuality.OK,
    )
    return RegimeSnapshot(
        as_of=AS_OF,
        gate=MarketGate(GateVerdict.BULL, 100.0, 95.0, 15.0),
        spy_distribution=distribution,
        qqq_distribution=distribution,
        dd_level=DistributionLevel.NORMAL,
        data_quality=DataQuality.OK,
    )


def _exposure() -> ExposureDecision:
    return ExposureDecision(
        verdict=ExposureVerdict.NEW_ENTRY_ALLOWED,
        gate=GateVerdict.BULL,
        dd_level=DistributionLevel.NORMAL,
        data_quality=DataQuality.OK,
        is_conservatively_downgraded=False,
    )


def _news(source_id: str, day: int, body: str = "A" * 100) -> TextItem:
    stamp = datetime(2027, 2, day, tzinfo=UTC)
    return TextItem(
        source_id=source_id,
        symbol="AAPL",
        source_type="news",
        published_at=stamp,
        title="headline",
        source_url=f"https://example.com/{source_id}",
        content_text=body,
        fetched_at=stamp,
    )


def _filing(source_id: str = "edgar:1", title: str | None = "10-Q - Apple") -> TextItem:
    stamp = datetime(2027, 2, 20, tzinfo=UTC)
    return TextItem(
        source_id=source_id,
        symbol="AAPL",
        source_type="filing",
        published_at=stamp,
        title=title,
        source_url="https://example.com/filing",
        content_text="B" * 100,
        fetched_at=stamp,
    )


def _request(*text_items: TextItem) -> ExportRequest:
    candidate = ExportCandidate(
        candidate=Candidate(
            symbol="AAPL",
            as_of=AS_OF,
            signal_names=("trend_sma",),
            metrics={
                "score": 0.5,
                "score_rsi_pullback": 0.2,
                "score_trend_quality": 0.2,
                "score_liquidity": 0.1,
            },
            rank=1,
        ),
        risk_assessment=RiskAssessment(
            symbol="AAPL",
            status="approved",
            max_shares=10,
            entry_price=100.0,
            stop_price=95.0,
            reasons=(),
            binding_constraint="trade_risk",
        ),
        text_items=text_items,
    )
    return ExportRequest(
        as_of=AS_OF,
        generated_at=GENERATED_AT,
        regime_snapshot=_snapshot(),
        exposure_decision=_exposure(),
        performance_summary=None,
        candidates=(candidate,),
        limits=LIMITS,
    )


class TestBuildAnalysisInput:
    def test_it_stamps_the_agreed_schema_version_and_as_of(self):
        payload = build_analysis_input(_request())

        assert payload.schema_version == "analysis-input-v1"
        assert payload.as_of == AS_OF
        assert payload.generated_at == GENERATED_AT

    def test_a_candidate_without_any_text_is_still_exported(self):
        payload = build_analysis_input(_request())

        candidate = payload.candidates[0]
        assert candidate.symbol == "AAPL"
        assert candidate.news == []
        assert candidate.filings == []
        assert "<score_breakdown>" in candidate.score_breakdown
        assert "<risk_constraints>" in candidate.risk_constraints

    def test_news_is_newest_first_and_capped_by_count(self):
        payload = build_analysis_input(
            _request(
                _news("finnhub:1", 20),
                _news("finnhub:2", 25),
                _news("finnhub:3", 28),
            )
        )

        assert [item.source_id for item in payload.candidates[0].news] == [
            "finnhub:3",
            "finnhub:2",
        ]

    def test_news_bodies_are_truncated_to_the_export_budget(self):
        payload = build_analysis_input(_request(_news("finnhub:1", 20)))

        assert len(payload.candidates[0].news[0].summary) == LIMITS.max_news_chars

    def test_the_provider_is_derived_from_the_source_id_prefix(self):
        payload = build_analysis_input(_request(_news("finnhub:1", 20)))

        assert payload.candidates[0].news[0].provider == "finnhub"

    def test_filing_text_is_truncated_and_its_form_type_extracted(self):
        payload = build_analysis_input(_request(_filing()))

        filing = payload.candidates[0].filings[0]
        assert filing.form_type == "10-Q"
        assert len(filing.text) == LIMITS.max_filing_chars
        assert filing.filed_at == datetime(2027, 2, 20, tzinfo=UTC)

    def test_a_filing_without_a_title_falls_back_to_unknown(self):
        payload = build_analysis_input(_request(_filing(title=None)))

        assert payload.candidates[0].filings[0].form_type == "unknown"

    def test_an_empty_decision_history_exports_null_not_an_empty_string(self):
        payload = build_analysis_input(_request())

        assert payload.candidates[0].decision_history is None

    def test_an_absent_performance_summary_exports_null(self):
        payload = build_analysis_input(_request())

        assert payload.context.performance_summary is None
        assert payload.context.market_regime is not None


class TestAtomicWrite:
    def test_it_writes_into_the_given_directory_and_returns_an_absolute_path(
        self,
        tmp_path,
    ):
        path = write_analysis_input(build_analysis_input(_request()), tmp_path / "out")

        assert path == (tmp_path / "out" / ANALYSIS_INPUT_FILENAME).resolve()
        assert path.is_absolute()
        assert (
            json.loads(path.read_text(encoding="utf-8"))["as_of"] == AS_OF.isoformat()
        )

    def test_a_failed_write_preserves_the_previous_file_and_leaves_no_temp(
        self,
        tmp_path,
        monkeypatch,
    ):
        destination = tmp_path / "analysis_input.json"
        destination.write_text('{"previous": true}', encoding="utf-8")

        def _explode(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("swing_copilot.analysis.export.os.replace", _explode)

        with pytest.raises(OSError, match="disk full"):
            write_json_atomically(destination, {"new": True})

        assert json.loads(destination.read_text(encoding="utf-8")) == {"previous": True}
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_a_rerun_replaces_the_previous_content(self, tmp_path):
        destination = tmp_path / "analysis_input.json"
        write_json_atomically(destination, {"generation": 1})

        write_json_atomically(destination, {"generation": 2})

        assert json.loads(destination.read_text(encoding="utf-8")) == {"generation": 2}
