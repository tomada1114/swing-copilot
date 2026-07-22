"""Fully offline five-symbol E2E smoke test for all nine daily-batch steps (FR-12).

Every external port (`DataProvider`, text clients, `EdgarClient`, LLM client,
`Notifier`, `Clock`) is a fake — no real network/API call anywhere, matching
every other test module's pattern in this repo (the autouse socket guard in
`tests/conftest.py` would fail the test immediately if one slipped through).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from swing_copilot.config import load_settings
from swing_copilot.data.base import BarFetchResult
from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary, SourcedFact
from swing_copilot.models import DailyRunOptions, RunStatus
from swing_copilot.pipeline.daily import DailyDependencies, run_daily
from swing_copilot.report.html_report import MARKET_STRIP_SYMBOLS
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - registers built-ins
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - registers built-ins
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import FundamentalsRecord, MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.base import TextItem
from swing_copilot.universe import UniverseMember

AS_OF = date(2027, 3, 1)
SYMBOLS = ["AAPL", "MSFT", "GOOG", "AMZN", "NVDA"]

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
        self.requested_symbols: list[list[str]] = []

    def get_daily_bars(self, symbols, start, end):
        del start, end
        self.requested_symbols.append(list(symbols))
        return BarFetchResult(bars=self._bars, failures=())

    def get_latest_bars(self, symbols, as_of):
        del symbols, as_of
        return BarFetchResult(bars=self._bars, failures=())


class FakeEdgarClient:
    def fetch_fundamentals(self, symbol, as_of):
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

    def fetch_filing_texts(self, symbol, form_types, *, as_of):
        del form_types
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


class FakeCalendarClient:
    def fetch_calendar_events(self, start, end):
        del start, end
        return []


class FakeLLMClient:
    def analyze(self, request):
        match = re.search(r"対象銘柄: (\S+)", request.prompt)
        symbol = match.group(1) if match else "UNKNOWN"
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


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, summary, report_path):
        self.calls.append((summary, report_path))
        return True


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
def deps(tmp_path):
    market_store = MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )
    state_store = StateStore(Database(tmp_path / "copilot.duckdb"))
    state_store.init_schema()
    settings = load_settings("config/settings.yaml")
    object.__setattr__(settings.notification, "enabled", True)

    return DailyDependencies(
        data_provider=FakeDataProvider(
            _uptrending_bars([*SYMBOLS, *MARKET_STRIP_SYMBOLS], AS_OF)
        ),
        market_store=market_store,
        state_store=state_store,
        settings=settings,
        universe=tuple(_member(symbol) for symbol in SYMBOLS),
        strategies_config=STRATEGIES_CONFIG,
        clock=FakeClock(),
        edgar_client=FakeEdgarClient(),
        news_client=FakeNewsClient(),
        calendar_client=FakeCalendarClient(),
        llm_client=FakeLLMClient(),
        notifier=FakeNotifier(),
        output_dir=str(tmp_path / "reports"),
    )


class TestFiveSymbolEndToEnd:
    def test_all_nine_steps_complete_and_produce_a_full_report(self, deps):
        # Live mode (with the browser suppressed) exercises every step,
        # including 8_notify, which dry-run mode intentionally skips.
        result = run_daily(DailyRunOptions(as_of=AS_OF, no_open=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()
        assert (result.report_path.parent / "latest.html").is_file()

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
            "6_llm",
            "7_report",
            "8_notify",
            "9_open",
        ]
        assert all(status == "success" for _step, status in steps)

    def test_report_contains_every_candidate_and_llm_content(self, deps):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.report_path is not None
        html = result.report_path.read_text(encoding="utf-8")
        for symbol in SYMBOLS:
            assert f'id="card-{symbol}"' in html
        assert "May indicate continued stability." in html

    def test_price_step_also_populates_the_market_strip(self, deps):
        # The market strip's symbols (SPY/QQQ/^VIX/^TNX) are never part of
        # the screening universe, so they only get bars if step 1 (prices)
        # explicitly fetches them too.
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.report_path is not None
        html = result.report_path.read_text(encoding="utf-8")
        assert "取得不可" not in html
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

    def test_notifier_is_called_with_the_generated_report_path(self, deps):
        # Dry-run suppresses notification, so this contract needs live mode.
        result = run_daily(DailyRunOptions(as_of=AS_OF, no_open=True), deps)

        assert len(deps.notifier.calls) == 1
        _summary, notified_path = deps.notifier.calls[0]
        assert notified_path == result.report_path

    def test_rerun_is_idempotent_and_gets_a_new_run_id(self, deps):
        first = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)
        second = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert first.run_id != second.run_id
        assert second.status == RunStatus.SUCCESS

        bars = deps.market_store.read_bars(
            SYMBOLS, AS_OF - timedelta(days=400), AS_OF, AS_OF
        )
        assert not bars.duplicated(subset=["symbol", "date"]).any()
