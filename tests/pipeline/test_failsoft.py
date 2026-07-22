"""Fail-soft boundary tests for pipeline/daily.py (FR-12, `docs/03_basic_design.md` 7).

Text collection (5) and LLM analysis (6) failures degrade the run but never
abort it: report (7), notify (8), and browser-open (9) always attempt to
complete. Market/store/screening (1-4) failures are fatal, exit nonzero, and
never corrupt state for a subsequent successful rerun.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import pytest

if TYPE_CHECKING:
    from uuid import UUID

from swing_copilot.data.base import BarFetchResult
from swing_copilot.models import DailyRunOptions, RunStatus
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
            "ranking": ["rsi14_asc", "avg_volume_desc", "symbol_asc"],
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


class FailingNotifier:
    def notify(self, summary, report_path):
        del summary, report_path
        return False


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
        assert _step_status(state_store, result.run_id, "7_report") == "success"

    def test_degraded_report_shows_the_screening_only_fallback_message(self, base_deps):
        deps = replace(base_deps, news_client=ExplodingNewsClient())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.report_path is not None
        html = result.report_path.read_text(encoding="utf-8")
        assert "本日はニュース・開示分析を取得できませんでした" in html
        # Non-LLM card content still renders normally (fail-soft, not hidden).
        assert "テクニカル" in html


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
        assert _step_status(state_store, result.run_id, "7_report") == "success"


class TestNotifyFailureDegrades:
    def test_notify_failure_degrades_but_the_report_still_exists(
        self, base_deps, state_store, settings
    ):
        object.__setattr__(settings.notification, "enabled", True)
        deps = replace(base_deps, notifier=FailingNotifier())

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert _step_status(state_store, result.run_id, "8_notify") == "failed"
        assert _step_status(state_store, result.run_id, "9_open") == "success"


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
                    "ranking": ["rsi14_asc", "avg_volume_desc", "symbol_asc"],
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
