"""Tests for pipeline/daily.py's fatal steps 1-4 (FR-12).

Fail-soft steps 5-9 are covered by tests/pipeline/test_failsoft.py and
tests/test_e2e_smoke.py.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from uuid import uuid4

import pandas as pd
import pytest

from swing_copilot.config import config_snapshot_hash, config_snapshot_sections
from swing_copilot.data.base import BarFetchResult, FetchFailure
from swing_copilot.exceptions import PreflightAbort
from swing_copilot.models import DailyRunOptions, Position, RunMode, RunStatus
from swing_copilot.pipeline.daily import (
    _FUNDAMENTALS_EMPTY_BACKOFF_DAYS,
    _FUNDAMENTALS_REFRESH_INTERVAL_DAYS,
    DailyDependencies,
    _config_hash,
    _FundamentalsFreshness,
    _refresh_interval_days,
    _select_symbols,
    run_daily,
)
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - imported for its @register_filter side effect
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - imported for its @register_signal side effect
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import (
    FundamentalsFetchStamp,
    FundamentalsFetchState,
    FundamentalsRecord,
    MarketStore,
)
from swing_copilot.storage.paper_records import TradeDecisionRecord
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.base import TextItem
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


# Issue #219 fixtures: a holding that sorts *after* every universe member and
# is outside the universe, so lexicographic order and held-first order
# disagree about which symbol the NFR-03 budget should reach first.
_HELD_SYMBOL = "ZHELD"


class _RecordingEdgarClient:
    """EDGAR fake that records the order `_run_step_fundamentals` fetches in."""

    def __init__(self):
        self.fetched: list[str] = []

    def fetch_fundamentals(self, symbol, as_of):
        del as_of
        self.fetched.append(symbol)
        return _healthy_fundamentals(symbol)

    def fetch_filing_texts(self, symbol, form_types, *, as_of, since=None, limit=None):
        del symbol, form_types, as_of, since, limit
        return []


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

    def test_run_records_what_its_config_hash_stood_for(self, deps, state_store):
        """Issue #189: the hash alone is one-way, so the values are written too."""
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        (version,) = state_store.get_config_versions()
        assert (
            version.config_hash
            == state_store.get_run_config_hashes(AS_OF)[result.run_id]
        )
        assert version.sections == config_snapshot_sections(deps.settings)
        assert version.snapshot_hash == config_snapshot_hash(version.sections)

    def test_the_ledger_keeps_the_first_run_date_a_configuration_was_seen_on(
        self, deps, state_store
    ):
        run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True, allow_same_day_rerun=True),
            deps,
        )

        (version,) = state_store.get_config_versions()
        assert version.first_seen_run_date == AS_OF

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
        second = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True, allow_same_day_rerun=True),
            deps,
        )

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
        second = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True, allow_same_day_rerun=True),
            deps,
        )

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
            output_dir=str(tmp_path),
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
        self, settings, market_store, state_store, tmp_path
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
            output_dir=str(tmp_path),
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
            output_dir=str(tmp_path),
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
    def _sectored_universe(self, sizes: dict[str, int]) -> tuple[UniverseMember, ...]:
        """One member per slot, named so alphabetical order is fully predictable."""
        members: list[UniverseMember] = []
        for sector, size in sorted(sizes.items()):
            offset = len(members)
            members += [
                UniverseMember(
                    symbol=f"S{offset + index:03d}",
                    company_name=f"S{offset + index:03d}",
                    gics_sector=sector,
                    source_symbol=f"S{offset + index:03d}",
                )
                for index in range(size)
            ]
        return tuple(members)

    def test_limit_restricts_the_universe_to_a_sample(self, deps):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True, limit=1), deps)
        assert result.status == RunStatus.SUCCESS

    def test_limit_is_not_the_alphabetically_first_n_symbols(self):
        # Issue #205: `universe[:limit]` over a `ORDER BY symbol` universe made
        # `--limit 20` mean "the 20 tickers starting with A", which changes what
        # the universe-relative RS percentile (condition 7) measures.
        universe = self._sectored_universe({"Energy": 100, "Utilities": 100})

        symbols = _select_symbols(universe, set(), 20)

        assert symbols != [f"S{index:03d}" for index in range(20)]
        assert len(symbols) == 20
        assert max(symbols) > "S150"

    def test_limit_selects_the_same_symbols_on_every_run(self):
        universe = self._sectored_universe({"Energy": 40, "Utilities": 60})

        first = _select_symbols(universe, set(), 25)
        again = _select_symbols(tuple(reversed(universe)), set(), 25)

        # Deterministic, and pinned so a reordered universe or another machine
        # cannot silently redraw the sample.
        assert first == again
        assert first[:3] == ["S003", "S011", "S013"]

    def test_held_symbols_are_screened_regardless_of_the_limit(self):
        universe = self._sectored_universe({"Energy": 100, "Utilities": 100})

        symbols = _select_symbols(universe, {"HELD"}, 20)

        assert "HELD" in symbols
        assert symbols == sorted(symbols)

    def test_zero_limit_selects_only_held_symbols(self):
        universe = self._sectored_universe({"Energy": 100, "Utilities": 100})

        assert _select_symbols(universe, {"HELD"}, 0) == ["HELD"]
        assert _select_symbols(universe, set(), 0) == []

    def test_limit_at_or_above_the_universe_size_keeps_every_symbol(self):
        universe = self._sectored_universe({"Energy": 3})

        assert _select_symbols(universe, set(), 5) == ["S000", "S001", "S002"]

    def test_no_limit_selects_the_whole_universe(self):
        universe = self._sectored_universe({"Energy": 3, "Utilities": 2})

        symbols = _select_symbols(universe, set(), None)

        assert symbols == [member.symbol for member in universe]

    def test_no_limit_keeps_holdings_that_left_the_universe(self):
        # Issue #212: production never passes `--limit`, so this branch is the
        # one that runs daily. A holding dropped from the S&P 500 snapshot must
        # still reach the fetch set, or its exit checks run on stale bars.
        universe = self._sectored_universe({"Energy": 3})

        symbols = _select_symbols(universe, {"S001", "OLDCO"}, None)

        assert symbols == ["OLDCO", "S000", "S001", "S002"]

    def test_no_limit_returns_alphabetical_order_for_any_universe_order(self):
        universe = self._sectored_universe({"Energy": 3})

        assert _select_symbols(tuple(reversed(universe)), set(), None) == [
            "S000",
            "S001",
            "S002",
        ]

    def test_holdings_do_not_change_which_symbols_the_limit_samples(self):
        # The `--limit` branch already unioned holdings; #212 must not perturb
        # the sample it draws, only add the holdings on top of it.
        universe = self._sectored_universe({"Energy": 40, "Utilities": 60})

        without_holdings = _select_symbols(universe, set(), 25)
        with_holdings = _select_symbols(universe, {"OLDCO"}, 25)

        assert without_holdings[:3] == ["S003", "S011", "S013"]
        assert with_holdings == sorted([*without_holdings, "OLDCO"])

    def test_limit_excludes_never_fetched_symbols_from_screening_rejections(
        self, deps, state_store
    ):
        # P1-02 regression: `deps.universe` has AAPL+MSFT, but `limit=1`
        # narrows this run's actual fetch scope to one of them. The other must
        # not appear in `screening_rejections` -- it was never fetched this
        # run, so classifying it at all would be a spurious rejection, not a
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

    def test_full_universe_run_fetches_holdings_dropped_from_the_universe(
        self, deps, state_store
    ):
        # Issue #212: the production 18:30 routine never passes `--limit`, and
        # `_select_symbols()` is the only input to the daily price fetch. A
        # position whose symbol left the S&P 500 snapshot must still get today's
        # bar, otherwise its trailing stop / max-hold checks read stale prices.
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
                symbol="OLDCO",
                is_paper=True,
                entry_date=AS_OF - timedelta(days=5),
                entry_price=100.0,
                shares=10,
                status="open",
                stop_price=95.0,
            )
        )
        provider = RecordingDataProvider(_bars_for(["AAPL", "MSFT", "OLDCO"], AS_OF))
        full_universe_deps = replace(deps, data_provider=provider)

        result = run_daily(DailyRunOptions(is_dry_run=True), full_universe_deps)

        assert result.status == RunStatus.SUCCESS
        assert "OLDCO" in provider.requested_symbols[0]
        assert {"AAPL", "MSFT"} <= set(provider.requested_symbols[0])

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


