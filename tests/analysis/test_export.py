"""Analysis-input assembly and atomic export (`analysis/export.py`)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

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
from swing_copilot.text.base import FilingSection, TextItem
from swing_copilot.text.calendar_fred import (
    FRED_RELEASE_DATES_URL,
    FRED_RELEASE_SERIES_URL,
    FRED_SERIES_OBSERVATIONS_URL,
    FredCalendarClient,
    FredCalendarTiming,
)

AS_OF = date(2027, 3, 1)
GENERATED_AT = datetime(2027, 3, 1, 12, tzinfo=UTC)
LIMITS = TextExportLimits(
    max_news_items=2,
    max_news_chars=20,
    max_filing_chars=15,
    max_filing_chars_per_symbol=30,
    max_calendar_events=2,
    max_calendar_chars=20,
)


class _FixedClock:
    """Wall clock for the FRED adapter's `fetched_at` stamp."""

    def now(self) -> datetime:
        return GENERATED_AT

    def today(self) -> date:
        return AS_OF


def _fake_fred(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Offline stand-in for the three FRED endpoints the adapter chains."""
    del params
    payloads: dict[str, dict[str, Any]] = {
        FRED_RELEASE_DATES_URL: {
            "release_dates": [
                {
                    "release_id": 50,
                    "release_name": "Employment Situation",
                    "date": "2027-02-05",
                }
            ]
        },
        FRED_RELEASE_SERIES_URL: {"seriess": [{"id": "PAYEMS"}]},
        FRED_SERIES_OBSERVATIONS_URL: {
            "observations": [
                {"date": "2027-01-01", "value": "158200.0"},
                {"date": "2026-12-01", "value": "158000.0"},
            ]
        },
    }
    return payloads[url]


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


def _news(
    source_id: str,
    day: int,
    body: str = "A" * 100,
    related: tuple[str, ...] = (),
) -> TextItem:
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
        related_symbols=related,
    )


def _filing(
    source_id: str = "edgar:1",
    title: str | None = "10-Q - Apple",
    *,
    body: str = "B" * 100,
    sections: tuple[FilingSection, ...] = (),
) -> TextItem:
    stamp = datetime(2027, 2, 20, tzinfo=UTC)
    return TextItem(
        source_id=source_id,
        symbol="AAPL",
        source_type="filing",
        published_at=stamp,
        title=title,
        source_url="https://example.com/filing",
        content_text=body,
        fetched_at=stamp,
        filing_sections=sections,
    )


def _calendar_event(source_id: str, day: int, body: str = "C" * 100) -> TextItem:
    stamp = datetime(2027, 2, day, tzinfo=UTC)
    return TextItem(
        source_id=source_id,
        symbol=None,
        source_type="calendar",
        published_at=stamp,
        title="FOMC meeting",
        source_url=f"https://example.com/{source_id}",
        content_text=body,
        fetched_at=stamp,
    )


def _request(
    *text_items: TextItem, calendar_events: tuple[TextItem, ...] = ()
) -> ExportRequest:
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
        run_id=UUID("123e4567-e89b-12d3-a456-426614174000"),
        strategy_key="default",
        generated_at=GENERATED_AT,
        regime_snapshot=_snapshot(),
        exposure_decision=_exposure(),
        performance_summary=None,
        candidates=(candidate,),
        limits=LIMITS,
        calendar_events=calendar_events,
    )


class TestBuildAnalysisInput:
    def test_it_stamps_the_agreed_schema_version_and_as_of(self):
        payload = build_analysis_input(_request())

        assert payload.schema_version == "analysis-input-v3"
        assert payload.as_of == AS_OF
        assert str(payload.run_id) == "123e4567-e89b-12d3-a456-426614174000"
        assert payload.strategy_key == "default"
        assert len(payload.input_digest) == 64
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

    def test_news_with_a_blank_summary_does_not_displace_one_with_a_body(self):
        payload = build_analysis_input(
            _request(
                _news("finnhub:1", 28, body=""),
                _news("finnhub:2", 20, body="A" * 100),
                _news("finnhub:3", 10, body="A" * 100),
            )
        )

        assert [item.source_id for item in payload.candidates[0].news] == [
            "finnhub:2",
            "finnhub:3",
        ]

    def test_news_not_mentioning_the_symbol_is_demoted_below_news_that_does(self):
        payload = build_analysis_input(
            _request(
                _news("finnhub:1", 28, related=("MSFT",)),
                _news("finnhub:2", 20, related=("AAPL",)),
                _news("finnhub:3", 10, related=("AAPL", "MSFT")),
            )
        )

        assert [item.source_id for item in payload.candidates[0].news] == [
            "finnhub:2",
            "finnhub:3",
        ]

    def test_off_target_news_still_fills_the_cap_when_nothing_is_on_target(self):
        payload = build_analysis_input(
            _request(
                _news("finnhub:1", 20, related=("MSFT",)),
                _news("finnhub:2", 25, related=("MSFT",)),
                _news("finnhub:3", 28, related=("MSFT",)),
            )
        )

        assert [item.source_id for item in payload.candidates[0].news] == [
            "finnhub:3",
            "finnhub:2",
        ]

    def test_news_without_related_tickers_is_not_demoted(self):
        payload = build_analysis_input(
            _request(
                _news("finnhub:1", 28, related=("MSFT",)),
                _news("finnhub:2", 10, related=()),
            )
        )

        assert [item.source_id for item in payload.candidates[0].news] == [
            "finnhub:2",
            "finnhub:1",
        ]

    def test_relevance_ordering_beats_a_newer_but_off_target_body(self):
        payload = build_analysis_input(
            _request(
                _news("finnhub:1", 28, body="A" * 100, related=("MSFT",)),
                _news("finnhub:2", 20, body="", related=("AAPL",)),
                _news("finnhub:3", 10, body="A" * 100, related=("AAPL",)),
            )
        )

        assert [item.source_id for item in payload.candidates[0].news] == [
            "finnhub:3",
            "finnhub:2",
        ]

    def test_news_selection_does_not_depend_on_collection_order(self):
        items = (
            _news("finnhub:1", 28, related=("MSFT",)),
            _news("finnhub:2", 20, related=("AAPL",)),
            _news("finnhub:3", 20, related=("AAPL",)),
            _news("finnhub:4", 10, related=()),
        )

        first = build_analysis_input(_request(*items)).candidates[0].news
        second = build_analysis_input(_request(*reversed(items))).candidates[0].news

        assert [item.source_id for item in first] == ["finnhub:3", "finnhub:2"]
        assert [item.model_dump() for item in first] == [
            item.model_dump() for item in second
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
        assert filing.coverage is not None
        assert filing.coverage.selection_mode == "head_fallback"
        assert filing.coverage.is_truncated is True

    def test_symbol_budget_prioritizes_ten_q_over_newer_long_form(self):
        newer = _filing("edgar:8k", "8-K - Apple", body="8" * 100)
        older = _filing("edgar:10q", body="Q" * 100)
        older = replace(
            older,
            published_at=datetime(2027, 2, 19, tzinfo=UTC),
        )

        payload = build_analysis_input(_request(newer, older))

        by_id = {filing.source_id: filing for filing in payload.candidates[0].filings}
        assert len(by_id["edgar:10q"].text) == LIMITS.max_filing_chars
        assert len(by_id["edgar:8k"].text) == LIMITS.max_filing_chars
        assert sum(len(filing.text) for filing in by_id.values()) == 30

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

    def test_no_calendar_items_exports_an_empty_list(self):
        payload = build_analysis_input(_request())

        assert payload.context.calendar_events == []


class TestCalendarEvents:
    def test_calendar_events_are_newest_first_and_capped_by_count(self):
        payload = build_analysis_input(
            _request(
                calendar_events=(
                    _calendar_event("fred:1", 20),
                    _calendar_event("fred:2", 25),
                    _calendar_event("fred:3", 28),
                )
            )
        )

        assert [item.source_id for item in payload.context.calendar_events] == [
            "fred:3",
            "fred:2",
        ]

    def test_calendar_event_bodies_are_truncated_to_the_export_budget(self):
        payload = build_analysis_input(
            _request(calendar_events=(_calendar_event("fred:1", 20),))
        )

        assert (
            len(payload.context.calendar_events[0].summary) == LIMITS.max_calendar_chars
        )

    def test_the_provider_is_derived_from_the_source_id_prefix(self):
        payload = build_analysis_input(
            _request(calendar_events=(_calendar_event("fred:1", 20),))
        )

        assert payload.context.calendar_events[0].provider == "fred"

    def test_fred_calendar_summary_stays_distinct_from_its_title(self):
        """Regression for Issue #82: `title` and `summary` must not duplicate.

        Built from a real `FredCalendarClient` over an offline fake so the
        export contract is asserted against the adapter's actual output, and
        under the tightest export budget in this module (20 chars).
        """
        client = FredCalendarClient(
            "test-key",
            http_get=_fake_fred,
            timing=FredCalendarTiming(clock=_FixedClock(), sleep_fn=lambda _s: None),
        )
        events = client.fetch_calendar_events(
            date(2027, 2, 1), date(2027, 2, 28), as_of=date(2027, 2, 1)
        )

        payload = build_analysis_input(_request(calendar_events=tuple(events)))

        exported = payload.context.calendar_events[0]
        assert exported.title == "Employment Situation"
        assert exported.summary != exported.title
        assert exported.summary == "Scheduled for 2027-0"

    def test_a_calendar_item_never_appears_on_any_candidate(self):
        payload = build_analysis_input(
            _request(
                _news("finnhub:1", 20), calendar_events=(_calendar_event("fred:1", 20),)
            )
        )

        assert payload.candidates[0].news[0].source_id == "finnhub:1"
        assert len(payload.candidates[0].news) == 1


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
