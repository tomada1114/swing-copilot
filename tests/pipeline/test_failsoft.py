"""Fail-soft boundary tests for pipeline/daily.py (FR-12, `docs/03_basic_design.md` 7).

Text collection (5) and analysis-input export (6) failures degrade the run but
never abort it: notification (7) and local output (8) still run when applicable.
complete. Market/store/screening (1-4) failures are fatal, exit nonzero, and
never corrupt state for a subsequent successful rerun.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pandas as pd
import pytest

if TYPE_CHECKING:
    from uuid import UUID

from swing_copilot.analysis.export import ANALYSIS_INPUT_FILENAME
from swing_copilot.data.base import BarFetchResult
from swing_copilot.models import DailyRunOptions, Position, RunStatus
from swing_copilot.paper.journal import PaperJournal
from swing_copilot.pipeline import daily as daily_module
from swing_copilot.pipeline.daily import DailyDependencies, run_daily
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - registers built-ins
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - registers built-ins
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.base import TextItem
from swing_copilot.universe import UniverseMember

AS_OF = date(2027, 3, 1)

STRATEGIES_CONFIG = {
    "strategies": {
        "default": {
            "filters_all": ["volume_min"],
            "signals_all": ["trend_sma"],
            "candidate_limit": 10,
        }
    }
}


class FakeClock:
    def today(self):
        return AS_OF

    def now(self):
        return datetime(2027, 3, 1, 12, tzinfo=UTC)


class FakeDataProvider:
    def __init__(self, bars: pd.DataFrame):
        self._bars = bars

    def get_daily_bars(self, symbols, start, end):
        del symbols, start, end
        return BarFetchResult(bars=self._bars, failures=())

    def get_latest_bars(self, symbols, as_of):
        del symbols, as_of
        return BarFetchResult(bars=self._bars, failures=())


class ExplodingNewsClient:
    def fetch_company_news(self, symbol, since, *, as_of):
        del symbol, since, as_of
        msg = "Finnhub unreachable"
        raise RuntimeError(msg)


class FakeNewsClient:
    def fetch_company_news(self, symbol, since, *, as_of):
        del since
        return [
            TextItem(
                source_id=f"news:{symbol}",
                symbol=symbol,
                source_type="news",
                published_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
                title=f"{symbol} news",
                source_url=f"https://example.com/{symbol}",
                content_text=f"{symbol} announced a new product line.",
                fetched_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
            )
        ]


class ExplodingCalendarClient:
    def fetch_calendar_events(self, start, end):
        del start, end
        msg = "FRED unreachable"
        raise RuntimeError(msg)


class FakeCalendarClient:
    """Returns one symbol-less macro event, mirroring `FredCalendarClient`."""

    def fetch_calendar_events(self, start, end):
        del end
        stamp = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
        return [
            TextItem(
                source_id="fred:1:2027-03-05",
                symbol=None,
                source_type="calendar",
                published_at=stamp,
                title="Employment Situation",
                source_url="https://fred.stlouisfed.org/release?rid=1",
                content_text="Employment Situation",
                fetched_at=stamp,
            )
        ]


class ExplodingPostmortemStateStore(StateStore):
    """A real `StateStore`, except reading `.database` always raises.

    `run_postmortem_step` reaches `state_store.database` before any of its
    own history-query calls; overriding just that property simulates a
    genuine unexpected connectivity failure at that seam, without disturbing
    any other step's use of this same store (screening/risk/text/export all
    keep working normally against the real underlying `Database`).
    """

    @property
    def database(self):
        msg = "state store connectivity failure"
        raise RuntimeError(msg)


class ExplodingPerformanceSummaryStateStore(StateStore):
    """A real `StateStore`, except `get_closed_positions_with_strategy()` always raises.

    P2-12: `PaperJournal.summarize_performance()` reaches this method first;
    overriding just it simulates an unexpected storage failure at that one
    seam, proving `_compute_performance_summary()`'s defensive try/except
    degrades gracefully (the export step still succeeds) rather than crashing
    the run.
    """

    def get_closed_positions_with_strategy(self, is_paper=True, as_of=None):
        del is_paper, as_of
        msg = "closed-positions query failure"
        raise RuntimeError(msg)


class PartiallyFailingNewsClient:
    """Raises only for `failing_symbol`; returns real news for every other symbol."""

    def __init__(self, failing_symbol: str):
        self._failing_symbol = failing_symbol

    def fetch_company_news(self, symbol, since, *, as_of):
        del since
        if symbol == self._failing_symbol:
            msg = f"Finnhub unreachable for {symbol}"
            raise RuntimeError(msg)
        return [
            TextItem(
                source_id=f"news:{symbol}",
                symbol=symbol,
                source_type="news",
                published_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
                title=f"{symbol} news",
                source_url=f"https://example.com/{symbol}",
                content_text=f"{symbol} announced a new product line.",
                fetched_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
            )
        ]


class FailingNotifier:
    def notify(self, summary, report_path):
        del summary, report_path
        return False


class _AssertNeverCalledNotifier:
    """Proves step 8 never reaches the real webhook client during dry-run."""

    def notify(self, summary, report_path):
        del summary, report_path
        msg = "Notifier.notify() must not be called in dry-run mode"
        raise AssertionError(msg)


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


@pytest.fixture
def market_store(tmp_path):
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store


@pytest.fixture
def base_deps(settings, market_store, state_store, tmp_path):
    universe = (_member("AAPL"), _member("MSFT"))
    return DailyDependencies(
        data_provider=FakeDataProvider(_uptrending_bars(["AAPL", "MSFT"], AS_OF)),
        market_store=market_store,
        state_store=state_store,
        settings=settings,
        universe=universe,
        strategies_config=STRATEGIES_CONFIG,
        clock=FakeClock(),
        output_dir=str(tmp_path / "reports"),
    )


def _step_status(state_store: StateStore, run_id: UUID, step: str) -> str:
    with state_store._database.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT status FROM run_steps WHERE run_id = ? AND step = ?",
            [str(run_id), step],
        ).fetchone()
    assert row is not None
    status: str = row[0]
    return status


class TestTextCollectionFailureDegrades:
    def test_text_failure_degrades_but_still_completes_the_run(
        self, base_deps, state_store
    ):
        deps = replace(base_deps, news_client=ExplodingNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert _step_status(state_store, result.run_id, "5_text") == "failed"
        assert (
            _step_status(state_store, result.run_id, "6_analysis_export") == "skipped"
        )
        assert _step_status(state_store, result.run_id, "8_output") == "success"

    def test_degraded_report_shows_the_screening_only_fallback_message(self, base_deps):
        deps = replace(base_deps, news_client=ExplodingNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.report_path is not None
        markdown = result.report_path.read_text(encoding="utf-8")
        assert "分析待ち（swing-daily スキルで分析を実行してください）" in markdown
        assert "## Candidates" in markdown


class TestTextStepDetailMessagesAreTruthful:
    """`_text_step_outcome`'s detail must state what actually happened.

    Distinguishes a calendar-only failure with no target symbols from
    genuine per-symbol failures, and from both together.
    """

    def test_calendar_failure_with_zero_target_symbols_is_stated_truthfully(
        self, base_deps, state_store, settings
    ):
        # No candidates and no held positions: guarantee zero target
        # symbols by making the liquidity filter reject every symbol.
        object.__setattr__(settings.technical_signals.volume, "min_avg_volume", 10**12)
        deps = replace(base_deps, calendar_client=ExplodingCalendarClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        with state_store._database.connect() as conn:  # noqa: SLF001
            detail = conn.execute(
                "SELECT detail FROM run_steps WHERE run_id = ? AND step = '5_text'",
                [str(result.run_id)],
            ).fetchone()[0]
        assert detail == (
            "calendar fetch failed; no target symbols (0 candidates, 0 held positions)"
        )

    def test_genuine_per_symbol_failures_are_distinguished_from_calendar_failure(
        self, base_deps, state_store
    ):
        deps = replace(base_deps, news_client=ExplodingNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        with state_store._database.connect() as conn:  # noqa: SLF001
            detail = conn.execute(
                "SELECT detail FROM run_steps WHERE run_id = ? AND step = '5_text'",
                [str(result.run_id)],
            ).fetchone()[0]
        assert detail == (
            "per-symbol fetch failed for 2/2 target symbol(s): ['AAPL', 'MSFT']"
        )

    def test_calendar_and_per_symbol_failures_are_both_reported(
        self, base_deps, state_store
    ):
        deps = replace(
            base_deps,
            news_client=ExplodingNewsClient(),
            calendar_client=ExplodingCalendarClient(),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        with state_store._database.connect() as conn:  # noqa: SLF001
            detail = conn.execute(
                "SELECT detail FROM run_steps WHERE run_id = ? AND step = '5_text'",
                [str(result.run_id)],
            ).fetchone()[0]
        assert detail == (
            "calendar fetch failed and per-symbol fetch failed for "
            "2/2 target symbol(s): ['AAPL', 'MSFT']"
        )


class TestAnalysisExportFailureDegrades:
    def test_export_write_failure_degrades_but_still_completes_the_run(
        self, base_deps, state_store, monkeypatch
    ):
        def _raise(*_args, **_kwargs):
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(daily_module, "write_analysis_input", _raise)
        deps = replace(base_deps, news_client=FakeNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert result.analysis_input_path is None
        assert _step_status(state_store, result.run_id, "6_analysis_export") == "failed"
        assert _step_status(state_store, result.run_id, "8_output") == "success"

    def test_no_candidates_skips_the_export_without_degrading(
        self, base_deps, state_store, settings
    ):
        object.__setattr__(settings.technical_signals.volume, "min_avg_volume", 10**12)
        deps = replace(base_deps, news_client=FakeNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert result.analysis_input_path is None
        assert (
            _step_status(state_store, result.run_id, "6_analysis_export") == "skipped"
        )


class TestPostmortemFailureDegrades:
    def test_postmortem_failure_degrades_but_still_completes_the_run(
        self, base_deps, tmp_path
    ):
        exploding_state_store = ExplodingPostmortemStateStore(
            Database(tmp_path / "copilot.duckdb")
        )
        deps = replace(base_deps, state_store=exploding_state_store)

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert (
            _step_status(exploding_state_store, result.run_id, "postmortem") == "failed"
        )
        assert (
            _step_status(exploding_state_store, result.run_id, "8_output") == "success"
        )

    def test_postmortem_succeeds_when_no_prior_runs_exist_yet(
        self, base_deps, state_store
    ):
        # Common case for a brand-new install: nothing to look back at yet.
        # This must not degrade the run.
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), base_deps)

        assert result.status == RunStatus.SUCCESS
        assert result.exit_code == 0
        assert _step_status(state_store, result.run_id, "postmortem") == "success"


class TestMaeMfeFailureDegrades:
    def test_excursion_storage_failure_does_not_abort_output(
        self,
        base_deps: DailyDependencies,
        state_store: StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(
            _state_store: StateStore,
            _market_store: MarketStore,
            _as_of: date,
        ) -> None:
            msg = "excursion storage unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(daily_module, "update_position_excursions", _raise)

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), base_deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert _step_status(state_store, result.run_id, "mae_mfe") == "failed"
        assert _step_status(state_store, result.run_id, "8_output") == "success"
        assert "MAE/MFE: unexpected error" in result.report_path.read_text(
            encoding="utf-8"
        )


class TestUniverseFallbackDegrades:
    def test_fallback_snapshot_is_recorded_and_visible_in_a_degraded_report(
        self, base_deps: DailyDependencies, state_store: StateStore
    ) -> None:
        warning = (
            "Universe refresh failed; using persisted snapshot 2026-07-13: "
            "wikipedia unavailable"
        )
        deps = replace(base_deps, universe_warning=warning)

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert _step_status(state_store, result.run_id, "0_universe") == "failed"
        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT detail FROM run_steps WHERE run_id = ? AND step = '0_universe'",
                [str(result.run_id)],
            ).fetchone()
        assert row is not None
        detail = row[0]
        assert detail == warning
        assert result.report_path is not None
        assert warning in result.report_path.read_text(encoding="utf-8")


class TestMixedOutcomeTextStepPreservesSuccesses:
    def test_one_symbol_failing_keeps_the_other_symbols_news_and_degrades_the_run(
        self, base_deps, state_store
    ):
        deps = replace(base_deps, news_client=PartiallyFailingNewsClient("MSFT"))

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert _step_status(state_store, result.run_id, "5_text") == "failed"
        # Step 6 still exports: partial text is still worth analyzing.
        assert (
            _step_status(state_store, result.run_id, "6_analysis_export") == "success"
        )

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol FROM text_items WHERE source_type = 'news'"
            ).fetchall()
        # AAPL's news survived MSFT's fetch failure instead of being discarded.
        assert {row[0] for row in rows} == {"AAPL"}


class TestPartialTextStillExportsEveryCandidate:
    def test_symbol_without_news_is_still_exported_for_screening_assessment(
        self, base_deps, state_store
    ):
        deps = replace(base_deps, news_client=PartiallyFailingNewsClient("MSFT"))

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert _step_status(state_store, result.run_id, "5_text") == "failed"
        assert result.analysis_input_path is not None
        payload = json.loads(result.analysis_input_path.read_text(encoding="utf-8"))
        by_symbol = {item["symbol"]: item for item in payload["candidates"]}
        # Both candidates are exported; MSFT simply carries no news.
        assert set(by_symbol) == {"AAPL", "MSFT"}
        assert by_symbol["MSFT"]["news"] == []
        assert [item["source_id"] for item in by_symbol["AAPL"]["news"]] == [
            "news:AAPL"
        ]


class TestHeldSymbolGetsTextCoverage:
    def test_held_symbol_absent_from_todays_candidates_still_gets_text_coverage(
        self, base_deps, state_store
    ):
        state_store.upsert_position(
            Position(
                position_id=uuid4(),
                symbol="TSLA",
                is_paper=True,
                entry_date=AS_OF - timedelta(days=5),
                entry_price=100.0,
                shares=10,
                status="open",
            )
        )
        deps = replace(base_deps, news_client=FakeNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol FROM text_items WHERE source_type = 'news'"
            ).fetchall()
        symbols_covered = {row[0] for row in rows}
        # TSLA is held but not part of `universe`/today's screening candidates.
        assert "TSLA" in symbols_covered
        assert {"AAPL", "MSFT"} <= symbols_covered


class TestNotifyFailureDegrades:
    def test_notify_failure_degrades_but_the_report_still_exists(
        self, base_deps, state_store, settings
    ):
        object.__setattr__(settings.notification, "enabled", True)
        deps = replace(base_deps, notifier=FailingNotifier())

        # A live (non-dry-run) run: dry-run mode always suppresses step 7
        # (see `TestDryRunSuppressesNotification` below), so exercising the
        # actual notify-failure path requires `is_dry_run=False`.
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=False), deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert _step_status(state_store, result.run_id, "7_notify") == "failed"
        assert _step_status(state_store, result.run_id, "8_output") == "success"


class TestDryRunSuppressesNotification:
    def test_dry_run_skips_notify_even_when_notification_is_enabled(
        self, base_deps, state_store, settings
    ):
        object.__setattr__(settings.notification, "enabled", True)
        deps = replace(base_deps, notifier=_AssertNeverCalledNotifier())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert _step_status(state_store, result.run_id, "7_notify") == "skipped"
        with state_store._database.connect() as conn:  # noqa: SLF001
            detail = conn.execute(
                "SELECT detail FROM run_steps WHERE run_id = ? AND step = ?",
                [str(result.run_id), "7_notify"],
            ).fetchone()[0]
        assert detail == "skipped: dry-run mode"


class TestMarketFailureIsFatalAndRerunnable:
    def test_price_fetch_failure_is_fatal_and_a_later_run_succeeds(
        self, settings, market_store, state_store, tmp_path
    ):
        universe = (_member("AAPL"),)
        empty_bars = pd.DataFrame(
            columns=["symbol", "date", "open", "high", "low", "close", "volume"]
        )
        failing_deps = DailyDependencies(
            data_provider=FakeDataProvider(empty_bars),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=FakeClock(),
            output_dir=str(tmp_path / "reports"),
        )

        failed = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), failing_deps)
        assert failed.status == RunStatus.FAILED
        assert failed.exit_code == 1
        assert failed.report_path is None

        working_deps = replace(
            failing_deps,
            data_provider=FakeDataProvider(_uptrending_bars(["AAPL"], AS_OF)),
        )
        retried = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), working_deps)

        assert retried.status == RunStatus.SUCCESS
        assert retried.run_id != failed.run_id


class TestScreeningFailureIsFatalAndRerunnable:
    def test_unregistered_filter_key_is_fatal_and_a_fixed_rerun_succeeds(
        self, base_deps, state_store
    ):
        broken_strategies_config = {
            "strategies": {
                "default": {
                    "filters_all": ["nonexistent_filter"],
                    "signals_all": ["trend_sma"],
                    "candidate_limit": 10,
                }
            }
        }
        broken_deps = replace(base_deps, strategies_config=broken_strategies_config)

        failed = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), broken_deps)

        assert failed.status == RunStatus.FAILED
        assert failed.exit_code == 1
        assert _step_status(state_store, failed.run_id, "3_screening") == "failed"

        retried = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), base_deps)

        assert retried.status == RunStatus.SUCCESS
        assert retried.run_id != failed.run_id


class TestCalendarEventsReachAnalysisInput:
    """Symbol-less calendar `TextItem`s reach `context.calendar_events`.

    Never any candidate: they are collected but never attached to a symbol's
    `news`/`filings` since `TextItem.symbol` is `None`.
    """

    @staticmethod
    def _exported(result):
        assert result.analysis_input_path is not None
        return json.loads(result.analysis_input_path.read_text(encoding="utf-8"))

    def test_a_collected_calendar_event_reaches_the_run_wide_context(self, base_deps):
        deps = replace(
            base_deps,
            news_client=FakeNewsClient(),
            calendar_client=FakeCalendarClient(),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        payload = self._exported(result)
        calendar_events = payload["context"]["calendar_events"]
        assert [item["source_id"] for item in calendar_events] == ["fred:1:2027-03-05"]
        # Never attached to any candidate: it has no symbol to match.
        for candidate in payload["candidates"]:
            assert "fred:1:2027-03-05" not in [
                item["source_id"] for item in candidate["news"]
            ]

    def test_no_calendar_client_exports_an_empty_calendar_events_list(self, base_deps):
        deps = replace(base_deps, news_client=FakeNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert self._exported(result)["context"]["calendar_events"] == []


class TestPerformanceSummaryReachesAnalysisInput:
    """P2-12 (REQ-003): P1-06's PaperJournal.summarize_performance() wiring."""

    @staticmethod
    def _exported(result):
        assert result.analysis_input_path is not None
        return json.loads(result.analysis_input_path.read_text(encoding="utf-8"))

    def test_closed_paper_trade_performance_reaches_the_exported_context(
        self, base_deps, state_store
    ):
        position = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=AS_OF - timedelta(days=30),
            entry_price=100.0,
            shares=10,
            status="open",
            stop_price=95.0,
        )
        state_store.upsert_position(position)
        PaperJournal(state_store).close_position(
            position.position_id, AS_OF - timedelta(days=5), 110.0, "target"
        )
        deps = replace(base_deps, news_client=FakeNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        performance = self._exported(result)["context"]["performance_summary"]
        assert "<performance_summary>" in performance
        assert "クローズ済み取引数: 1" in performance

    def test_no_closed_trades_yet_omits_the_performance_block_without_failing(
        self, base_deps
    ):
        deps = replace(base_deps, news_client=FakeNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert self._exported(result)["context"]["performance_summary"] is None

    def test_summarize_performance_failure_degrades_gracefully_not_fatally(
        self, base_deps, tmp_path
    ):
        exploding_state_store = ExplodingPerformanceSummaryStateStore(
            Database(tmp_path / "copilot.duckdb")
        )
        deps = replace(
            base_deps,
            state_store=exploding_state_store,
            news_client=FakeNewsClient(),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        # The performance-summary lookup failure is swallowed by
        # `_compute_performance_summary()`'s own try/except, so the export
        # step still succeeds -- it just omits the performance block.
        assert result.status == RunStatus.SUCCESS
        assert self._exported(result)["context"]["performance_summary"] is None

    def test_exported_file_lands_beside_the_markdown_report(self, base_deps, tmp_path):
        deps = replace(base_deps, news_client=FakeNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert (
            result.analysis_input_path
            == (
                tmp_path
                / "reports"
                / AS_OF.isoformat()
                / str(result.run_id)
                / ANALYSIS_INPUT_FILENAME
            ).resolve()
        )