class TestAccountEquityUnsetWarning:
    """P8-117 REQ-005/REQ-006: the `## Warnings` line, independent of preflight."""

    def test_unset_equity_adds_a_report_warning(self, deps):
        # The shipped `config/settings.yaml` now carries a real equity figure,
        # so the unset case is constructed here rather than read from the file.
        unset_deps = replace(
            deps,
            settings=deps.settings.model_copy(
                update={
                    "risk": deps.settings.risk.model_copy(
                        update={"account_equity_usd": None}
                    )
                }
            ),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), unset_deps)

        assert result.brief is not None
        assert any("account_equity_usd" in notice for notice in result.brief.notices)

    def test_set_equity_adds_no_warning(self, deps):
        equity_deps = replace(
            deps,
            settings=deps.settings.model_copy(
                update={
                    "risk": deps.settings.risk.model_copy(
                        update={"account_equity_usd": 100_000.0}
                    )
                }
            ),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), equity_deps)

        assert result.brief is not None
        assert not any(
            "account_equity_usd" in notice for notice in result.brief.notices
        )


#: `deps`'s bars run through `_bars_for(["AAPL", "MSFT"], AS_OF)`, whose last
#: generated session is `AS_OF - 1 day` (see `TestAsOfDefaulting`), so a live
#: (no explicit `--as-of`) run resolves `run_date` to this date, not `AS_OF`.
_LIVE_RUN_DATE = AS_OF - timedelta(days=1)


