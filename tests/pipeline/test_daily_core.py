"""Tests for pipeline/daily.py's fatal steps 1-4 (FR-12).

Fail-soft steps 5-9 are covered by tests/pipeline/test_failsoft.py and
tests/test_e2e_smoke.py.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pandas as pd
import pytest

from swing_copilot.data.base import BarFetchResult, FetchFailure
from swing_copilot.models import DailyRunOptions, Position, RunMode, RunStatus
from swing_copilot.pipeline.daily import (
    DailyDependencies,
    _config_hash,
    run_daily,
)
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - imported for its @register_filter side effect
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - imported for its @register_signal side effect
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import FundamentalsRecord, MarketStore
from swing_copilot.storage.paper_records import TradeDecisionRecord
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


class FakeMonotonic:
    """Returns each value in order, then repeats the last one forever.

    Mirrors a real monotonic clock: once "time" has passed a fixed point
    (e.g. the NFR-03 deadline), it never goes back before it.
    """

    def __init__(self, *values: float):
        self._values = list(values)
        self._index = 0

    def __call__(self) -> float:
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


def _bars_for(
    symbols: list[str], as_of: date, days: int = 210, volume: int = 2_000_000
) -> pd.DataFrame:
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
                    "volume": volume,
                }
            )
    return pd.DataFrame(rows)


def _healthy_fundamentals(symbol: str) -> list[FundamentalsRecord]:
    """Fundamentals that pass `ProfitablePositiveFCFEquityFilter` cleanly.

    Needed even when a strategy's `filters_all` doesn't include
    `profitable_positive_fcf_equity`: the P1-02 rejection classifier mirrors
    that filter's threshold logic unconditionally (screening/
    rejection_classifier.py), so a symbol with no fundamentals on file would
    otherwise be misclassified as DATA_INSUFFICIENT_HISTORY instead of the
    reason this fixture actually targets.
    """
    quarter_ends = [
        date(2026, 3, 31),
        date(2026, 6, 30),
        date(2026, 9, 30),
        date(2026, 12, 31),
    ]
    filed_ats = [
        datetime(2026, 4, 15, tzinfo=UTC),
        datetime(2026, 7, 15, tzinfo=UTC),
        datetime(2026, 10, 15, tzinfo=UTC),
        datetime(2027, 1, 15, tzinfo=UTC),
    ]
    return [
        FundamentalsRecord(
            accession_no=f"acc-{symbol}-{i}",
            symbol=symbol,
            form="10-Q",
            fiscal_period_end=quarter_ends[i],
            filed_at=filed_ats[i],
            revenue=100.0,
            net_income=10.0,
            fcf=10.0,
            equity=60.0,
            assets=100.0,
            shares=1_000_000.0,
            source_url="https://www.sec.gov/example",
            fetched_at=datetime(2027, 1, 20, tzinfo=UTC),
        )
        for i in range(4)
    ]


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
        }
    }
}

# TestPastDecisionsThreading: a second strategy key, to distinguish "the
# correct strategy_key threaded through" from "it happened to match the
# only strategy that exists".
TWO_STRATEGIES_CONFIG = {
    "strategies": {
        "default": {
            "filters_all": ["volume_min"],
            "signals_all": ["trend_sma"],
            "candidate_limit": 10,
        },
        "growth_v2": {
            "filters_all": ["volume_min"],
            "signals_all": ["trend_sma"],
            "candidate_limit": 10,
        },
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
    def test_completes_all_eight_steps_successfully(self, deps, state_store):
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
            "6_analysis_export",
            "7_notify",
            "8_output",
            "mae_mfe",
            "postmortem",
            "retro_collect",
            "retro_evaluate",
            "track_update",
        ]
        # 1/3/4/8 succeed outright; 2/5/6/7 are deliberate skips (no
        # optional clients configured); postmortem (P2-11), the retro
        # collect/evaluate steps (P8-30) and verdict tracking succeed with
        # nothing to look back at yet — none of these are failures.
        assert all(status in {"success", "skipped"} for _step, status in steps)

        bars = deps.market_store.read_bars(
            ["AAPL", "MSFT"], AS_OF - timedelta(days=400), AS_OF, AS_OF
        )
        assert set(bars["fetched_at"]) == {pd.Timestamp("2027-03-01T12:00:00Z")}

    def test_default_null_progress_keeps_run_daily_stderr_silent(self, deps, capsys):
        run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert capsys.readouterr().err == ""

    def test_persists_insufficient_regime_snapshot_without_stopping_run(
        self, deps, state_store
    ):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT gate_verdict, dd_level, data_quality FROM regime_snapshots WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchone()
        assert result.status is RunStatus.SUCCESS
        assert row == ("UNKNOWN", "UNKNOWN", "INSUFFICIENT")

    def test_persists_conservative_exposure_for_insufficient_regime(
        self, deps, state_store
    ):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT verdict, data_quality FROM exposure_decisions WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchone()
        assert row == ("CASH_PRIORITY", "INSUFFICIENT")


class TestRunFingerprintAndMetadata:
    def test_fingerprint_is_canonical_and_covers_settings_strategy_and_key(self, deps):
        canonical = _config_hash(deps.settings, STRATEGIES_CONFIG, "default")
        reordered = {
            "strategies": {
                "default": {
                    "candidate_limit": 10,
                    "signals_all": ["trend_sma"],
                    "filters_all": ["volume_min"],
                }
            }
        }
        changed_strategy = {
            "strategies": {
                "default": {
                    "filters_all": ["volume_min"],
                    "signals_all": ["trend_sma"],
                    "candidate_limit": 9,
                }
            }
        }
        changed_risk = deps.settings.risk.model_copy(
            update={"max_trade_risk_pct": 0.02}
        )
        changed_settings = deps.settings.model_copy(update={"risk": changed_risk})

        assert len(canonical) == 64
        assert canonical == _config_hash(deps.settings, reordered, "default")
        assert canonical != _config_hash(deps.settings, changed_strategy, "default")
        assert canonical != _config_hash(changed_settings, STRATEGIES_CONFIG, "default")
        assert canonical != _config_hash(
            deps.settings, TWO_STRATEGIES_CONFIG, "growth_v2"
        )

    def test_run_persists_reconstructable_metadata(self, deps, state_store):
        tracked_deps = replace(
            deps,
            universe_snapshot_date=date(2027, 2, 20),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), tracked_deps)

        with state_store._database.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT config_hash, metadata_json FROM runs WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchone()
        metadata = json.loads(row[1])
        assert row[0] == _config_hash(
            tracked_deps.settings,
            tracked_deps.strategies_config,
            tracked_deps.strategy_key,
        )
        assert metadata["schema_version"] == "run-metadata-v1"
        assert metadata["app_version"]
        assert metadata["provider"] == {
            "name": "yfinance",
            "data_tier": "prototype",
        }
        assert metadata["universe_snapshot"]["snapshot_date"] == "2027-02-20"
        assert len(metadata["universe_snapshot"]["identity"]) == 64


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
        # 8 pre-existing steps + local postmortem, MAE/MFE, the two retro
        # (collect/evaluate) steps, and verdict tracking.
        assert first_steps == (13,)
        assert second_steps == (13,)


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

    def test_limit_excludes_never_fetched_symbols_from_screening_rejections(
        self, deps, state_store
    ):
        # P1-02 regression: `deps.universe` has AAPL+MSFT, but `limit=1`
        # narrows this run's actual fetch scope to AAPL only. MSFT must not
        # appear in `screening_rejections` -- it was never fetched this run,
        # so classifying it at all would be a spurious rejection, not a
        # genuine screening outcome.
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True, limit=1), deps)

        assert result.status == RunStatus.SUCCESS
        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol FROM screening_rejections WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchall()
        assert rows == []

    def test_zero_limit_keeps_open_holdings_in_current_run_fetch_scope(
        self, deps, state_store
    ):
        class RecordingDataProvider(FakeDataProvider):
            def __init__(self, bars):
                super().__init__(bars)
                self.requested_symbols: list[tuple[str, ...]] = []

            def get_daily_bars(self, symbols, start, end):
                self.requested_symbols.append(tuple(symbols))
                return super().get_daily_bars(symbols, start, end)

        state_store.upsert_position(
            Position(
                position_id=uuid4(),
                symbol="AAPL",
                is_paper=True,
                entry_date=AS_OF - timedelta(days=5),
                entry_price=100.0,
                shares=10,
                status="open",
                stop_price=95.0,
            )
        )
        provider = RecordingDataProvider(_bars_for(["AAPL", "MSFT"], AS_OF))
        zero_limit_deps = replace(deps, data_provider=provider)

        result = run_daily(DailyRunOptions(is_dry_run=True, limit=0), zero_limit_deps)

        assert result.status == RunStatus.SUCCESS
        assert "AAPL" in provider.requested_symbols[0]
        assert "MSFT" not in provider.requested_symbols[0]

    @pytest.mark.parametrize(
        "entry_offset",
        [
            pytest.param(-1, id="immediately-before"),
            pytest.param(0, id="exactly-at"),
            pytest.param(1, id="immediately-after"),
        ],
    )
    def test_historical_run_excludes_current_positions_at_all_boundaries(
        self, deps, state_store, entry_offset
    ):
        class RecordingDataProvider(FakeDataProvider):
            def __init__(self, bars):
                super().__init__(bars)
                self.requested_symbols: list[tuple[str, ...]] = []

            def get_daily_bars(self, symbols, start, end):
                self.requested_symbols.append(tuple(symbols))
                return super().get_daily_bars(symbols, start, end)

        state_store.upsert_position(
            Position(
                position_id=uuid4(),
                symbol="AAPL",
                is_paper=True,
                entry_date=AS_OF + timedelta(days=entry_offset),
                entry_price=100.0,
                shares=10,
                status="open",
                stop_price=95.0,
            )
        )
        provider = RecordingDataProvider(_bars_for(["AAPL", "MSFT"], AS_OF))
        historical_deps = replace(deps, data_provider=provider)

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True, limit=0),
            historical_deps,
        )

        assert result.status == RunStatus.SUCCESS
        assert "AAPL" not in provider.requested_symbols[0]
        assert result.brief is not None
        assert any(
            "historical replay does not use current position state" in notice
            for notice in result.brief.notices
        )


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

            def fetch_filing_texts(
                self, symbol, form_types, *, as_of, since=None, limit=None
            ):
                del symbol, form_types, as_of, since, limit
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

            def fetch_filing_texts(
                self, symbol, form_types, *, as_of, since=None, limit=None
            ):
                del symbol, form_types, as_of, since, limit
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


class TestFundamentalsSameDaySkip:
    def test_rerun_with_past_as_of_still_skips_same_day_refetch(
        self, settings, market_store, state_store, tmp_path
    ):
        """Regression for P6-25.

        `has_fundamentals_fetched_on` must be checked against the injected
        `Clock`'s wall-clock date, not `as_of`: `fetched_at` is a real fetch
        timestamp, so a same-day rerun with a *past* `--as-of` must still
        skip the redundant EDGAR network fetch. Before this fix, the check
        compared `fetched_at`'s date to `as_of` directly, which never
        matched once `as_of` was in the past, so every rerun refetched.
        """
        past_as_of = AS_OF - timedelta(days=5)

        class TodayFixedClock:
            def today(self):
                return AS_OF  # "real" wall-clock today, independent of as_of

            def now(self):
                return datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC)

        class CountingEdgarClient:
            def __init__(self):
                self.calls = 0

            def fetch_fundamentals(self, symbol, as_of):
                del as_of
                self.calls += 1
                return [
                    FundamentalsRecord(
                        accession_no=f"acc-{symbol}-{self.calls}",
                        symbol=symbol,
                        form="10-Q",
                        fiscal_period_end=past_as_of,
                        filed_at=datetime.combine(
                            past_as_of, datetime.min.time(), tzinfo=UTC
                        ),
                        revenue=1.0,
                        net_income=1.0,
                        fcf=1.0,
                        equity=1.0,
                        assets=2.0,
                        shares=1.0,
                        source_url="https://www.sec.gov/example",
                        # Stamped with "today" (SystemClock, in production),
                        # not `past_as_of` -- matching the real EdgarClient.
                        fetched_at=datetime.combine(
                            AS_OF, datetime.min.time(), tzinfo=UTC
                        ),
                    )
                ]

            def fetch_filing_texts(
                self, symbol, form_types, *, as_of, since=None, limit=None
            ):
                del symbol, form_types, as_of, since, limit
                return []

        universe = (_member("AAPL"),)
        edgar_client = CountingEdgarClient()
        deps_with_edgar = DailyDependencies(
            data_provider=FakeDataProvider(_bars_for(["AAPL"], past_as_of)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=TodayFixedClock(),
            edgar_client=edgar_client,
            output_dir=str(tmp_path / "reports"),
        )

        first = run_daily(
            DailyRunOptions(as_of=past_as_of, is_dry_run=True), deps_with_edgar
        )
        second = run_daily(
            DailyRunOptions(as_of=past_as_of, is_dry_run=True), deps_with_edgar
        )

        assert first.status == RunStatus.SUCCESS
        assert second.status == RunStatus.SUCCESS
        assert edgar_client.calls == 1


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


class TestTimeoutBudget:
    """NFR-03 run-timeout budget (`deps.monotonic`/`settings.schedule.timeout_minutes`)."""

    def test_no_breach_completes_normally(self, deps):
        deps_with_monotonic = replace(deps, monotonic=FakeMonotonic(0.0))

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps_with_monotonic
        )

        assert result.status == RunStatus.SUCCESS

    def test_mid_fundamentals_breach_degrades_but_the_run_still_completes(
        self, settings, market_store, state_store, tmp_path
    ):
        object.__setattr__(settings.schedule, "timeout_minutes", 1)  # 60s budget

        class SlowEdgarClient:
            def fetch_fundamentals(self, symbol, as_of):
                del as_of
                return [
                    FundamentalsRecord(
                        accession_no=f"acc-{symbol}",
                        symbol=symbol,
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

            def fetch_filing_texts(
                self, symbol, form_types, *, as_of, since=None, limit=None
            ):
                del symbol, form_types, as_of, since, limit
                return []

        universe = (_member("AAPL"), _member("MSFT"))
        # run_started_at=0.0 -> deadline=60.0; index0(AAPL) check=10.0 (ok,
        # fetched); index1(MSFT) check=70.0 (breach, stops before fetching).
        deps_with_edgar = DailyDependencies(
            data_provider=FakeDataProvider(_bars_for(["AAPL", "MSFT"], AS_OF)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=universe,
            strategies_config=STRATEGIES_CONFIG,
            clock=FakeClock(),
            edgar_client=SlowEdgarClient(),
            monotonic=FakeMonotonic(0.0, 10.0, 70.0),
            output_dir=str(tmp_path / "reports"),
        )

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps_with_edgar
        )

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()

        with state_store._database.connect() as conn:  # noqa: SLF001
            fundamentals_row = conn.execute(
                "SELECT status, detail FROM run_steps "
                "WHERE run_id = ? AND step = '2_fundamentals'",
                [str(result.run_id)],
            ).fetchone()
            text_row = conn.execute(
                "SELECT status, detail FROM run_steps "
                "WHERE run_id = ? AND step = '5_text'",
                [str(result.run_id)],
            ).fetchone()
            output_row = conn.execute(
                "SELECT status FROM run_steps WHERE run_id = ? AND step = '8_output'",
                [str(result.run_id)],
            ).fetchone()

        # Not fatal: fundamentals stopped early with a partial result, not a
        # failure -- one symbol was already fetched before the budget broke.
        assert fundamentals_row[0] == "success"
        assert "time budget exceeded after 1/2 symbols" in fundamentals_row[1]
        # Once the budget is exceeded it stays exceeded, so the next
        # network-bound step is skipped too -- this is what degrades the run.
        assert text_row[0] == "skipped"
        assert "time budget exceeded" in text_row[1]
        # Cheap/local steps still ran and produced a report.
        assert output_row[0] == "success"

    def test_pre_step_breach_skips_network_steps_but_the_run_still_completes(
        self, deps, state_store
    ):
        object.__setattr__(deps.settings.schedule, "timeout_minutes", 1)  # 60s budget
        # run_started_at=0.0 -> deadline=60.0; by the time steps 5/6/7 check,
        # "elapsed" is already far past the budget, even though nothing in
        # the fatal steps (1-4) itself was individually slow.
        deps_late = replace(deps, monotonic=FakeMonotonic(0.0, 999_999.0))

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps_late)

        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0
        assert result.report_path is not None
        assert result.report_path.is_file()

        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = dict(
                conn.execute(
                    "SELECT step, status FROM run_steps WHERE run_id = ?",
                    [str(result.run_id)],
                ).fetchall()
            )
        assert rows["5_text"] == "skipped"
        assert rows["6_analysis_export"] == "skipped"
        assert rows["7_notify"] == "skipped"
        assert rows["8_output"] == "success"


class TestScreeningRejections:
    """P1-02: `screening_rejections` end-to-end through the daily pipeline."""

    def test_liquidity_rejection_is_recorded_and_reported(
        self, settings, market_store, state_store, tmp_path
    ):
        universe = (_member("AAPL"), _member("MSFT"), _member("LOWVOL"))
        bars = pd.concat(
            [
                _bars_for(["AAPL", "MSFT"], AS_OF),
                _bars_for(["LOWVOL"], AS_OF, volume=100),
            ]
        )
        market_store.upsert_fundamentals(
            [
                *_healthy_fundamentals("AAPL"),
                *_healthy_fundamentals("MSFT"),
                *_healthy_fundamentals("LOWVOL"),
            ]
        )
        deps = DailyDependencies(
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

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        with state_store._database.connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT symbol, stage, reason_code FROM screening_rejections "
                "WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchall()
        assert rows == [("LOWVOL", "fundamental_filter", "FILTER_LOW_LIQUIDITY")]

        assert result.report_path is not None
        report_text = result.report_path.read_text(encoding="utf-8")
        assert "## 落選サマリ" in report_text
        assert "FILTER_LOW_LIQUIDITY" in report_text
        assert "| 1 |" in report_text

    def test_all_pass_reports_zero_rejections(self, deps, state_store):
        # REQ-010 boundary: both AAPL/MSFT pass, so screening_rejections has
        # zero rows and the report's summary renders without error.
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute(
                "SELECT count(*) FROM screening_rejections WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchone()
        assert count == (0,)

        assert result.report_path is not None
        report_text = result.report_path.read_text(encoding="utf-8")
        assert "## 落選サマリ" in report_text
        assert "該当なし(0件)" in report_text


class TestPastDecisionsThreading:
    """P1-05 REQ-008 strategy_key threading test.

    `deps.strategy_key` must reach `DailyBriefContext` and scope each
    candidate's `past_decisions` -- not merely coincidentally match the
    shared "default" value.
    """

    def test_strategy_key_threads_into_past_decisions_end_to_end(
        self, deps, state_store
    ):
        custom_deps = replace(
            deps,
            strategy_key="growth_v2",
            strategies_config=TWO_STRATEGIES_CONFIG,
        )
        # A decision recorded under a different strategy_key than this run's
        # own must not surface in `past_decisions`.
        wrong_strategy_run = state_store.start_run(
            AS_OF - timedelta(days=5), RunMode.LIVE, "cfg"
        )
        state_store.record_trade_decision(
            TradeDecisionRecord(
                run_id=wrong_strategy_run,
                symbol="AAPL",
                strategy_key="default",
                position_id=None,
                decision="followed",
                reason_memo="wrong strategy decision",
                virtual_fill_price=100.0,
            )
        )
        right_strategy_run = state_store.start_run(
            AS_OF - timedelta(days=3), RunMode.LIVE, "cfg"
        )
        state_store.record_trade_decision(
            TradeDecisionRecord(
                run_id=right_strategy_run,
                symbol="AAPL",
                strategy_key="growth_v2",
                position_id=None,
                decision="ignored",
                reason_memo="correct strategy decision",
                virtual_fill_price=None,
            )
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), custom_deps)

        assert result.status == RunStatus.SUCCESS
        assert result.brief is not None
        aapl = next(c for c in result.brief.candidates if c.symbol == "AAPL")
        assert len(aapl.past_decisions) == 1
        assert aapl.past_decisions[0].decision == "ignored"
        assert aapl.past_decisions[0].reason_memo == "correct strategy decision"

        assert result.report_path is not None
        report_text = result.report_path.read_text(encoding="utf-8")
        assert "### 過去判断" in report_text
        assert "correct strategy decision" in report_text
        assert "wrong strategy decision" not in report_text
