"""Fully offline five-symbol E2E smoke test for the daily-batch pipeline (FR-12).

Every external port (`DataProvider`, text clients, `EdgarClient`, `Clock`) is a
fake — no real network/API call anywhere, matching every other test module's
pattern in this repo (the autouse socket guard in `tests/conftest.py` would
fail the test immediately if one slipped through). Discord notification
(`Notifier`) is no longer one of `copilot-daily`'s own steps (Issue #383): it
now runs once per day from `scripts/notify_daily.py`, well after the
qualitative verdict exists, so it has its own offline coverage in
`tests/report/test_verdict_notification.py` / `tests/test_notify_daily.py`.

Qualitative analysis itself is not performed by `copilot-daily` (it exports
`analysis_input.json` for the `swing-daily` skill and stops); this suite
therefore asserts on the *exported* analysis input, not on any LLM prompt.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from swing_copilot.analysis.cli import ingest
from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.analysis.validate import AnalysisIngestError
from swing_copilot.config import StrategiesConfig, load_settings
from swing_copilot.models import DailyRunOptions, RunMode, RunStatus
from swing_copilot.pipeline.daily import DailyDependencies, run_daily
from swing_copilot.report.daily_brief import (
    MARKET_STRIP_SYMBOLS,
    PENDING_ANALYSIS_MESSAGE,
)
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - registers built-ins
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - registers built-ins
)
from swing_copilot.screening.base import Candidate
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import FundamentalsRecord, MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.storage.verdict_records import VerdictReasonRecord, VerdictRecord
from swing_copilot.text.base import TextItem
from swing_copilot.universe import UniverseMember
from tests.analysis.conftest import RUN_ID as ARCHIVED_RUN_ID
from tests.analysis.conftest import input_payload, result_payload
from tests.support.fakes import (
    FixedClock,
    StubCalendarClient,
    StubDataProvider,
    StubEdgarClient,
    StubNewsClient,
)

AS_OF = date(2027, 3, 1)
#: A live run (no `--as-of`) dates itself from the newest bar the provider
#: returned, and `_uptrending_bars` stops one day short of `AS_OF`.
LIVE_RUN_DATE = AS_OF - timedelta(days=1)
SYMBOLS = ["AAPL", "MSFT", "GOOG", "AMZN", "NVDA"]

STRATEGIES_CONFIG = StrategiesConfig.model_validate(
    {
        "strategies": {
            "default": {
                "filters_all": ["volume_min"],
                "signals_all": ["trend_sma"],
                "candidate_limit": 10,
            }
        }
    }
)


#: The fixed `now()` `FixedClock(AS_OF, _NOW)` below returns.
_NOW = datetime(2027, 3, 1, 12, tzinfo=UTC)


def _one_fundamentals_record(symbol: str, as_of: datetime) -> list[FundamentalsRecord]:
    """A `StubEdgarClient` fundamentals factory, one healthy record per symbol."""
    return [
        FundamentalsRecord(
            accession_no=f"acc-{symbol}",
            symbol=symbol,
            form="10-Q",
            fiscal_period_end=AS_OF,
            filed_at=datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC),
            revenue=1_000_000.0,
            net_income=100_000.0,
            fcf=80_000.0,
            equity=500_000.0,
            assets=1_000_000.0,
            shares=1_000_000.0,
            source_url=f"https://www.sec.gov/{symbol}",
            fetched_at=as_of,
        )
    ]


def _one_filing_text(symbol: str, as_of: datetime) -> list[TextItem]:
    """A `StubEdgarClient` filing-texts factory, one filing per symbol."""
    return [
        TextItem(
            source_id=f"edgar:{symbol}",
            symbol=symbol,
            source_type="filing",
            published_at=as_of - timedelta(days=1),
            title=f"10-Q - {symbol}",
            source_url=f"https://www.sec.gov/{symbol}/10-Q",
            content_text=f"{symbol} reported steady quarterly results.",
            fetched_at=as_of,
        )
    ]


def _uptrending_bars(symbols: list[str], as_of: date, days: int = 260) -> pd.DataFrame:
    rows = []
    for symbol in symbols:
        for i in range(days):
            bar_date = as_of - timedelta(days=days - i)
            price = 100.0 + i * 0.5
            rows.append(
                {
                    "symbol": symbol,
                    "date": bar_date,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "volume": 2_000_000,
                }
            )
    return pd.DataFrame(rows)


def _member(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        company_name=f"{symbol} Inc.",
        gics_sector="Information Technology",
        source_symbol=symbol,
    )


def _offline_skill_result(input_payload: dict[str, object]) -> dict[str, object]:
    """Build the deterministic fixture written by the external skill boundary."""
    candidates = input_payload["candidates"]
    assert isinstance(candidates, list)
    return {
        "schema_version": "analysis-result-v3",
        "run_id": input_payload["run_id"],
        "as_of": input_payload["as_of"],
        "strategy_key": input_payload["strategy_key"],
        "input_digest": input_payload["input_digest"],
        "generated_by": "offline skill fixture",
        "symbols": [
            {
                "symbol": candidate["symbol"],
                "news_summary": None,
                "filing_analyses": [],
                "screening_assessment": {
                    "summary": "No extra qualitative concern.",
                    "strengths": [],
                    "concerns": [],
                },
                "verdict": {"recommendation": "proceed", "reasons": []},
            }
            for candidate in candidates
        ],
        "no_trade": False,
        "no_trade_reason": None,
    }


@pytest.fixture
def deps(tmp_path):
    market_store = MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )
    state_store = StateStore(Database(tmp_path / "copilot.duckdb"))
    state_store.init_schema()
    settings = load_settings("config/settings.yaml")

    return DailyDependencies(
        data_provider=StubDataProvider(
            _uptrending_bars([*SYMBOLS, *MARKET_STRIP_SYMBOLS], AS_OF)
        ),
        market_store=market_store,
        state_store=state_store,
        settings=settings,
        universe=tuple(_member(symbol) for symbol in SYMBOLS),
        strategies_config=STRATEGIES_CONFIG,
        clock=FixedClock(AS_OF, _NOW),
        edgar_client=StubEdgarClient(
            _one_filing_text, fundamentals=_one_fundamentals_record
        ),
        news_client=StubNewsClient(),
        calendar_client=StubCalendarClient(),
        output_dir=str(tmp_path / "reports"),
    )


class TestFiveSymbolEndToEnd:
    def test_all_pipeline_steps_complete_and_produce_a_markdown_brief(self, deps):
        result = run_daily(DailyRunOptions(as_of=AS_OF), deps)

        assert result.status == RunStatus.SUCCESS
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert result.report_path.suffix == ".md"
        assert (result.report_path.parent.parent / "latest.md").is_file()
        assert result.brief is not None

        with deps.state_store._database.connect() as conn:  # noqa: SLF001
            steps = conn.execute(
                "SELECT step, status FROM run_steps WHERE run_id = ? ORDER BY step",
                [str(result.run_id)],
            ).fetchall()
        assert [s[0] for s in steps] == [
            "1_prices",
            "2_fundamentals",
            "3_screening",
            "4_risk",
            "5_text",
            "6_analysis_export",
            "8_output",
            "postmortem",
            "retro_collect",
            "retro_evaluate",
            "track_update",
        ]
        assert dict(steps) == {
            "1_prices": "success",
            "2_fundamentals": "success",
            "3_screening": "success",
            "4_risk": "success",
            "5_text": "success",
            "6_analysis_export": "success",
            "8_output": "success",
            "postmortem": "success",
            "retro_collect": "success",
            "retro_evaluate": "success",
            "track_update": "success",
        }

    def test_markdown_contains_every_candidate_and_the_pending_analysis_placeholder(
        self, deps
    ):
        # `copilot-daily` never performs qualitative analysis itself, so its
        # own report always shows the pending-analysis placeholder -- the
        # `swing-daily` skill and `copilot-ingest-analysis` fill it in later.
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.report_path is not None
        markdown = result.report_path.read_text(encoding="utf-8")
        for symbol in SYMBOLS:
            assert f"## {symbol}" in markdown
        assert f"- 定性: {PENDING_ANALYSIS_MESSAGE}" in markdown

    def test_analysis_input_is_exported_with_expected_content(self, deps):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert result.analysis_input_path is not None
        assert result.analysis_input_path.is_file()

        payload = json.loads(result.analysis_input_path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == "analysis-input-v3"
        assert payload["run_id"] == str(result.run_id)
        assert payload["strategy_key"] == "default"
        assert len(payload["input_digest"]) == 64
        assert payload["as_of"] == AS_OF.isoformat()
        assert payload["context"]["market_regime"] is not None

        by_symbol = {item["symbol"]: item for item in payload["candidates"]}
        assert set(by_symbol) == set(SYMBOLS)
        for symbol, candidate in by_symbol.items():
            assert "<score_breakdown>" in candidate["score_breakdown"]
            assert "<risk_constraints>" in candidate["risk_constraints"]
            news_ids = {item["source_id"] for item in candidate["news"]}
            assert news_ids == {f"news:{symbol}"}
            filing_ids = {item["source_id"] for item in candidate["filings"]}
            assert filing_ids == {f"edgar:{symbol}"}
            assert candidate["filings"][0]["form_type"] == "10-Q"

    def test_same_day_runs_keep_independent_analysis_artifact_directories(self, deps):
        first = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)
        second = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True, allow_same_day_rerun=True),
            deps,
        )

        assert first.analysis_input_path is not None
        assert second.analysis_input_path is not None
        assert first.analysis_input_path != second.analysis_input_path
        assert first.analysis_input_path.parent.name == str(first.run_id)
        assert second.analysis_input_path.parent.name == str(second.run_id)
        assert (first.analysis_input_path.parent / "report_context.json").is_file()
        assert (second.analysis_input_path.parent / "report_context.json").is_file()

    def test_exported_run_identity_allows_offline_skill_result_ingest(self, deps):
        run = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert run.analysis_input_path is not None
        input_payload = json.loads(run.analysis_input_path.read_text(encoding="utf-8"))
        result_payload = _offline_skill_result(input_payload)
        result_path = run.analysis_input_path.with_name("analysis_result.json")
        result_path.write_text(json.dumps(result_payload), encoding="utf-8")

        report_path = ingest(
            run.analysis_input_path,
            result_path,
            run.analysis_input_path.with_name("report_context.json"),
        )

        assert report_path == run.report_path
        assert "No extra qualitative concern." in report_path.read_text(
            encoding="utf-8"
        )

    def test_mismatched_skill_result_preserves_the_daily_report(self, deps):
        run = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert run.analysis_input_path is not None
        assert run.report_path is not None
        input_payload = json.loads(run.analysis_input_path.read_text(encoding="utf-8"))
        result_payload = _offline_skill_result(input_payload)
        result_payload["run_id"] = "123e4567-e89b-12d3-a456-426614174999"
        result_path = run.analysis_input_path.with_name("analysis_result.json")
        result_path.write_text(json.dumps(result_payload), encoding="utf-8")

        latest_path = run.report_path.parent.parent / "latest.md"
        report_before = run.report_path.read_bytes()
        latest_before = latest_path.read_bytes()
        with pytest.raises(AnalysisIngestError, match="run_id"):
            ingest(
                run.analysis_input_path,
                result_path,
                run.analysis_input_path.with_name("report_context.json"),
            )

        assert run.report_path.read_bytes() == report_before
        assert latest_path.read_bytes() == latest_before

    def test_price_step_also_populates_the_market_strip(self, deps):
        # The market strip's symbols (SPY/QQQ/^VIX/^TNX) are never part of
        # the screening universe, so they only get bars if step 1 (prices)
        # explicitly fetches them too.
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.report_path is not None
        markdown = result.report_path.read_text(encoding="utf-8")
        assert "N/A" not in markdown.split("## Candidates", maxsplit=1)[0]
        for symbol in MARKET_STRIP_SYMBOLS:
            bars = deps.market_store.read_bars(
                [symbol], AS_OF - timedelta(days=5), AS_OF, AS_OF
            )
            assert not bars.empty

        # The price step must have actually asked the provider for the
        # market strip symbols, not just happened to already have their bars.
        assert deps.data_provider.requested_symbols
        requested = deps.data_provider.requested_symbols[-1]
        assert set(MARKET_STRIP_SYMBOLS).issubset(requested)

    def test_rerun_is_idempotent_and_gets_a_new_run_id(self, deps):
        first = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)
        second = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True, allow_same_day_rerun=True),
            deps,
        )

        assert first.run_id != second.run_id
        assert second.status == RunStatus.SUCCESS

        bars = deps.market_store.read_bars(
            SYMBOLS, AS_OF - timedelta(days=400), AS_OF, AS_OF
        )
        assert not bars.duplicated(subset=["symbol", "date"]).any()

    def test_live_current_run_includes_the_symbols_own_prior_verdicts(self, deps):
        _seed_prior_verdict(deps, AS_OF - timedelta(days=2), "相関が高かった")

        result = run_daily(DailyRunOptions(), deps)

        assert result.analysis_input_path is not None
        payload = json.loads(result.analysis_input_path.read_text(encoding="utf-8"))
        aapl = next(item for item in payload["candidates"] if item["symbol"] == "AAPL")
        assert aapl["prior_verdicts"] is not None
        assert "<prior_verdicts>" in aapl["prior_verdicts"]
        assert "相関が高かった" in aapl["prior_verdicts"]

    def test_explicit_as_of_run_does_not_include_prior_verdicts(self, deps):
        _seed_prior_verdict(deps, AS_OF - timedelta(days=1), "過去の判断")

        result = run_daily(DailyRunOptions(as_of=AS_OF), deps)

        assert result.analysis_input_path is not None
        payload = json.loads(result.analysis_input_path.read_text(encoding="utf-8"))
        aapl = next(item for item in payload["candidates"] if item["symbol"] == "AAPL")
        assert aapl["prior_verdicts"] is None


def _seed_prior_verdict(
    deps: DailyDependencies, run_date: date, reason_text: str
) -> None:
    """Archive one earlier verdict for AAPL, the way an ingest would have."""
    prior_id = deps.state_store.start_run(run_date, RunMode.LIVE, "prior")
    deps.state_store.record_candidates(
        [Candidate("AAPL", run_date, ("trend_sma",), {"close": 100.0}, 1)],
        prior_id,
        "default",
    )
    deps.state_store.replace_run_verdicts(
        prior_id,
        [
            VerdictRecord(
                run_id=prior_id,
                symbol="AAPL",
                as_of=run_date,
                strategy_key="default",
                recommendation="skip",
                reasons=(VerdictReasonRecord(text=reason_text, source_ids=()),),
                no_trade=False,
            )
        ],
        [],
    )


def _archive_ingested_run(output_dir: str, run_date: date) -> None:
    """Leave one past run's directory exactly as `copilot-ingest-analysis` does.

    Both documents are present and agree on `run_id`, which is what makes the
    run collectable; the verdict's `as_of` comes from the directory's date.
    """
    directory = Path(output_dir) / run_date.isoformat() / ARCHIVED_RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    archived_input = input_payload(as_of=run_date.isoformat())
    (directory / ANALYSIS_INPUT_FILENAME).write_text(
        json.dumps(archived_input), encoding="utf-8"
    )
    (directory / ANALYSIS_RESULT_FILENAME).write_text(
        json.dumps(
            result_payload(
                as_of=run_date.isoformat(),
                input_digest=archived_input["input_digest"],
            )
        ),
        encoding="utf-8",
    )


class TestPriorVerdictsReachBackToTheLastRun:
    """Issue #207: `<prior_verdicts>` must not lag the archive by a whole run.

    `retro_collect` is the only writer of `verdicts`, so while it ran after
    step 6 the exported block could only ever show verdicts up to D-2 -- the
    two most recent business days, the ones a repeat candidate is actually
    about, were silently blank.
    """

    def test_the_previous_days_verdict_is_exported_by_the_next_run(self, deps):
        archived_date = LIVE_RUN_DATE - timedelta(days=1)
        _archive_ingested_run(deps.output_dir, archived_date)

        result = run_daily(DailyRunOptions(), deps)

        assert result.run_date == LIVE_RUN_DATE
        assert result.analysis_input_path is not None
        payload = json.loads(result.analysis_input_path.read_text(encoding="utf-8"))
        aapl = next(item for item in payload["candidates"] if item["symbol"] == "AAPL")
        assert aapl["prior_verdicts"] is not None
        assert "<prior_verdicts>" in aapl["prior_verdicts"]
        assert f"日付: {archived_date.isoformat()}" in aapl["prior_verdicts"]
        assert "前回の判断: proceed" in aapl["prior_verdicts"]
        assert "No contradicting disclosure." in aapl["prior_verdicts"]

    @pytest.mark.parametrize(
        "day_offset", [0, 1], ids=["same-day-as-the-run", "after-the-run"]
    )
    def test_a_verdict_not_strictly_before_the_run_date_is_withheld(
        self, deps, day_offset
    ):
        # The point-in-time cutoff (`as_of < run_date`) is unchanged by the
        # reordering: collecting earlier must not let today's -- or a
        # future-dated -- verdict flow back into today's own input.
        archived_date = LIVE_RUN_DATE + timedelta(days=day_offset)
        _archive_ingested_run(deps.output_dir, archived_date)

        result = run_daily(DailyRunOptions(), deps)

        with deps.state_store.database.connect() as conn:
            collected = conn.execute("SELECT as_of FROM verdicts").fetchall()
        # The archive really was collected before the export ran, so the
        # exclusion below is the as-of rule and not a missing row.
        assert [row[0] for row in collected] == [archived_date]

        assert result.analysis_input_path is not None
        payload = json.loads(result.analysis_input_path.read_text(encoding="utf-8"))
        aapl = next(item for item in payload["candidates"] if item["symbol"] == "AAPL")
        assert aapl["prior_verdicts"] is None


class TestPriorVerdictOutcomesReachTheSameDaysExport:
    """Issue #209: an outcome maturing on day D belongs in day D's own export.

    `retro_evaluate` is the only writer of `verdict_outcomes`, so while it ran
    after step 6 every `<prior_verdicts>` entry whose horizon came due that
    morning was exported with its classification still blank, and the skill
    only saw it the next run.
    """

    def test_an_outcome_maturing_on_the_run_date_is_exported_with_it(self, deps):
        # `_uptrending_bars` writes one bar per calendar day, so the benchmark
        # calendar makes the 5th session after the archived run exactly the
        # live run's own date -- the boundary case (maturity == as_of).
        archived_date = LIVE_RUN_DATE - timedelta(days=5)
        _archive_ingested_run(deps.output_dir, archived_date)

        result = run_daily(DailyRunOptions(), deps)

        assert result.run_date == LIVE_RUN_DATE
        with deps.state_store.database.connect() as conn:
            matured = conn.execute(
                "SELECT as_of, horizon_days, classification FROM verdict_outcomes"
            ).fetchall()
        assert matured == [(LIVE_RUN_DATE, 5, "HIT")]

        assert result.analysis_input_path is not None
        payload = json.loads(result.analysis_input_path.read_text(encoding="utf-8"))
        aapl = next(item for item in payload["candidates"] if item["symbol"] == "AAPL")
        assert aapl["prior_verdicts"] is not None
        # The whole point of the reordering: the classification, not the
        # "評価期間が未到来" placeholder the old ordering always produced here.
        assert "結果: 5日: HIT (+1.10%)" in aapl["prior_verdicts"]