class TestSameDayRerunGuard:
    """P8-118: abort before start_run when run_date already has a success run."""

    def test_existing_success_run_aborts_before_start_run(self, deps, state_store):
        existing_id = uuid4()
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
                "started_at, report_path) VALUES (?, ?, 'live', 'cfg', 'success', "
                "?, ?)",
                [
                    str(existing_id),
                    _LIVE_RUN_DATE,
                    datetime(2027, 2, 28, 15, 5, tzinfo=UTC),
                    "reports/2027-02-28/x.md",
                ],
            )

        with pytest.raises(PreflightAbort) as exc_info:
            run_daily(DailyRunOptions(is_dry_run=True), deps)

        assert exc_info.value.reason == "same_day_rerun"
        message = str(exc_info.value)
        assert str(existing_id) in message
        assert _LIVE_RUN_DATE.isoformat() in message
        assert "reports/2027-02-28/x.md" in message
        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute("SELECT count(*) FROM runs").fetchone()
        assert count == (1,)

    def test_allow_same_day_rerun_bypasses_the_guard(self, deps, state_store):
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
                "started_at) VALUES (?, ?, 'live', 'cfg', 'success', ?)",
                [
                    str(uuid4()),
                    _LIVE_RUN_DATE,
                    datetime(2027, 2, 28, 15, 5, tzinfo=UTC),
                ],
            )

        result = run_daily(
            DailyRunOptions(is_dry_run=True, allow_same_day_rerun=True), deps
        )

        assert result.status == RunStatus.SUCCESS

    def test_only_failed_or_running_existing_runs_do_not_abort(self, deps, state_store):
        with state_store._database.connect() as conn:  # noqa: SLF001
            for status in ("failed", "running"):
                conn.execute(
                    "INSERT INTO runs (run_id, run_date, mode, config_hash, "
                    "status, started_at) VALUES (?, ?, 'live', 'cfg', ?, ?)",
                    [
                        str(uuid4()),
                        _LIVE_RUN_DATE,
                        status,
                        datetime(2027, 2, 28, 15, 5, tzinfo=UTC),
                    ],
                )

        result = run_daily(DailyRunOptions(is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS

    def test_historical_as_of_applies_the_same_guard(self, deps, state_store):
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
                "started_at) VALUES (?, ?, 'live', 'cfg', 'success', ?)",
                [str(uuid4()), AS_OF, datetime(2027, 3, 1, 15, 5, tzinfo=UTC)],
            )

        with pytest.raises(PreflightAbort):
            run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

    def test_a_market_holiday_evening_run_checks_the_resolved_run_date(
        self, deps, state_store
    ):
        # fetch_cutoff (deps.clock.today() == AS_OF) has no bar that day; the
        # latest prefetched bar resolves run_date to an earlier trading day
        # instead. _bars_for(symbols, X)'s last session is X - 1 day, so
        # asking for bars "as of" holiday_run_date + 1 day lands the latest
        # bar exactly on holiday_run_date.
        holiday_run_date = AS_OF - timedelta(days=5)
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
                "started_at) VALUES (?, ?, 'live', 'cfg', 'success', ?)",
                [
                    str(uuid4()),
                    holiday_run_date,
                    datetime(2027, 2, 24, 15, 5, tzinfo=UTC),
                ],
            )
        holiday_provider = FakeDataProvider(
            _bars_for(["AAPL", "MSFT"], holiday_run_date + timedelta(days=1))
        )
        holiday_deps = replace(deps, data_provider=holiday_provider)

        with pytest.raises(PreflightAbort) as exc_info:
            run_daily(DailyRunOptions(is_dry_run=True), holiday_deps)

        assert holiday_run_date.isoformat() in str(exc_info.value)


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

    def test_edgar_client_total_failure_reports_instead_of_failing_the_run(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review finding 1 (HIGH) reversed this expectation.

        A total EDGAR failure used to be fatal, which was defensible while
        the step always attempted the whole universe. Under the incremental
        rule a typical day attempts one or two symbols, so the same condition
        is reached by a single permanently broken ticker -- and, because
        failures are never stamped, it would recur every single day. The run
        now completes and reports the outage in the step detail instead.
        """

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
            output_dir=str(tmp_path / "reports"),
        )

        result = run_daily(
            DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps_with_edgar
        )

        assert result.status == RunStatus.SUCCESS
        assert result.exit_code == 0
        assert "EDGAR refreshed nothing" in _fundamentals_step_detail(
            state_store, result.run_id
        )


class TestFundamentalsSameDaySkip:
    def test_rerun_with_past_as_of_still_skips_same_day_refetch(
        self, settings, market_store, state_store, tmp_path
    ):
        """Regression for P6-25.

        Fetch freshness must be measured against the injected `Clock`'s
        wall-clock date, not `as_of`: the fetch log holds real fetch
        timestamps, so a same-day rerun with a *past* `--as-of` must still
        skip the redundant EDGAR network fetch. Before this fix, the check
        compared the recorded fetch date to `as_of` directly, which never
        matched once `as_of` was in the past, so every rerun refetched.
        (Issue #258 moved the bookkeeping from `fundamentals.fetched_at` to
        `fundamentals_fetch_log`; this P6-25 invariant is unchanged, and the
        same-day skip is now the incremental rule's "0 elapsed days" case.)
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
            DailyRunOptions(
                as_of=past_as_of, is_dry_run=True, allow_same_day_rerun=True
            ),
            deps_with_edgar,
        )

        assert first.status == RunStatus.SUCCESS
        assert second.status == RunStatus.SUCCESS
        assert edgar_client.calls == 1


_UNSET = object()


class TestFundamentalsFreshnessRule:
    """Issue #258: `docs/03_basic_design.md` 8.3's weekly/incremental rule.

    Pinned as a unit matrix because the whole point of the change is *which
    symbols are not fetched*, and every boundary here (never fetched, exactly
    the interval, a filing dated exactly on the last fetch day) is a place a
    plausible off-by-one would silently either refetch the universe daily or
    sit on stale fundamentals for a week after a 10-Q lands.
    """

    TODAY = date(2027, 3, 8)

    def _freshness(  # noqa: PLR0913 - one keyword per input the rule reads
        self,
        *,
        last_fetched_on=None,
        fetched_through_on=_UNSET,
        latest_filing_on=None,
        latest_ingested_on=None,
        consecutive_empty=0,
        as_of=None,
    ):
        """Build a freshness view over one symbol.

        `fetched_through_on` defaults to `last_fetched_on`, which is what a
        normal (non-replay) run records; pass it explicitly to model a replay
        or a row written before the column existed. `as_of` defaults to
        `TODAY`; pass it to model the ordinary evening run, whose evaluation
        date trails the wall clock.
        """
        if fetched_through_on is _UNSET:
            fetched_through_on = last_fetched_on
        return _FundamentalsFreshness(
            today=self.TODAY,
            as_of=self.TODAY if as_of is None else as_of,
            fetch_state=(
                {}
                if last_fetched_on is None
                else {
                    "AAPL": FundamentalsFetchState(
                        last_fetched_on=last_fetched_on,
                        fetched_through_on=fetched_through_on,
                        consecutive_empty=consecutive_empty,
                    )
                }
            ),
            latest_filing_on=(
                {} if latest_filing_on is None else {"AAPL": latest_filing_on}
            ),
            latest_ingested_on=(
                {} if latest_ingested_on is None else {"AAPL": latest_ingested_on}
            ),
        )

    def test_a_never_fetched_symbol_is_always_fetched(self):
        assert self._freshness().needs_fetch("AAPL")

    def test_exactly_the_refresh_interval_fetches(self):
        """The boundary is inclusive on the fetch side: 7 days old is due."""
        last = self.TODAY - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS)

        assert self._freshness(last_fetched_on=last).needs_fetch("AAPL")

    def test_one_day_short_of_the_interval_skips(self):
        last = self.TODAY - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS - 1)

        assert not self._freshness(last_fetched_on=last).needs_fetch("AAPL")

    def test_older_than_the_interval_fetches(self):
        last = self.TODAY - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS + 30)

        assert self._freshness(last_fetched_on=last).needs_fetch("AAPL")

    def test_staleness_is_measured_in_as_of_time_not_wall_clock(self):
        """Issue #258 review, final round, finding A.

        An ordinary evening run evaluates the previous trading day, so its
        recorded horizon trails the wall clock -- by one day mid-week, by
        three over a weekend. Measuring staleness against `today` banks that
        offset as age on every run and shortens the interval from seven days
        to four or six. Here the horizon is six *evaluation* days old but
        nine wall-clock days old: the symbol must not be due.
        """
        as_of = self.TODAY - timedelta(days=3)  # Monday run, Friday's close

        freshness = self._freshness(
            as_of=as_of,
            last_fetched_on=self.TODAY - timedelta(days=6),
            fetched_through_on=as_of
            - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS - 1),
        )

        assert not freshness.needs_fetch("AAPL")

    def test_the_interval_still_elapses_in_as_of_time(self):
        """The other side: seven evaluation days really is due."""
        as_of = self.TODAY - timedelta(days=3)

        freshness = self._freshness(
            as_of=as_of,
            last_fetched_on=self.TODAY - timedelta(days=6),
            fetched_through_on=as_of
            - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS),
        )

        assert freshness.needs_fetch("AAPL")

    def test_the_retry_window_is_measured_in_as_of_time_too(self):
        """A filing date and the evaluation date are both data dates.

        Six evaluation days after the filing the window is still open, even
        though nine wall-clock days have passed.
        """
        as_of = self.TODAY - timedelta(days=3)

        freshness = self._freshness(
            as_of=as_of,
            last_fetched_on=self.TODAY - timedelta(days=1),
            fetched_through_on=as_of,
            latest_filing_on=as_of
            - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS - 1),
        )

        assert freshness.needs_fetch("AAPL")

    def test_a_same_day_rerun_skips(self):
        """P6-25's same-day skip."""
        assert not self._freshness(last_fetched_on=self.TODAY).needs_fetch("AAPL")

    def test_a_same_day_rerun_of_an_ancient_as_of_still_skips(self):
        """Issue #258 review finding 2 (second attempt).

        A replay of a date older than the refresh interval polls EDGAR today
        but only reaches a stale horizon. Keying the skip on the horizon --
        the first attempt at this fix -- re-fetched the whole universe on
        every rerun, which is exactly the cost P6-25 exists to prevent. The
        wall-clock fetch day is what the same-day skip must read.
        """
        ancient = self.TODAY - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS + 30)

        freshness = self._freshness(
            last_fetched_on=self.TODAY, fetched_through_on=ancient
        )

        assert not freshness.needs_fetch("AAPL")

    def test_a_stale_horizon_from_a_replay_is_due_the_next_day(self):
        """The other half: the replay must not have granted real freshness."""
        ancient = self.TODAY - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS + 30)

        freshness = self._freshness(
            last_fetched_on=self.TODAY - timedelta(days=1), fetched_through_on=ancient
        )

        assert freshness.needs_fetch("AAPL")

    def test_an_unknown_horizon_is_due_rather_than_fresh(self):
        """A row written before `fetched_through` existed must not read fresh."""
        freshness = self._freshness(
            last_fetched_on=self.TODAY - timedelta(days=1), fetched_through_on=None
        )

        assert freshness.needs_fetch("AAPL")

    @pytest.mark.parametrize(
        ("consecutive_empty", "expected"),
        [
            pytest.param(0, _FUNDAMENTALS_REFRESH_INTERVAL_DAYS, id="no-empties"),
            pytest.param(1, 1, id="first-empty-retries-tomorrow"),
            pytest.param(2, 2, id="second-empty"),
            pytest.param(3, 4, id="third-empty"),
            pytest.param(
                4, _FUNDAMENTALS_REFRESH_INTERVAL_DAYS, id="converged-to-weekly"
            ),
            pytest.param(99, _FUNDAMENTALS_REFRESH_INTERVAL_DAYS, id="stays-converged"),
        ],
    )
    def test_the_empty_backoff_widens_and_then_converges(
        self, consecutive_empty, expected
    ):
        """Issue #258 review, second round, finding 1.

        Both requirements live in this table. `1` keeps a systemic empty
        response from freezing fundamentals for a week (it is retried
        tomorrow); reaching the ordinary interval is what stops a
        permanently factless symbol from costing a request a day forever.
        The shortened gaps sum to the interval, so the escalation never
        overshoots what it converges to.
        """
        assert _refresh_interval_days(consecutive_empty) == expected

    def test_an_empty_answer_shortens_the_gap_to_a_single_day(self):
        """The rule, not just the table: yesterday's empty is due today.

        `fetched_through_on=None` is what an empty answer leaves behind for a
        symbol that has never had a productive fetch -- the horizon does not
        move on an empty result.
        """
        freshness = self._freshness(
            last_fetched_on=self.TODAY - timedelta(days=1),
            fetched_through_on=None,
            consecutive_empty=1,
        )

        assert freshness.needs_fetch("AAPL")

    def test_a_converged_empty_symbol_waits_out_the_full_interval(self):
        """Converged, the throttle holds a due symbol until the interval."""
        freshness = self._freshness(
            last_fetched_on=self.TODAY
            - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS - 1),
            fetched_through_on=None,
            consecutive_empty=len(_FUNDAMENTALS_EMPTY_BACKOFF_DAYS) + 1,
        )

        assert not freshness.needs_fetch("AAPL")

    def test_the_throttle_never_holds_a_symbol_past_one_interval(self):
        """Finding A's invariant at the rule level: the ceiling is the interval."""
        freshness = self._freshness(
            last_fetched_on=self.TODAY
            - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS),
            fetched_through_on=None,
            consecutive_empty=99,
        )

        assert freshness.needs_fetch("AAPL")

    def test_an_empty_answer_does_not_reset_the_staleness_clock(self):
        """Finding A, at the rule level.

        The symbol was last productive a full interval ago and has been
        retried (empty) since. The backstop is measured from the horizon, not
        from the retry, so the symbol is still due -- a fruitless retry
        cannot buy another interval of staleness.
        """
        freshness = self._freshness(
            last_fetched_on=self.TODAY - timedelta(days=1),
            fetched_through_on=self.TODAY
            - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS),
            consecutive_empty=1,
        )

        assert freshness.needs_fetch("AAPL")

    def test_a_filing_not_yet_ingested_fetches_within_the_interval(self):
        last = self.TODAY - timedelta(days=3)

        freshness = self._freshness(
            last_fetched_on=last, latest_filing_on=last + timedelta(days=1)
        )

        assert freshness.needs_fetch("AAPL")

    def test_a_filing_dated_exactly_on_the_last_fetch_day_fetches(self):
        """Inclusive on purpose.

        A 10-Q accepted at 16:30 ET is stored as that day's date at midnight
        UTC, i.e. *before* the same evening's fetch timestamp. A strict
        `filing > last_fetch` comparison would drop it entirely.
        """
        last = self.TODAY - timedelta(days=3)

        freshness = self._freshness(last_fetched_on=last, latest_filing_on=last)

        assert freshness.needs_fetch("AAPL")

    def test_the_trigger_stays_armed_after_a_fetch_that_found_nothing(self):
        """Issue #258 review finding 1: the trigger is a window, not an edge.

        EDGAR's bulk company-facts lags the filing itself, so the fetch a
        filing triggers routinely returns nothing new. The old rule compared
        the filing date against the *last fetch* date, so that empty fetch
        disarmed the trigger for the whole backstop interval -- exactly the
        promise "held + candidates are picked up by the trigger" failing
        quietly. Here the filing is a day older than the fetch that missed
        it, and the symbol must still be due.
        """
        filed_on = self.TODAY - timedelta(days=4)
        fetched_after_it = self.TODAY - timedelta(days=3)

        freshness = self._freshness(
            last_fetched_on=fetched_after_it, latest_filing_on=filed_on
        )

        assert freshness.needs_fetch("AAPL")

    def test_the_trigger_disarms_once_the_filing_is_ingested(self):
        """The window closes early on success, so the retry is not perpetual."""
        filed_on = self.TODAY - timedelta(days=4)

        freshness = self._freshness(
            last_fetched_on=self.TODAY - timedelta(days=3),
            latest_filing_on=filed_on,
            latest_ingested_on=filed_on,
        )

        assert not freshness.needs_fetch("AAPL")

    def test_an_ingested_filing_older_than_the_collected_one_stays_armed(self):
        """Last quarter's record does not satisfy this quarter's filing."""
        filed_on = self.TODAY - timedelta(days=4)

        freshness = self._freshness(
            last_fetched_on=self.TODAY - timedelta(days=3),
            latest_filing_on=filed_on,
            latest_ingested_on=filed_on - timedelta(days=90),
        )

        assert freshness.needs_fetch("AAPL")

    def test_the_retry_window_closes_at_the_refresh_interval(self):
        """The bound that stops an 8-K from arming the trigger forever.

        An 8-K never becomes a `FundamentalsRecord`, so "retry until it
        lands" is unbounded without this. At exactly the interval the window
        is shut and the backstop -- which owns the symbol from here -- is the
        only rule left.
        """
        filed_on = self.TODAY - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS)

        freshness = self._freshness(
            last_fetched_on=self.TODAY - timedelta(days=1), latest_filing_on=filed_on
        )

        assert not freshness.needs_fetch("AAPL")

    def test_the_retry_window_is_still_open_one_day_before_the_interval(self):
        filed_on = self.TODAY - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS - 1)

        freshness = self._freshness(
            last_fetched_on=self.TODAY - timedelta(days=1), latest_filing_on=filed_on
        )

        assert freshness.needs_fetch("AAPL")

    def test_no_collected_filing_falls_back_to_the_elapsed_days_rule(self):
        """The universe outside text collection's ~30 symbols lives here."""
        last = self.TODAY - timedelta(days=3)

        assert not self._freshness(last_fetched_on=last).needs_fetch("AAPL")

    def test_another_symbols_filing_never_triggers_this_one(self):
        fetched_on = self.TODAY - timedelta(days=3)
        freshness = _FundamentalsFreshness(
            today=self.TODAY,
            as_of=self.TODAY,
            fetch_state={
                "AAPL": FundamentalsFetchState(
                    last_fetched_on=fetched_on, fetched_through_on=fetched_on
                )
            },
            latest_filing_on={"MSFT": self.TODAY},
            latest_ingested_on={},
        )

        assert not freshness.needs_fetch("AAPL")


