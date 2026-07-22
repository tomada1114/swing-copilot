"""Tests for pipeline/daily.py's fatal steps 1-4 (FR-12).

Fail-soft steps 5-9 are covered by tests/pipeline/test_failsoft.py and
tests/test_e2e_smoke.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from swing_copilot.data.base import BarFetchResult, FetchFailure
from swing_copilot.models import DailyRunOptions, RunStatus
from swing_copilot.pipeline.daily import DailyDependencies, run_daily
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - imported for its @register_filter side effect
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - imported for its @register_signal side effect
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import FundamentalsRecord, MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import UniverseMember

AS_OF = date(2027, 3, 1)


class FakeClock:
    def today(self):
        return AS_OF

    def now(self):
        return datetime(2027, 3, 1, 12, tzinfo=UTC)


class FakeDataProvider:
    def __init__(self, bars: pd.DataFrame, failures: tuple[FetchFailure, ...] = ()):
        self._bars = bars
        self._failures = failures

    def get_daily_bars(self, symbols, start, end):
        del symbols, start, end
        return BarFetchResult(bars=self._bars, failures=self._failures)

    def get_latest_bars(self, symbols, as_of):
        del symbols, as_of
        return BarFetchResult(bars=self._bars, failures=self._failures)


def _bars_for(symbols: list[str], as_of: date, days: int = 210) -> pd.DataFrame:
    rows = []
    for symbol in symbols:
        for i in range(days):
            bar_date = as_of - timedelta(days=days - i)
            price = 100.0 + i * 0.1
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
        company_name=symbol,
        gics_sector="Information Technology",
        source_symbol=symbol,
    )


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
def deps(settings, market_store, state_store, tmp_path):
    universe = (_member("AAPL"), _member("MSFT"))
    bars = _bars_for(["AAPL", "MSFT"], AS_OF)
    return DailyDependencies(
        data_provider=FakeDataProvider(bars),
        market_store=market_store,
        state_store=state_store,
        settings=settings,
        universe=universe,
        strategies_config=STRATEGIES_CONFIG,
        clock=FakeClock(),
        edgar_client=None,
        output_dir=str(tmp_path / "reports"),
    )


class TestHappyPath:
    def test_completes_all_nine_steps_successfully(self, deps, state_store):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        assert result.exit_code == 0
        assert result.run_date == AS_OF

        with state_store._database.connect() as conn:  # noqa: SLF001
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
        # 1/3/4/7/9 succeed outright; 2/5/6/8 are deliberate skips (no
        # optional clients configured) — none of these are failures.
        assert all(status in {"success", "skipped"} for _step, status in steps)

        bars = deps.market_store.read_bars(
            ["AAPL", "MSFT"], AS_OF - timedelta(days=400), AS_OF, AS_OF
        )
        assert set(bars["fetched_at"]) == {pd.Timestamp("2027-03-01T12:00:00Z")}


class TestIdempotency:
    def test_two_runs_get_distinct_run_ids_and_no_duplicate_bars(
        self, deps, market_store
    ):
        first = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)
        second = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert first.run_id != second.run_id
        assert first.status == RunStatus.SUCCESS
        assert second.status == RunStatus.SUCCESS

        bars = market_store.read_bars(
            ["AAPL", "MSFT"], AS_OF - timedelta(days=400), AS_OF, AS_OF
        )
        # Re-running must not duplicate (symbol, date) rows.
        assert not bars.duplicated(subset=["symbol", "date"]).any()

    def test_two_runs_have_independent_step_histories(self, deps, state_store):
        first = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)
        second = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        with state_store._database.connect() as conn:  # noqa: SLF001
            first_steps = conn.execute(
                "SELECT count(*) FROM run_steps WHERE run_id = ?", [str(first.run_id)]
            ).fetchone()
            second_steps = conn.execute(
                "SELECT count(*) FROM run_steps WHERE run_id = ?", [str(second.run_id)]
            ).fetchone()
        assert first_steps == (9,)
        assert second_steps == (9,)


class TestFatalStepFailure:
    def test_price_fetch_failure_marks_run_failed_and_stops(
        self, settings, market_store, state_store
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
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), failing_deps)

        assert result.status == RunStatus.FAILED
        assert result.exit_code == 1

        with state_store._database.connect() as conn:  # noqa: SLF001
            steps = conn.execute(
                "SELECT step FROM run_steps WHERE run_id = ?", [str(result.run_id)]
            ).fetchall()
            run_row = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", [str(result.run_id)]
            ).fetchone()
        assert [s[0] for s in steps] == ["1_prices"]
        assert run_row == ("failed",)

    def test_failed_run_can_be_followed_by_a_successful_rerun(
        self, settings, market_store, state_store
    ):
        universe = (_member("AAPL"), _member("MSFT"))
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
        )
        failed_result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), failing_deps
        )
        assert failed_result.status == RunStatus.FAILED

        working_deps = DailyDependencies(
            data_provider=FakeDataProvider(_bars_for(["AAPL", "MSFT"], AS_OF)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=FakeClock(),
        )
        retry_result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), working_deps
        )

        assert retry_result.status == RunStatus.SUCCESS
        assert retry_result.run_id != failed_result.run_id


class TestAsOfDefaulting:
    def test_missing_as_of_uses_latest_date_in_fetched_bars(self, deps):
        result = run_daily(DailyRunOptions(is_dry_run=True), deps)
        assert result.run_date == AS_OF - timedelta(days=1)


class TestSymbolLimit:
    def test_limit_restricts_universe_to_first_n_symbols(self, deps):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True, limit=1), deps)
        assert result.status == RunStatus.SUCCESS


class TestFundamentalsStepSkipped:
    def test_no_edgar_client_records_step_as_skipped(self, deps, state_store):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, detail FROM run_steps WHERE run_id = ? AND step = '2_fundamentals'",
                [str(result.run_id)],
            ).fetchone()
        assert row[0] == "skipped"
        assert "skipped" in row[1]

    def test_edgar_client_partial_failure_still_succeeds(
        self, settings, market_store, state_store, tmp_path
    ):

        class FakeEdgarClient:
            def fetch_fundamentals(self, symbol, as_of):
                del as_of
                if symbol == "AAPL":
                    return [
                        FundamentalsRecord(
                            accession_no="acc-1",
                            symbol="AAPL",
                            form="10-Q",
                            fiscal_period_end=AS_OF,
                            filed_at=datetime.combine(
                                AS_OF, datetime.min.time(), tzinfo=UTC
                            ),
                            revenue=1.0,
                            net_income=1.0,
                            fcf=1.0,
                            equity=1.0,
                            assets=2.0,
                            shares=1.0,
                            source_url="https://www.sec.gov/example",
                            fetched_at=datetime.combine(
                                AS_OF, datetime.min.time(), tzinfo=UTC
                            ),
                        )
                    ]
                msg = "EDGAR unreachable"
                raise RuntimeError(msg)

            def fetch_filing_texts(self, symbol, form_types, *, as_of):
                del symbol, form_types, as_of
                return []

        universe = (_member("AAPL"), _member("MSFT"))
        deps_with_edgar = DailyDependencies(
            data_provider=FakeDataProvider(_bars_for(["AAPL", "MSFT"], AS_OF)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=FakeClock(),
            edgar_client=FakeEdgarClient(),
            output_dir=str(tmp_path / "reports"),
        )

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps_with_edgar
        )

        assert result.status == RunStatus.SUCCESS
        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, detail FROM run_steps WHERE run_id = ? AND step = '2_fundamentals'",
                [str(result.run_id)],
            ).fetchone()
        assert row[0] == "success"
        assert "MSFT" in row[1]

    def test_edgar_client_total_failure_is_fatal(
        self, settings, market_store, state_store
    ):
        class AlwaysFailingEdgarClient:
            def fetch_fundamentals(self, symbol, as_of):
                del symbol, as_of
                msg = "EDGAR unreachable"
                raise RuntimeError(msg)

            def fetch_filing_texts(self, symbol, form_types, *, as_of):
                del symbol, form_types, as_of
                return []

        universe = (_member("AAPL"),)
        deps_with_edgar = DailyDependencies(
            data_provider=FakeDataProvider(_bars_for(["AAPL"], AS_OF)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=FakeClock(),
            edgar_client=AlwaysFailingEdgarClient(),
        )

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps_with_edgar
        )

        assert result.status == RunStatus.FAILED


class TestUnexpectedStepException:
    def test_unexpected_exception_is_recorded_as_a_failed_step_not_a_crash(
        self, deps, state_store
    ):
        class ExplodingDataProvider:
            def get_daily_bars(self, symbols, start, end):
                del symbols, start, end
                msg = "boom"
                raise RuntimeError(msg)

            def get_latest_bars(self, symbols, as_of):
                del symbols, as_of
                msg = "boom"
                raise RuntimeError(msg)

        exploding_deps = DailyDependencies(
            data_provider=ExplodingDataProvider(),
            market_store=deps.market_store,
            state_store=deps.state_store,
            settings=deps.settings,
            universe=deps.universe,
            strategies_config=deps.strategies_config,
            clock=deps.clock,
        )

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), exploding_deps
        )

        assert result.status == RunStatus.FAILED
        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT status, detail FROM run_steps WHERE run_id = ? AND step = '1_prices'",
                [str(result.run_id)],
            ).fetchone()
        assert row[0] == "failed"
        assert "boom" in row[1]
