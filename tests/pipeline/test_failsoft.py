"""Fail-soft boundary tests for pipeline/daily.py (FR-12, `docs/03_basic_design.md` 7).

Text collection (5) and LLM analysis (6) failures degrade the run but never
abort it: notification (7) and local output (8) still run when applicable.
complete. Market/store/screening (1-4) failures are fatal, exit nonzero, and
never corrupt state for a subsequent successful rerun.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pandas as pd
import pytest

if TYPE_CHECKING:
    from uuid import UUID

from swing_copilot.data.base import BarFetchResult
from swing_copilot.llm.client import BudgetExceededError
from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary, SourcedFact
from swing_copilot.models import DailyRunOptions, Position, RunStatus
from swing_copilot.paper.journal import PaperJournal
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


class ExplodingLLMClient:
    def analyze(self, request):
        del request
        msg = "Claude API unreachable"
        raise RuntimeError(msg)


class ExplodingCalendarClient:
    def fetch_calendar_events(self, start, end):
        del start, end
        msg = "FRED unreachable"
        raise RuntimeError(msg)


class ExplodingPostmortemStateStore(StateStore):
    """A real `StateStore`, except reading `.database` always raises.

    `run_postmortem_step` reaches `state_store.database` before any of its
    own history-query calls; overriding just that property simulates a
    genuine unexpected connectivity failure at that seam, without disturbing
    any other step's use of this same store (screening/risk/text/LLM all
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
    degrades gracefully (LLM step still succeeds) rather than crashing the run.
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


class PartiallyFailingLLMClient:
    """Raises `BudgetExceededError` for one candidate only; a real fake for the rest."""

    def __init__(self, failing_symbol: str):
        self._failing_symbol = failing_symbol

    def analyze(self, request):
        match = re.search(r"対象銘柄: (\S+)", request.prompt)
        symbol = match.group(1) if match else "UNKNOWN"
        if symbol == self._failing_symbol:
            msg = "Monthly LLM budget cap would be exceeded"
            raise BudgetExceededError(msg)
        if request.schema is NewsSummary:
            return NewsSummary(
                symbol=symbol,
                period="test-period",
                facts=[
                    SourcedFact(
                        statement="Fake fact.", source_ids=list(request.source_ids)
                    )
                ],
                interpretation=["May indicate continued stability."],
                sentiment=1,
                risk_flags=[],
                sources=["https://example.com"],
                catalyst_quality="none",
                catalyst_quality_source_ids=list(request.source_ids),
            )
        return FilingAnalysis(
            symbol=symbol,
            filing_type="10-Q",
            facts=[
                SourcedFact(
                    statement="Fake filing fact.", source_ids=list(request.source_ids)
                )
            ],
            interpretation=["May suggest steady operations."],
            red_flags=[],
            yoy_changes=[],
            guidance_direction="neutral",
        )


class CapturingLLMClient:
    """A real fake that records every `AnalyzeRequest` it was called with.

    P2-12: used to assert on prompt *content* (decision-context blocks),
    mirroring `tests/test_e2e_smoke.py::FakeLLMClient`'s pattern.
    """

    def __init__(self):
        self.requests = []

    def analyze(self, request):
        self.requests.append(request)
        match = re.search(r"対象銘柄: (\S+)", request.prompt)
        symbol = match.group(1) if match else "UNKNOWN"
        return NewsSummary(
            symbol=symbol,
            period="test-period",
            facts=[
                SourcedFact(statement="Fake fact.", source_ids=list(request.source_ids))
            ],
            interpretation=["May indicate continued stability."],
            sentiment=1,
            risk_flags=[],
            sources=["https://example.com"],
            catalyst_quality="none",
            catalyst_quality_source_ids=list(request.source_ids),
        )


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
        assert _step_status(state_store, result.run_id, "6_llm") == "skipped"
        assert _step_status(state_store, result.run_id, "8_output") == "success"

    def test_degraded_report_shows_the_screening_only_fallback_message(self, base_deps):
        deps = replace(base_deps, news_client=ExplodingNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.report_path is not None
        markdown = result.report_path.read_text(encoding="utf-8")
        assert "本日はニュース・開示分析を取得できませんでした" in markdown
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


class TestLLMAnalysisFailureDegrades:
    def test_llm_failure_degrades_but_still_completes_the_run(
        self, base_deps, state_store
    ):
        deps = replace(
            base_deps, news_client=FakeNewsClient(), llm_client=ExplodingLLMClient()
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert _step_status(state_store, result.run_id, "6_llm") == "failed"
        assert _step_status(state_store, result.run_id, "8_output") == "success"


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
        # step 6 still skips: no llm_client configured on base_deps.
        assert _step_status(state_store, result.run_id, "6_llm") == "skipped"

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol FROM text_items WHERE source_type = 'news'"
            ).fetchall()
        # AAPL's news survived MSFT's fetch failure instead of being discarded.
        assert {row[0] for row in rows} == {"AAPL"}


class TestMixedOutcomeLLMStepPreservesSuccesses:
    def test_one_candidates_budget_error_keeps_the_other_candidates_summary(
        self, base_deps, state_store
    ):
        deps = replace(
            base_deps,
            news_client=FakeNewsClient(),
            llm_client=PartiallyFailingLLMClient("MSFT"),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert _step_status(state_store, result.run_id, "6_llm") == "failed"
        assert _step_status(state_store, result.run_id, "8_output") == "success"

        markdown = result.report_path.read_text(encoding="utf-8")
        # AAPL's summary survived MSFT's mid-loop BudgetExceededError instead
        # of being discarded along with it.
        assert "May indicate continued stability." in markdown
        # Not the total-failure fallback: this is a partial, per-candidate
        # degradation, not "no LLM output produced at all".
        assert "本日はニュース・開示分析を取得できませんでした" not in markdown


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


class TestPerformanceSummaryReachesLLMPrompt:
    """P2-12 (REQ-003): P1-06's PaperJournal.summarize_performance() wiring."""

    def test_closed_paper_trade_performance_reaches_the_news_prompt(
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
        llm_client = CapturingLLMClient()
        deps = replace(base_deps, news_client=FakeNewsClient(), llm_client=llm_client)

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert llm_client.requests
        assert any(
            "<performance_summary>" in request.prompt for request in llm_client.requests
        )
        assert any(
            "クローズ済み取引数: 1" in request.prompt for request in llm_client.requests
        )

    def test_no_closed_trades_yet_omits_the_performance_block_without_failing(
        self, base_deps
    ):
        llm_client = CapturingLLMClient()
        deps = replace(base_deps, news_client=FakeNewsClient(), llm_client=llm_client)

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert llm_client.requests
        assert all(
            "<performance_summary>" not in request.prompt
            for request in llm_client.requests
        )

    def test_summarize_performance_failure_degrades_gracefully_not_fatally(
        self, base_deps, tmp_path
    ):
        exploding_state_store = ExplodingPerformanceSummaryStateStore(
            Database(tmp_path / "copilot.duckdb")
        )
        llm_client = CapturingLLMClient()
        deps = replace(
            base_deps,
            state_store=exploding_state_store,
            news_client=FakeNewsClient(),
            llm_client=llm_client,
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        # The performance-summary lookup failure is swallowed by
        # `_compute_performance_summary()`'s own try/except: it never
        # reaches a per-candidate `summarize_news`/`analyze_filing` call, so
        # the LLM step itself still succeeds (unlike a real per-candidate
        # LLM failure, which would degrade the run).
        assert result.status == RunStatus.SUCCESS
        assert llm_client.requests
        assert all(
            "<performance_summary>" not in request.prompt
            for request in llm_client.requests
        )