class _CountingEdgarClient:
    """EDGAR fake that records *which* symbols reached the network (#258)."""

    def __init__(self, records_by_symbol=None):
        self.calls: list[str] = []
        self._records_by_symbol = records_by_symbol or {}

    def fetch_fundamentals(self, symbol, as_of):
        del as_of
        self.calls.append(symbol)
        return self._records_by_symbol.get(symbol, _healthy_fundamentals(symbol))

    def fetch_filing_texts(self, symbol, form_types, *, as_of, since=None, limit=None):
        del symbol, form_types, as_of, since, limit
        return []


class _FixedClock:
    """A clock pinned to one wall-clock day, for multi-day scenarios."""

    def __init__(self, today: date):
        self._today = today

    def today(self):
        return self._today

    def now(self):
        return datetime.combine(self._today, time(12), tzinfo=UTC)


class _AlwaysFailingEdgarClient(_CountingEdgarClient):
    """Stands in for EDGAR being down for every symbol."""

    def fetch_fundamentals(self, symbol, as_of):
        del as_of
        self.calls.append(symbol)
        msg = "EDGAR is down"
        raise RuntimeError(msg)


def _fundamentals_step_detail(state_store, run_id):
    with state_store._database.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT detail FROM run_steps WHERE run_id = ? AND step = '2_fundamentals'",
            [run_id],
        ).fetchone()
    assert row is not None
    return row[0]


class TestFundamentalsIncrementalRefresh:
    """Issue #258, end to end: the rule must reach the EDGAR client itself.

    `TestFundamentalsFreshnessRule` pins the decision; these prove the decision
    is what actually gates the network call, that the bookkeeping written
    afterwards matches what the run really bought, and that the step reports
    a refresh failure instead of taking the run down with it.
    """

    def _deps(  # noqa: PLR0913 - a fixture-assembly helper, one arg per fixture
        self,
        settings,
        market_store,
        state_store,
        tmp_path,
        edgar_client,
        symbols=("AAPL",),
        clock=None,
    ):
        return DailyDependencies(
            data_provider=FakeDataProvider(
                _bars_for(list(symbols), AS_OF + timedelta(days=1))
            ),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=tuple(_member(symbol) for symbol in symbols),
            strategies_config=STRATEGIES_CONFIG,
            clock=clock or FakeClock(),
            edgar_client=edgar_client,
            output_dir=str(tmp_path / "reports"),
        )

    def _run(self, deps, as_of=AS_OF):
        return run_daily(
            DailyRunOptions(as_of=as_of, is_dry_run=True, allow_same_day_rerun=True),
            deps,
        )

    def _seed_fetch(self, market_store, days_ago, symbol="AAPL"):
        """Record a past run's fetch, as a normal (non-replay) run would."""
        stamp = datetime.combine(
            AS_OF - timedelta(days=days_ago), datetime.min.time(), tzinfo=UTC
        )
        market_store.record_fundamentals_fetches(
            [FundamentalsFetchStamp(symbol, stamp, stamp, 0)]
        )

    def _fetch_state(self, market_store, symbol="AAPL"):
        return market_store.read_fundamentals_fetch_state([symbol]).get(symbol)

    def _seed_ingested_filing(self, market_store, filed_on, symbol="AAPL"):
        """Give `symbol` filing history, so an empty result contradicts it."""
        market_store.upsert_fundamentals(
            [
                replace(
                    _healthy_fundamentals(symbol)[0],
                    accession_no=f"acc-{symbol}-seeded",
                    filed_at=datetime.combine(
                        filed_on, datetime.min.time(), tzinfo=UTC
                    ),
                )
            ]
        )

    def _seed_filing(self, state_store, filed_on, form="10-Q", symbol="AAPL"):
        published_at = datetime.combine(filed_on, datetime.min.time(), tzinfo=UTC)
        state_store.record_text_items(
            [
                TextItem(
                    source_id=f"edgar:0000000-27-{symbol}-{form}",
                    symbol=symbol,
                    source_type="filing",
                    published_at=published_at,
                    title=f"{form} - {symbol}",
                    source_url="https://www.sec.gov/example",
                    content_text="Periodic report body.",
                    fetched_at=published_at,
                )
            ]
        )

    def test_a_first_ever_run_fetches_and_stamps_the_log(
        self, settings, market_store, state_store, tmp_path
    ):
        edgar_client = _CountingEdgarClient()

        result = self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        assert result.status == RunStatus.SUCCESS
        assert edgar_client.calls == ["AAPL"]
        assert self._fetch_state(market_store) == FundamentalsFetchState(
            last_fetched_on=AS_OF, fetched_through_on=AS_OF
        )

    def test_a_recent_fetch_with_no_new_filing_skips_the_network(
        self, settings, market_store, state_store, tmp_path
    ):
        self._seed_fetch(market_store, days_ago=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS - 1)
        edgar_client = _CountingEdgarClient()

        result = self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        assert result.status == RunStatus.SUCCESS
        assert edgar_client.calls == []

    def test_reaching_the_refresh_interval_fetches_again(
        self, settings, market_store, state_store, tmp_path
    ):
        self._seed_fetch(market_store, days_ago=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS)
        edgar_client = _CountingEdgarClient()

        result = self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        assert result.status == RunStatus.SUCCESS
        assert edgar_client.calls == ["AAPL"]
        assert self._fetch_state(market_store) == FundamentalsFetchState(
            last_fetched_on=AS_OF, fetched_through_on=AS_OF
        )

    def test_a_filing_collected_since_the_last_fetch_forces_an_early_refetch(
        self, settings, market_store, state_store, tmp_path
    ):
        self._seed_fetch(market_store, days_ago=2)
        self._seed_filing(state_store, AS_OF - timedelta(days=1))
        edgar_client = _CountingEdgarClient()

        result = self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        assert result.status == RunStatus.SUCCESS
        assert edgar_client.calls == ["AAPL"]

    def test_a_filing_already_ingested_does_not_force_a_refetch(
        self, settings, market_store, state_store, tmp_path
    ):
        """The trigger disarms on the record landing, not on a fetch happening."""
        filed_on = AS_OF - timedelta(days=3)
        market_store.upsert_fundamentals(
            [
                replace(
                    record,
                    accession_no="acc-landed",
                    filed_at=datetime.combine(
                        filed_on, datetime.min.time(), tzinfo=UTC
                    ),
                )
                for record in _healthy_fundamentals("AAPL")[:1]
            ]
        )
        self._seed_fetch(market_store, days_ago=2)
        self._seed_filing(state_store, filed_on)
        edgar_client = _CountingEdgarClient()

        result = self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        assert result.status == RunStatus.SUCCESS
        assert edgar_client.calls == []

    def test_a_pending_filing_keeps_retrying_after_an_empty_fetch(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review finding 1, end to end.

        The collected filing predates the recorded fetch, so the old
        one-shot edge treated it as already handled. Company-facts lag makes
        that the common case, and the symbol then waited out the whole
        backstop interval with last quarter's numbers.
        """
        self._seed_fetch(market_store, days_ago=2)
        self._seed_filing(state_store, AS_OF - timedelta(days=3))
        edgar_client = _CountingEdgarClient(records_by_symbol={"AAPL": []})

        result = self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        assert result.status == RunStatus.SUCCESS
        assert edgar_client.calls == ["AAPL"]

    def test_a_collected_10k_arms_the_trigger_too(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review finding 4: the trigger is not form-gated.

        `_FILING_FORM_TYPES` governs which forms step 5 *collects*; it must
        never become the trigger's filter, or the form that carries the
        annual figures would be the one form unable to ask for them.
        """
        self._seed_fetch(market_store, days_ago=2)
        self._seed_filing(state_store, AS_OF - timedelta(days=1), form="10-K")
        edgar_client = _CountingEdgarClient()

        result = self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        assert result.status == RunStatus.SUCCESS
        assert edgar_client.calls == ["AAPL"]

    def test_a_past_as_of_replay_does_not_grant_current_freshness(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review finding 2, half one.

        A replay only ever retrieves `filed_at <= as_of`, so it must not
        record the wall clock as its *horizon* -- doing so let one replay
        declare the universe fresh and suppress the operator's real refresh
        for a whole week.
        """
        replay_as_of = AS_OF - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS + 3)
        edgar_client = _CountingEdgarClient()
        deps = self._deps(settings, market_store, state_store, tmp_path, edgar_client)

        self._run(deps, as_of=replay_as_of)
        assert self._fetch_state(market_store) == FundamentalsFetchState(
            last_fetched_on=AS_OF, fetched_through_on=replay_as_of
        )

        # The operator's next real run, on the following wall-clock day: the
        # same-day skip no longer applies and the stale horizon makes the
        # symbol due, so the replay bought it no freshness at all.
        next_day = AS_OF + timedelta(days=1)
        self._run(
            self._deps(
                settings,
                market_store,
                state_store,
                tmp_path,
                edgar_client,
                clock=_FixedClock(next_day),
            ),
            as_of=next_day,
        )

        assert edgar_client.calls == ["AAPL", "AAPL"]

    def test_rerunning_an_ancient_as_of_the_same_day_skips_the_network(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review finding 2, half two -- and P6-25's own contract.

        The `as_of` here is well outside the refresh interval, which is the
        case the first attempt at the clamp got wrong: it keyed the same-day
        skip on the (deliberately stale) horizon, so every rerun re-fetched
        the whole universe. The existing P6-25 regression only used
        `AS_OF - 5 days`, inside the interval, so it could not see this.
        """
        ancient_as_of = AS_OF - timedelta(days=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS + 30)
        edgar_client = _CountingEdgarClient()
        deps = self._deps(settings, market_store, state_store, tmp_path, edgar_client)

        self._run(deps, as_of=ancient_as_of)
        self._run(deps, as_of=ancient_as_of)

        assert edgar_client.calls == ["AAPL"]

    def test_a_run_given_an_explicit_as_of_of_today_records_the_wall_clock(
        self, settings, market_store, state_store, tmp_path
    ):
        """`as_of == today` leaves the clamp inert, as the simple case."""
        edgar_client = _CountingEdgarClient()

        self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        with market_store.get_connection() as conn:
            stamped = conn.execute(
                "SELECT last_fetched_at, fetched_through "
                "FROM fundamentals_fetch_log WHERE symbol = ?",
                ["AAPL"],
            ).fetchone()
        assert stamped == (FakeClock().now(), FakeClock().now())

    def test_the_scheduled_run_records_the_trading_day_it_evaluated(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review, final round, finding A -- production conditions.

        The scheduled 18:30 JST run passes no `--as-of`, so `daily_runner`
        resolves `run_date` to the newest bar it fetched: the previous
        trading day. The horizon recorded is therefore that day, not the wall
        clock -- the fetch genuinely could not see a filing accepted after
        it. The earlier version of this test handed the fake provider bars
        whose newest date happened to equal the clock's today, so it asserted
        `horizon == now` and never exercised the real condition.

        What must *not* follow from this is a shortened refresh interval;
        that is what measuring staleness in `as_of` time protects, pinned by
        `TestFundamentalsFreshnessRule`'s as-of-time cases and by the
        next-day skip below.
        """
        previous_trading_day = AS_OF - timedelta(days=1)
        edgar_client = _CountingEdgarClient()
        deps = DailyDependencies(
            # `_bars_for(symbols, day)`'s newest bar is `day - 1`, so the
            # newest bar here is the day before the clock's today.
            data_provider=FakeDataProvider(_bars_for(["AAPL"], AS_OF)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=(_member("AAPL"),),
            strategies_config=STRATEGIES_CONFIG,
            clock=FakeClock(),
            edgar_client=edgar_client,
            output_dir=str(tmp_path / "reports"),
        )

        # No `as_of`: this is the scheduled-run path that overwrites run_date.
        run_daily(DailyRunOptions(is_dry_run=True), deps)

        assert edgar_client.calls == ["AAPL"]
        assert self._fetch_state(market_store) == FundamentalsFetchState(
            last_fetched_on=AS_OF, fetched_through_on=previous_trading_day
        )

    def test_the_scheduled_run_still_waits_the_whole_interval(
        self, settings, market_store, state_store, tmp_path
    ):
        """The consequence finding A was really about.

        Two scheduled runs whose evaluation dates are one day apart: the
        second must skip. Measuring the horizon (`AS_OF - 1`) against the
        wall clock instead would call it two days old on day one and would
        reach the interval a day early every week.
        """
        edgar_client = _CountingEdgarClient()

        for offset in range(_FUNDAMENTALS_REFRESH_INTERVAL_DAYS):
            day = AS_OF + timedelta(days=offset)
            run_daily(
                DailyRunOptions(is_dry_run=True, allow_same_day_rerun=True),
                DailyDependencies(
                    data_provider=FakeDataProvider(_bars_for(["AAPL"], day)),
                    market_store=market_store,
                    state_store=state_store,
                    settings=settings,
                    universe=(_member("AAPL"),),
                    strategies_config=STRATEGIES_CONFIG,
                    clock=_FixedClock(day),
                    edgar_client=edgar_client,
                    output_dir=str(tmp_path / "reports"),
                ),
            )

        # Day 0 fetched; days 1..6 are all inside the interval in as-of time.
        assert edgar_client.calls == ["AAPL"]

    def test_a_single_symbol_failing_never_fails_the_run(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review finding 1 (HIGH), constraint (a).

        The incremental rule means a typical day attempts nought to a couple
        of symbols, so "every attempted symbol failed" is reached by *one*
        broken ticker. Failures are never stamped, so that ticker is due
        again tomorrow -- making it a fatal condition took the whole run down
        (exit 1, no report) every single day.
        """
        self._seed_fetch(market_store, days_ago=1, symbol="AAPL")

        edgar_client = _AlwaysFailingEdgarClient()

        result = self._run(
            self._deps(
                settings,
                market_store,
                state_store,
                tmp_path,
                edgar_client,
                symbols=("AAPL", "MSFT"),
            )
        )

        assert edgar_client.calls == ["MSFT"]  # AAPL was skipped as still fresh
        assert result.status == RunStatus.SUCCESS
        assert result.exit_code == 0

    def test_a_total_edgar_outage_is_named_in_the_step_detail(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review finding 1 (HIGH), constraint (b).

        Dropping the fatal branch must not make an outage invisible. The
        detail says outright that nothing got through, so the operator is
        not left inferring it from a symbol list.
        """
        edgar_client = _AlwaysFailingEdgarClient()
        deps = self._deps(
            settings,
            market_store,
            state_store,
            tmp_path,
            edgar_client,
            symbols=("AAPL", "MSFT"),
        )

        result = self._run(deps)

        detail = _fundamentals_step_detail(state_store, result.run_id)
        assert "EDGAR refreshed nothing" in detail
        assert "all 2 attempted symbol(s)" in detail
        assert "AAPL" in detail
        assert "MSFT" in detail

    def test_a_partial_failure_is_reported_without_the_outage_marker(
        self, settings, market_store, state_store, tmp_path
    ):
        """Constraint (b)'s other side: one failure is not an outage."""

        class FailsOneSymbol(_CountingEdgarClient):
            def fetch_fundamentals(self, symbol, as_of):
                del as_of
                self.calls.append(symbol)
                if symbol == "MSFT":
                    msg = "EDGAR is down"
                    raise RuntimeError(msg)
                return _healthy_fundamentals(symbol)

        deps = self._deps(
            settings,
            market_store,
            state_store,
            tmp_path,
            FailsOneSymbol(),
            symbols=("AAPL", "MSFT"),
        )

        result = self._run(deps)

        detail = _fundamentals_step_detail(state_store, result.run_id)
        assert result.status == RunStatus.SUCCESS
        assert "EDGAR refreshed nothing" not in detail
        assert "failed symbols: 1 (MSFT)" in detail

    def test_a_run_whose_symbols_are_all_skipped_reports_nothing(
        self, settings, market_store, state_store, tmp_path
    ):
        """Nothing attempted, nothing to say -- and certainly no outage marker."""
        self._seed_fetch(market_store, days_ago=1)
        edgar_client = _CountingEdgarClient()

        result = self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        assert edgar_client.calls == []
        assert result.status == RunStatus.SUCCESS
        assert _fundamentals_step_detail(state_store, result.run_id) is None

    def test_an_empty_result_contradicting_stored_filings_is_reported(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review finding 3, the visibility half.

        A universe-wide empty response is the P6-25 incident shape, and it
        used to leave nothing at all in `run_steps.detail`. An empty answer
        that contradicts filings already on file is the operator-actionable
        case, so it is named.
        """
        self._seed_ingested_filing(market_store, AS_OF - timedelta(days=40))
        self._seed_fetch(market_store, days_ago=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS)
        edgar_client = _CountingEdgarClient(records_by_symbol={"AAPL": []})

        result = self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )

        detail = _fundamentals_step_detail(state_store, result.run_id)
        assert edgar_client.calls == ["AAPL"]
        assert "no records despite stored filings: 1 (AAPL)" in detail

    def test_an_empty_result_is_stamped_and_retried_the_next_day(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review finding 3, the retry half.

        The empty answer is stamped -- leaving it unstamped is what made the
        retry unbounded -- but its counter shortens the next gap to a single
        day, so a systemic empty response does not freeze fundamentals for a
        week. Within the same day the rerun still skips.
        """
        edgar_client = _CountingEdgarClient(records_by_symbol={"AAPL": []})
        deps = self._deps(settings, market_store, state_store, tmp_path, edgar_client)

        self._run(deps)
        # Polled today, but the horizon did not move: an empty answer carries
        # no data, so it must not restart the staleness clock.
        assert self._fetch_state(market_store) == FundamentalsFetchState(
            last_fetched_on=AS_OF, fetched_through_on=None, consecutive_empty=1
        )

        self._run(deps)  # same wall-clock day
        assert edgar_client.calls == ["AAPL"]

        next_day = AS_OF + timedelta(days=1)
        self._run(
            self._deps(
                settings,
                market_store,
                state_store,
                tmp_path,
                edgar_client,
                clock=_FixedClock(next_day),
            ),
            as_of=next_day,
        )
        assert edgar_client.calls == ["AAPL", "AAPL"]

    def test_a_permanently_empty_symbol_converges_on_the_weekly_cadence(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review, second round, finding 1.

        A symbol that will never have XBRL facts -- a delisted shell, a 20-F
        foreign private issuer, a trust -- must not cost one request a day
        forever. The backoff widens the gap on each consecutive empty answer
        and lands on the ordinary interval, so the permanent cost is the same
        as any other symbol's.
        """
        edgar_client = _CountingEdgarClient(records_by_symbol={"AAPL": []})

        # 22 days is the shortest walk that reaches the converged cadence:
        # polls land on offsets 0, 1, 3, 7, 14 and 21.
        fetched_on = self._walk_days(
            settings, market_store, state_store, tmp_path, edgar_client, days=22
        )

        gaps = [(later - earlier).days for earlier, later in pairwise(fetched_on)]
        assert gaps[: len(_FUNDAMENTALS_EMPTY_BACKOFF_DAYS)] == list(
            _FUNDAMENTALS_EMPTY_BACKOFF_DAYS
        )
        # Converged: every later gap is the ordinary interval, so the symbol
        # costs exactly what a normal one does from here on.
        assert set(gaps[len(_FUNDAMENTALS_EMPTY_BACKOFF_DAYS) :]) == {
            _FUNDAMENTALS_REFRESH_INTERVAL_DAYS
        }

    def test_a_non_empty_fetch_clears_the_backoff(
        self, settings, market_store, state_store, tmp_path
    ):
        """A transient empty spell must not leave the symbol backed off."""
        edgar_client = _CountingEdgarClient(records_by_symbol={"AAPL": []})
        self._run(
            self._deps(settings, market_store, state_store, tmp_path, edgar_client)
        )
        assert self._fetch_state(market_store).consecutive_empty == 1

        next_day = AS_OF + timedelta(days=1)
        self._run(
            self._deps(
                settings,
                market_store,
                state_store,
                tmp_path,
                _CountingEdgarClient(),
                clock=_FixedClock(next_day),
            ),
            as_of=next_day,
        )

        assert self._fetch_state(market_store).consecutive_empty == 0

    def _walk_days(  # noqa: PLR0913 - fixtures plus the two knobs the walk needs
        self, settings, market_store, state_store, tmp_path, edgar_client, days
    ):
        """Run one daily run per calendar day; return the days that polled."""
        polled_on: list[date] = []
        for offset in range(days):
            day = AS_OF + timedelta(days=offset)
            before = len(edgar_client.calls)
            self._run(
                self._deps(
                    settings,
                    market_store,
                    state_store,
                    tmp_path,
                    edgar_client,
                    clock=_FixedClock(day),
                ),
                as_of=day,
            )
            if len(edgar_client.calls) > before:
                polled_on.append(day)
        return polled_on

    def test_a_fruitless_retry_never_pushes_the_backstop_out(
        self, settings, market_store, state_store, tmp_path
    ):
        """Issue #258 review, final round, finding A.

        The pending-filing window exists because EDGAR's bulk company-facts
        lags the filing, so its retries routinely return nothing. If each of
        those fruitless retries restarted the staleness clock, the window
        would push the backstop out by however long it ran -- making a symbol
        whose trigger fired *staler* than one whose never did, the exact
        inversion of what the trigger is for. The horizon therefore only
        advances on a fetch that produced records.

        Pinned as the invariant that matters: **no symbol goes longer than
        one refresh interval between polls**, trigger or no trigger.
        """
        # A collected filing that company-facts never publishes: the trigger
        # fires and every retry comes back empty.
        self._seed_filing(state_store, AS_OF)
        edgar_client = _CountingEdgarClient(records_by_symbol={"AAPL": []})

        polled_on = self._walk_days(
            settings, market_store, state_store, tmp_path, edgar_client, days=22
        )

        gaps = [(later - earlier).days for earlier, later in pairwise(polled_on)]
        assert max(gaps) <= _FUNDAMENTALS_REFRESH_INTERVAL_DAYS
        # And the horizon never moved, because nothing was ever fetched.
        assert self._fetch_state(market_store).fetched_through_on is None

    def test_a_quiet_symbol_is_polled_exactly_on_the_interval(
        self, settings, market_store, state_store, tmp_path
    ):
        """The baseline the previous test must never be worse than.

        Finding A's inversion was "a symbol whose trigger fired ends up
        polled *less* often than one whose never did". Pinning both cadences
        -- exactly the interval here, at most the interval there -- is what
        makes the inversion impossible to reintroduce unnoticed.
        """
        edgar_client = _CountingEdgarClient()

        polled_on = self._walk_days(
            settings, market_store, state_store, tmp_path, edgar_client, days=15
        )

        gaps = [(later - earlier).days for earlier, later in pairwise(polled_on)]
        assert set(gaps) == {_FUNDAMENTALS_REFRESH_INTERVAL_DAYS}

    def test_day_two_of_a_mixed_universe_costs_exactly_three_requests(
        self, settings, market_store, state_store, tmp_path
    ):
        """The PR's actual claim, pinned: which symbols day two pays for.

        Every other test here isolates one rule. This one runs two
        consecutive days over a universe holding all of them at once and
        asserts the exact request count, so a future change that quietly
        re-expands the fetch set -- the whole regression this work exists to
        prevent -- fails here rather than only showing up as a slower run.

        Day two's five symbols:

        - FRESH1/FRESH2: fetched yesterday with records -> skipped.
        - PENDING: fetched yesterday, but a filing collected since then is
          still not in `fundamentals` -> fetched.
        - NOFACTS: yesterday's fetch came back empty, so its backoff is one
          day -> fetched.
        - EXPIRED: last fetched exactly `_FUNDAMENTALS_REFRESH_INTERVAL_DAYS`
          ago as of day two -> fetched by the backstop.
        """
        symbols = ("NOFACTS", "EXPIRED", "FRESH1", "FRESH2", "PENDING")
        day_one, day_two = AS_OF, AS_OF + timedelta(days=1)
        # Six days old on day one (skipped), exactly seven on day two (due).
        self._seed_fetch(
            market_store,
            days_ago=_FUNDAMENTALS_REFRESH_INTERVAL_DAYS - 1,
            symbol="EXPIRED",
        )
        edgar_client = _CountingEdgarClient(records_by_symbol={"NOFACTS": []})

        def run(day, clock_day):
            self._run(
                self._deps(
                    settings,
                    market_store,
                    state_store,
                    tmp_path,
                    edgar_client,
                    symbols=symbols,
                    clock=_FixedClock(clock_day),
                ),
                as_of=day,
            )

        run(day_one, day_one)
        assert sorted(edgar_client.calls) == ["FRESH1", "FRESH2", "NOFACTS", "PENDING"]

        # A filing for PENDING lands, collected by day one's text step.
        self._seed_filing(state_store, day_two, symbol="PENDING")
        day_one_calls = len(edgar_client.calls)

        run(day_two, day_two)

        assert sorted(edgar_client.calls[day_one_calls:]) == [
            "EXPIRED",
            "NOFACTS",
            "PENDING",
        ]

    def test_a_failed_fetch_is_not_stamped_so_the_next_run_retries(
        self, settings, market_store, state_store, tmp_path
    ):
        class FailingThenWorkingEdgarClient(_CountingEdgarClient):
            def fetch_fundamentals(self, symbol, as_of):
                del as_of
                self.calls.append(symbol)
                if len(self.calls) == 1:
                    msg = "EDGAR is down"
                    raise RuntimeError(msg)
                return _healthy_fundamentals(symbol)

        edgar_client = FailingThenWorkingEdgarClient()
        deps = self._deps(settings, market_store, state_store, tmp_path, edgar_client)

        self._run(deps)
        assert self._fetch_state(market_store) is None

        self._run(deps)
        assert edgar_client.calls == ["AAPL", "AAPL"]
        assert self._fetch_state(market_store) == FundamentalsFetchState(
            last_fetched_on=AS_OF, fetched_through_on=AS_OF
        )


class TestFundamentalsHeldFirstOrder:
    """Issue #219: the NFR-03 budget must truncate candidates, not holdings.

    `_select_symbols()` returns lexicographic order, so a holding whose ticker
    sorts after every candidate used to lose its fundamentals refresh to
    alphabetically-earlier candidates whenever the budget ran out mid-step --
    the exact outcome `_text_target_symbols()` already prevents on the text
    side. `_HELD_SYMBOL` is deliberately last alphabetically and outside the
    universe, so lexicographic order and held-first order disagree.
    """

    @pytest.fixture
    def make_deps(self, settings, market_store, state_store, tmp_path):
        """Factory for a live run holding `_HELD_SYMBOL` outside the universe."""
        state_store.upsert_position(
            Position(
                position_id=uuid4(),
                symbol=_HELD_SYMBOL,
                is_paper=True,
                entry_date=AS_OF - timedelta(days=5),
                entry_price=100.0,
                shares=10,
                status="open",
                stop_price=95.0,
            )
        )

        def _make(edgar_client, monotonic):
            return DailyDependencies(
                data_provider=FakeDataProvider(
                    _bars_for(["AAPL", "MSFT", _HELD_SYMBOL], AS_OF)
                ),
                market_store=market_store,
                state_store=state_store,
                settings=settings,
                universe=(_member("AAPL"), _member("MSFT")),
                strategies_config=STRATEGIES_CONFIG,
                clock=FakeClock(),
                edgar_client=edgar_client,
                monotonic=monotonic,
                output_dir=str(tmp_path / "reports"),
            )

        return _make

    @staticmethod
    def _fundamentals_step_row(state_store, run_id):
        with state_store._database.connect() as conn:  # noqa: SLF001
            return conn.execute(
                "SELECT status, detail FROM run_steps "
                "WHERE run_id = ? AND step = '2_fundamentals'",
                [str(run_id)],
            ).fetchone()

    def test_budget_cutoff_spends_the_last_fetch_on_the_holding(
        self, settings, market_store, make_deps
    ):
        object.__setattr__(settings.schedule, "timeout_minutes", 1)  # 60s budget
        edgar_client = _RecordingEdgarClient()
        # run_started_at=0.0 -> deadline=60.0; the first symbol's check (10.0)
        # passes and it is fetched, the second (70.0) breaches and stops the
        # step. Exactly one of three symbols wins the budget, and the holding
        # must be the one -- not "AAPL", which merely sorts first.
        deps_with_holding = make_deps(edgar_client, FakeMonotonic(0.0, 10.0, 70.0))

        result = run_daily(DailyRunOptions(is_dry_run=True), deps_with_holding)

        assert edgar_client.fetched == [_HELD_SYMBOL]
        # What was collected before the cut is still upserted, and it is the
        # holding's filings that landed -- point-in-time filtering unchanged.
        persisted = market_store.read_fundamentals(AS_OF)
        assert set(persisted["symbol"]) == {_HELD_SYMBOL}
        assert result.exit_code == 0

    def test_budget_cutoff_stays_non_fatal_with_a_partial_completion_detail(
        self, settings, state_store, make_deps
    ):
        object.__setattr__(settings.schedule, "timeout_minutes", 1)  # 60s budget
        deps_with_holding = make_deps(
            _RecordingEdgarClient(), FakeMonotonic(0.0, 10.0, 70.0)
        )

        result = run_daily(DailyRunOptions(is_dry_run=True), deps_with_holding)

        # Fail-soft boundary unchanged: reordering must not turn a budget cut
        # into a failed step or a failed run.
        assert self._fundamentals_step_row(state_store, result.run_id) == (
            "success",
            "time budget exceeded after 1/3 symbols",
        )
        assert result.status == RunStatus.DEGRADED
        assert result.exit_code == 0

    def test_without_a_cutoff_every_symbol_is_still_fetched_exactly_once(
        self, state_store, make_deps
    ):
        edgar_client = _RecordingEdgarClient()
        deps_with_holding = make_deps(edgar_client, FakeMonotonic(0.0))

        result = run_daily(DailyRunOptions(is_dry_run=True), deps_with_holding)

        assert result.status == RunStatus.SUCCESS
        # Same set as before, reordered held-first and deterministically:
        # each block keeps `_select_symbols()`'s lexicographic order.
        assert edgar_client.fetched == [_HELD_SYMBOL, "AAPL", "MSFT"]
        assert self._fundamentals_step_row(state_store, result.run_id) == (
            "success",
            None,
        )

    def test_freshness_skip_still_covers_the_reordered_held_symbol(
        self, market_store, make_deps
    ):
        # P6-25 semantics unchanged: freshness is measured against the
        # injected `Clock`'s wall-clock today, not `as_of`. Promoting the
        # holding to the front of the queue must not make it refetch over the
        # network. (Issue #258 moved the bookkeeping from `fundamentals.
        # fetched_at` to `fundamentals_fetch_log`; the ordering contract this
        # test guards is unchanged.)
        stamp = datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC)
        market_store.record_fundamentals_fetches(
            [FundamentalsFetchStamp(_HELD_SYMBOL, stamp, stamp, 0)]
        )
        edgar_client = _RecordingEdgarClient()
        deps_with_holding = make_deps(edgar_client, FakeMonotonic(0.0))

        result = run_daily(DailyRunOptions(is_dry_run=True), deps_with_holding)

        assert result.status == RunStatus.SUCCESS
        assert edgar_client.fetched == ["AAPL", "MSFT"]


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


class TestScreeningTruncations:
    """Issue #188: `screening_truncations` end-to-end through the daily pipeline."""

    def test_the_symbol_cut_by_candidate_limit_is_persisted_with_its_rank(
        self, settings, market_store, state_store, tmp_path
    ):
        # Both symbols pass every filter/signal, so a limit of 1 makes the
        # second one a near-miss: the case that previously survived only in
        # the run directory's `rejections.json`.
        capped = {
            "strategies": {
                "default": {
                    "filters_all": ["volume_min"],
                    "signals_all": ["trend_sma"],
                    "candidate_limit": 1,
                }
            }
        }
        deps = DailyDependencies(
            data_provider=FakeDataProvider(_bars_for(["AAPL", "MSFT"], AS_OF)),
            market_store=market_store,
            state_store=state_store,
            settings=settings,
            universe=(_member("AAPL"), _member("MSFT")),
            strategies_config=capped,
            clock=FakeClock(),
            edgar_client=None,
            output_dir=str(tmp_path / "reports"),
        )

        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        assert result.status == RunStatus.SUCCESS
        with state_store._database.connect() as conn:  # noqa: SLF001
            candidate_count, truncation_rows = (
                conn.execute(
                    "SELECT count(*) FROM candidates WHERE run_id = ?",
                    [str(result.run_id)],
                ).fetchone()[0],
                conn.execute(
                    "SELECT symbol, strategy_key, rank, as_of "
                    "FROM screening_truncations WHERE run_id = ?",
                    [str(result.run_id)],
                ).fetchall(),
            )
        assert candidate_count == 1
        assert len(truncation_rows) == 1
        assert truncation_rows[0][1:] == ("default", 2, AS_OF)
        # The truncated symbol is the one that is *not* the candidate.
        assert truncation_rows[0][0] in {"AAPL", "MSFT"}

    def test_no_truncation_is_recorded_when_every_candidate_fits(
        self, deps, state_store
    ):
        result = run_daily(DailyRunOptions(as_of=AS_OF, is_dry_run=True), deps)

        with state_store._database.connect() as conn:  # noqa: SLF001
            count = conn.execute(
                "SELECT count(*) FROM screening_truncations WHERE run_id = ?",
                [str(result.run_id)],
            ).fetchone()
        assert count == (0,)


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
