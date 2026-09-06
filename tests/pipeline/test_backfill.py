"""`copilot-backfill` contract tests: chunking, resume, fail-soft, single write."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pandas as pd
import pytest

from swing_copilot.data.base import ACTIONS_COLUMNS, BarFetchResult, FetchFailure
from swing_copilot.pipeline.backfill import (
    CHUNK_SLEEP_SECONDS,
    COVERAGE_TOLERANCE_DAYS,
    REBUILD_START,
    SYMBOL_CHUNK_SIZE,
    BarsBackfillDeps,
    FundamentalsBackfillDeps,
    backfill_bars,
    backfill_fundamentals,
    check_bars,
    check_entry_prices,
    rebuild_bars,
)
from swing_copilot.pipeline.backfill import main as backfill_main
from swing_copilot.pipeline.daily import select_symbols
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import (
    BarWriteResult,
    FundamentalsRecord,
    MarketStore,
    NonFiniteBarsError,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import UniverseMember
from swing_copilot.universe_sampling import select_universe_sample
from tests.support.runs import seed_run as support_seed_run

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
_START = date(2019, 1, 1)
_END = date(2026, 7, 30)
#: `copilot-backfill bars` argv up to (but excluding) the `--db` value.
_LIMIT_ARGV = ("bars", "--start", "2019-01-01", "--end", "2026-07-30", "--db")


def _sampling_universe() -> tuple[UniverseMember, ...]:
    """A multi-sector universe whose sample is not its alphabetical head."""
    return tuple(
        UniverseMember(
            symbol=f"S{index:03d}",
            company_name=f"S{index:03d}",
            gics_sector=sector,
            source_symbol=f"S{index:03d}",
        )
        for index, sector in enumerate(
            ("Information Technology", "Health Care", "Financials") * 6
        )
    )


class _FixedClock:
    def now(self) -> datetime:
        return _NOW

    def today(self) -> date:
        return _NOW.date()


def _bar_row(symbol: str, day: date) -> dict[str, object]:
    return {
        "symbol": symbol,
        "date": day,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 1_000_000,
    }


class _RecordingProvider:
    """Fake `DataProvider` recording every batch it was asked to fetch."""

    def __init__(
        self,
        *,
        failing_symbols: frozenset[str] = frozenset(),
        non_finite_symbols: frozenset[str] = frozenset(),
        rows_by_symbol: dict[str, list[dict[str, object]]] | None = None,
        splits: tuple[tuple[str, date, float], ...] = (),
    ) -> None:
        self.calls: list[tuple[list[str], date, date]] = []
        self._failing_symbols = failing_symbols
        #: Symbols returned with a NaN close, standing in for a provider whose
        #: own normalization does not drop them (`data/base.py`'s contract).
        self._non_finite_symbols = non_finite_symbols
        #: Full per-symbol histories, for the rebuild path; symbols absent
        #: from it fall back to the single `_START` row.
        self._rows_by_symbol = rows_by_symbol or {}
        self._splits = splits

    def _rows_for(self, symbol: str) -> list[dict[str, object]]:
        if symbol in self._rows_by_symbol:
            return self._rows_by_symbol[symbol]
        row = _bar_row(symbol, _START)
        if symbol in self._non_finite_symbols:
            row = row | {"close": float("nan")}
        return [row]

    def get_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> BarFetchResult:
        self.calls.append((list(symbols), start, end))
        succeeded = [s for s in symbols if s not in self._failing_symbols]
        bars = pd.DataFrame(
            [row for symbol in succeeded for row in self._rows_for(symbol)]
        )
        failures = tuple(
            FetchFailure(symbol=symbol, reason="no data returned", retryable=True)
            for symbol in symbols
            if symbol in self._failing_symbols
        )
        actions = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "ex_date": ex_date,
                    "kind": "split",
                    "value": factor,
                }
                for symbol, ex_date, factor in self._splits
                if symbol in succeeded
            ],
            columns=list(ACTIONS_COLUMNS),
        )
        return BarFetchResult(bars=bars, failures=failures, actions=actions)

    def get_latest_bars(self, symbols: list[str], as_of: date) -> BarFetchResult:
        """Never used by the backfill path; present only to satisfy the port."""
        msg = f"backfill must not call get_latest_bars ({symbols}, {as_of})"
        raise AssertionError(msg)


class _CountingStore:
    """Wraps a real `MarketStore` to count and order its write calls."""

    def __init__(self, inner: MarketStore) -> None:
        self._inner = inner
        self.write_calls: list[pd.DataFrame] = []
        self.action_calls: list[pd.DataFrame] = []
        #: The order the two writers were invoked in, which is itself a
        #: contract: splits have to be recorded before the bars they re-base.
        self.call_order: list[str] = []

    def earliest_bar_dates(self, symbols: list[str]) -> dict[str, date]:
        return self._inner.earliest_bar_dates(symbols)

    def write_bars(self, df: pd.DataFrame) -> BarWriteResult:
        self.write_calls.append(df.copy())
        self.call_order.append("bars")
        return self._inner.write_bars(df)

    def write_corporate_actions(
        self, df: pd.DataFrame, *, provider: str, fetched_at: datetime
    ) -> None:
        self.action_calls.append(df.copy())
        self.call_order.append("actions")
        self._inner.write_corporate_actions(
            df, provider=provider, fetched_at=fetched_at
        )


@pytest.fixture
def market_store(tmp_path: Path) -> MarketStore:
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


def _deps(
    provider: _RecordingProvider,
    store: MarketStore | _CountingStore,
    sleeps: list[float],
) -> BarsBackfillDeps:
    return BarsBackfillDeps(
        data_provider=provider,
        market_store=store,  # type: ignore[arg-type]
        clock=_FixedClock(),
        provider_name="yfinance",
        sleep_fn=sleeps.append,
    )


class TestBackfillBarsChunking:
    def test_splits_symbols_into_chunks_of_the_configured_size(
        self, market_store: MarketStore
    ) -> None:
        symbols = [f"S{i:03d}" for i in range(SYMBOL_CHUNK_SIZE + 3)]
        provider = _RecordingProvider()

        backfill_bars(_deps(provider, market_store, []), symbols, _START, _END)

        assert [len(call[0]) for call in provider.calls] == [SYMBOL_CHUNK_SIZE, 3]

    def test_sleeps_between_chunks_but_not_before_the_first(
        self, market_store: MarketStore
    ) -> None:
        symbols = [f"S{i:03d}" for i in range(SYMBOL_CHUNK_SIZE * 2)]
        provider = _RecordingProvider()
        sleeps: list[float] = []

        backfill_bars(_deps(provider, market_store, sleeps), symbols, _START, _END)

        assert sleeps == [CHUNK_SLEEP_SECONDS]

    def test_requests_the_end_date_inclusively(self, market_store: MarketStore) -> None:
        provider = _RecordingProvider()

        backfill_bars(_deps(provider, market_store, []), ["AAA"], _START, _END)

        _, start, end = provider.calls[0]
        assert start == _START
        assert end == date(2026, 7, 31)

    def test_writes_every_chunk_in_a_single_write_bars_call(
        self, market_store: MarketStore
    ) -> None:
        symbols = [f"S{i:03d}" for i in range(SYMBOL_CHUNK_SIZE * 2)]
        counting = _CountingStore(market_store)
        provider = _RecordingProvider()

        backfill_bars(_deps(provider, counting, []), symbols, _START, _END)

        assert len(counting.write_calls) == 1
        assert len(counting.write_calls[0]) == len(symbols)

    def test_stamps_provider_and_fetch_time_on_written_rows(
        self, market_store: MarketStore
    ) -> None:
        counting = _CountingStore(market_store)

        backfill_bars(_deps(_RecordingProvider(), counting, []), ["AAA"], _START, _END)

        written = counting.write_calls[0]
        assert written["provider"].tolist() == ["yfinance"]
        assert written["fetched_at"].tolist() == [_NOW]

    def test_a_non_finite_bar_rejects_the_whole_backfill_write(
        self, market_store: MarketStore
    ) -> None:
        """The single `write_bars` call makes the store's fail-fast batch-wide.

        Issue #227: one symbol's NaN close aborts the backfill instead of
        being persisted, and nothing from the batch reaches Parquet — the
        operator reruns after the provider's normalization is fixed.
        """
        provider = _RecordingProvider(non_finite_symbols=frozenset({"BBB"}))

        with pytest.raises(NonFiniteBarsError, match="BBB"):
            backfill_bars(
                _deps(provider, market_store, []), ["AAA", "BBB"], _START, _END
            )

        assert not market_store.parquet_root.exists()

    def test_skips_the_write_entirely_when_nothing_was_fetched(
        self, market_store: MarketStore
    ) -> None:
        counting = _CountingStore(market_store)
        provider = _RecordingProvider(failing_symbols=frozenset({"AAA"}))

        result = backfill_bars(_deps(provider, counting, []), ["AAA"], _START, _END)

        assert counting.write_calls == []
        assert result.written_rows == 0


class TestBackfillBarsResume:
    def test_skips_symbols_already_covered_from_before_start(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    {
                        **_bar_row("AAA", date(2018, 12, 31)),
                        "provider": "yfinance",
                        "fetched_at": _NOW,
                    }
                ]
            )
        )
        provider = _RecordingProvider()

        result = backfill_bars(
            _deps(provider, market_store, []), ["AAA", "BBB"], _START, _END
        )

        assert result.skipped_symbols == ("AAA",)
        assert provider.calls[0][0] == ["BBB"]

    def test_skips_a_symbol_whose_first_bar_is_the_trading_day_after_start(
        self, market_store: MarketStore
    ) -> None:
        # `--start` is a calendar date the operator picks and the documented
        # example (2019-01-01) is a market holiday, so the oldest bar that can
        # exist is 2019-01-02. Requiring the stored history to reach *on or
        # before* `--start` would make resume dead for that invocation and
        # refetch the whole universe on every rerun.
        market_store.write_bars(
            pd.DataFrame(
                [
                    {
                        **_bar_row("AAA", _START + timedelta(days=1)),
                        "provider": "yfinance",
                        "fetched_at": _NOW,
                    }
                ]
            )
        )
        provider = _RecordingProvider()

        result = backfill_bars(_deps(provider, market_store, []), ["AAA"], _START, _END)

        assert result.skipped_symbols == ("AAA",)
        assert provider.calls == []

    def test_refetches_a_symbol_whose_first_bar_is_past_the_tolerance(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    {
                        **_bar_row(
                            "AAA", _START + timedelta(days=COVERAGE_TOLERANCE_DAYS + 1)
                        ),
                        "provider": "yfinance",
                        "fetched_at": _NOW,
                    }
                ]
            )
        )
        provider = _RecordingProvider()

        result = backfill_bars(_deps(provider, market_store, []), ["AAA"], _START, _END)

        assert result.skipped_symbols == ()
        assert provider.calls[0][0] == ["AAA"]

    def test_refetches_a_symbol_whose_history_starts_after_start(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    {
                        **_bar_row("AAA", date(2025, 6, 2)),
                        "provider": "yfinance",
                        "fetched_at": _NOW,
                    }
                ]
            )
        )
        provider = _RecordingProvider()

        result = backfill_bars(_deps(provider, market_store, []), ["AAA"], _START, _END)

        assert result.skipped_symbols == ()
        assert provider.calls[0][0] == ["AAA"]

    def test_makes_no_provider_call_when_every_symbol_is_covered(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    {
                        **_bar_row("AAA", date(2018, 12, 31)),
                        "provider": "yfinance",
                        "fetched_at": _NOW,
                    }
                ]
            )
        )
        provider = _RecordingProvider()

        result = backfill_bars(_deps(provider, market_store, []), ["AAA"], _START, _END)

        assert provider.calls == []
        assert result.skipped_symbols == ("AAA",)


class TestBackfillBarsFailSoft:
    def test_reports_failed_symbols_and_keeps_the_successful_ones(
        self, market_store: MarketStore
    ) -> None:
        provider = _RecordingProvider(failing_symbols=frozenset({"BBB"}))

        result = backfill_bars(
            _deps(provider, market_store, []), ["AAA", "BBB", "CCC"], _START, _END
        )

        assert [failure.symbol for failure in result.failures] == ["BBB"]
        assert result.fetched_symbols == ("AAA", "CCC")
        assert result.written_rows == 2

    def test_a_failing_chunk_does_not_stop_later_chunks(
        self, market_store: MarketStore
    ) -> None:
        symbols = [f"S{i:03d}" for i in range(SYMBOL_CHUNK_SIZE + 1)]
        provider = _RecordingProvider(failing_symbols=frozenset(symbols[:5]))

        result = backfill_bars(_deps(provider, market_store, []), symbols, _START, _END)

        assert len(provider.calls) == 2
        assert len(result.failures) == 5
        assert len(result.fetched_symbols) == len(symbols) - 5


class _RecordingEdgarClient:
    """Fake EDGAR client returning one record per symbol, or raising."""

    def __init__(self, *, failing_symbols: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple[str, datetime, int]] = []
        self._failing_symbols = failing_symbols

    def fetch_fundamentals(
        self, symbol: str, as_of: datetime, *, lookback_days: int
    ) -> list[FundamentalsRecord]:
        self.calls.append((symbol, as_of, lookback_days))
        if symbol in self._failing_symbols:
            msg = "EDGAR unavailable"
            raise OSError(msg)
        return [
            FundamentalsRecord(
                accession_no=f"{symbol}-0001",
                symbol=symbol,
                form="10-Q",
                fiscal_period_end=date(2019, 3, 31),
                filed_at=datetime(2019, 4, 30, tzinfo=UTC),
                revenue=1.0,
                net_income=1.0,
                fcf=1.0,
                equity=1.0,
                assets=1.0,
                shares=1.0,
                source_url="https://example.invalid/filing",
                fetched_at=_NOW,
            )
        ]


class TestBackfillFundamentals:
    def test_requests_the_full_window_between_start_and_as_of(
        self, market_store: MarketStore
    ) -> None:
        client = _RecordingEdgarClient()
        deps = FundamentalsBackfillDeps(
            edgar_client=client, market_store=market_store, clock=_FixedClock()
        )

        backfill_fundamentals(deps, ["AAA"], _START, _NOW.date())

        symbol, as_of, lookback_days = client.calls[0]
        assert symbol == "AAA"
        assert as_of.date() == _NOW.date()
        # `fetch_fundamentals` bounds the window at `as_of - lookback_days`
        # and `as_of` is an end-of-day instant, so asserting the raw day count
        # would let the lower bound land at the *end* of `--start` and drop
        # every filing made during that day. Assert the boundary instead: a
        # filing at 00:00 on `--start` must still be inside the window.
        earliest = as_of - timedelta(days=lookback_days)
        assert earliest < datetime.combine(_START, time.min, tzinfo=UTC)
        assert earliest >= datetime.combine(
            _START - timedelta(days=1), time.min, tzinfo=UTC
        )

    def test_persists_records_and_reports_failed_symbols(
        self, market_store: MarketStore
    ) -> None:
        client = _RecordingEdgarClient(failing_symbols=frozenset({"BBB"}))
        deps = FundamentalsBackfillDeps(
            edgar_client=client, market_store=market_store, clock=_FixedClock()
        )

        result = backfill_fundamentals(deps, ["AAA", "BBB", "CCC"], _START, _NOW.date())

        assert result.failed_symbols == ("BBB",)
        assert result.written_records == 2
        stored = market_store.read_fundamentals(_NOW.date())
        assert sorted(stored["symbol"]) == ["AAA", "CCC"]


class TestBackfillCli:
    def test_bars_command_writes_rows_for_explicit_symbols(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        provider = _RecordingProvider()
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        backfill_main(
            [
                "bars",
                "--start",
                "2019-01-01",
                "--end",
                "2026-07-30",
                "--db",
                str(tmp_path / "copilot.duckdb"),
                "--symbols",
                "aaa,bbb",
            ]
        )

        assert provider.calls[0][0] == ["AAA", "BBB"]
        assert "書き込み 2 行" in capsys.readouterr().out

    def test_bars_command_reports_failed_symbols(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        provider = _RecordingProvider(failing_symbols=frozenset({"BBB"}))
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        backfill_main(
            [
                "bars",
                "--start",
                "2019-01-01",
                "--end",
                "2026-07-30",
                "--db",
                str(tmp_path / "copilot.duckdb"),
                "--symbols",
                "AAA,BBB",
            ]
        )

        assert "失敗した銘柄: BBB" in capsys.readouterr().out

    def test_bars_command_resolves_the_universe_when_no_symbols_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _RecordingProvider()
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )
        # The real resolver reads `settings.universe.snapshot_path`, a
        # gitignored CSV that only exists on a machine that has run the
        # fetcher. What this test owns is the `--symbols`-absent branch and
        # `--limit`, not snapshot parsing, so the membership is injected.
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.get_sp500_universe",
            lambda _as_of, **_kwargs: tuple(
                UniverseMember(symbol, symbol, "Information Technology", symbol)
                for symbol in ("AAA", "BBB", "CCC", "DDD")
            ),
        )

        backfill_main(
            [
                "bars",
                "--start",
                "2019-01-01",
                "--end",
                "2026-07-30",
                "--db",
                str(tmp_path / "copilot.duckdb"),
                "--limit",
                "3",
            ]
        )

        assert len(provider.calls[0][0]) == 3

    def test_exits_non_zero_when_every_symbol_failed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Nothing stored, nothing fetched: a zero exit status here would let a
        # chained `copilot-backfill ... && copilot-backtest ...` run against a
        # database that is exactly as empty as before.
        provider = _RecordingProvider(failing_symbols=frozenset({"AAA", "BBB"}))
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        with pytest.raises(SystemExit) as excinfo:
            backfill_main(
                [
                    "bars",
                    "--start",
                    "2019-01-01",
                    "--end",
                    "2026-07-30",
                    "--db",
                    str(tmp_path / "copilot.duckdb"),
                    "--symbols",
                    "AAA,BBB",
                ]
            )

        assert excinfo.value.code == 1
        assert "全銘柄の取得に失敗" in capsys.readouterr().err

    def test_a_non_finite_bar_exits_one_with_a_single_stderr_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Issue #250 (folded into #249): the store's rejection is not a crash.

        `write_bars` rejects the whole batch before touching a partition, so
        the run is fatal either way — what changes is that the operator gets
        the one stderr line Issue #221 standardized instead of a traceback.
        """
        provider = _RecordingProvider(non_finite_symbols=frozenset({"BBB"}))
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        with pytest.raises(SystemExit) as excinfo:
            backfill_main(
                [
                    "bars",
                    "--start",
                    "2019-01-01",
                    "--end",
                    "2026-07-30",
                    "--db",
                    str(tmp_path / "copilot.duckdb"),
                    "--symbols",
                    "AAA,BBB",
                ]
            )

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert stderr.splitlines() == [stderr.strip()]
        assert "非有限" in stderr
        # Fail-fast, unchanged since Issue #227: nothing reached Parquet.
        assert not (tmp_path / "bars").exists()

    def test_rejects_a_start_after_end(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            backfill_main(
                [
                    "bars",
                    "--start",
                    "2026-08-01",
                    "--end",
                    "2026-07-30",
                    "--db",
                    str(tmp_path / "copilot.duckdb"),
                    "--symbols",
                    "AAA",
                ]
            )

    def test_limit_samples_the_universe_instead_of_its_alphabetical_head(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #206: the third `--limit` stops meaning "the N tickers from A".

        Warming only the A-side of the cache is what decides which symbols a
        later smoke run or backtest finds already fetched.
        """
        universe = _sampling_universe()
        provider = _RecordingProvider()
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.get_sp500_universe",
            lambda _as_of, **_kwargs: universe,
        )

        backfill_main([*_LIMIT_ARGV, str(tmp_path / "copilot.duckdb"), "--limit", "6"])

        requested = provider.calls[0][0]
        alphabetical_head = sorted(member.symbol for member in universe)[:6]
        assert len(requested) == 6
        assert sorted(requested) != alphabetical_head

    def test_limit_covers_the_same_symbols_as_backtest_and_daily(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All three `--limit`s share one sampler, hence one salt (Issue #206)."""
        universe = _sampling_universe()
        provider = _RecordingProvider()
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.get_sp500_universe",
            lambda _as_of, **_kwargs: universe,
        )

        backfill_main([*_LIMIT_ARGV, str(tmp_path / "copilot.duckdb"), "--limit", "6"])

        # `copilot-backtest` takes the sampler's output directly; `copilot-daily`
        # unions holdings into it (none here), so all three must agree.
        expected = list(select_universe_sample(universe, 6).symbols)
        assert sorted(provider.calls[0][0]) == expected
        assert select_symbols(universe, set(), 6) == expected

    def test_rejects_a_non_positive_limit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The CLI keeps its own `>= 1` contract and its own message: the shared
        # sampler treats `0` as "select nothing" and only rejects negatives.
        with pytest.raises(SystemExit):
            backfill_main(
                [
                    "bars",
                    "--start",
                    "2019-01-01",
                    "--end",
                    "2026-07-30",
                    "--db",
                    str(tmp_path / "copilot.duckdb"),
                    "--limit",
                    "0",
                ]
            )

        assert "--limit は1以上の整数で指定してください。" in capsys.readouterr().err

    def test_defaults_end_to_today_when_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _RecordingProvider()
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )
        monkeypatch.setattr("swing_copilot.pipeline.backfill.SystemClock", _FixedClock)

        backfill_main(
            [
                "bars",
                "--start",
                "2019-01-01",
                "--db",
                str(tmp_path / "copilot.duckdb"),
                "--symbols",
                "AAA",
            ]
        )

        assert provider.calls[0][2] == _NOW.date() + timedelta(days=1)

    def test_fundamentals_command_persists_records(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = _RecordingEdgarClient()
        monkeypatch.setenv("EDGAR_IDENTITY", "tomada tomada@example.invalid")
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.EdgarClient", lambda _identity: client
        )

        backfill_main(
            [
                "fundamentals",
                "--start",
                "2019-01-01",
                "--end",
                "2026-07-30",
                "--db",
                str(tmp_path / "copilot.duckdb"),
                "--symbols",
                "AAA",
            ]
        )

        assert "書き込み 1 件" in capsys.readouterr().out

    def test_fundamentals_command_reports_failed_symbols(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = _RecordingEdgarClient(failing_symbols=frozenset({"BBB"}))
        monkeypatch.setenv("EDGAR_IDENTITY", "tomada tomada@example.invalid")
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.EdgarClient", lambda _identity: client
        )

        backfill_main(
            [
                "fundamentals",
                "--start",
                "2019-01-01",
                "--end",
                "2026-07-30",
                "--db",
                str(tmp_path / "copilot.duckdb"),
                "--symbols",
                "AAA,BBB",
            ]
        )

        assert "失敗した銘柄: BBB" in capsys.readouterr().out

    def test_fundamentals_command_fails_fast_without_an_edgar_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EDGAR_IDENTITY", "")

        with pytest.raises(SystemExit):
            backfill_main(
                [
                    "fundamentals",
                    "--start",
                    "2019-01-01",
                    "--end",
                    "2026-07-30",
                    "--db",
                    str(tmp_path / "copilot.duckdb"),
                    "--symbols",
                    "AAA",
                ]
            )

    def test_fundamentals_command_does_not_leak_a_real_dotenv_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_run_fundamentals` calls bare `Secrets()`, not `load_secrets()` (Issue #387).

        The autouse `.env` guard in `tests/conftest.py` patches
        `Secrets.model_config` directly rather than `load_secrets`, so it has
        to cover this call site too even though this test never patches
        `load_secrets` or `EdgarClient`. Plant a `.env` with a real-looking
        value and `chdir` into it, with no `EDGAR_IDENTITY` exported: if the
        guard's `env_file=None` patch ever stopped applying here, `Secrets()`
        would read the planted file and the command would proceed instead of
        failing fast.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
        (tmp_path / ".env").write_text("EDGAR_IDENTITY=leaked tomada@example.invalid\n")

        with pytest.raises(SystemExit):
            backfill_main(
                [
                    "fundamentals",
                    "--start",
                    "2019-01-01",
                    "--end",
                    "2026-07-30",
                    "--db",
                    str(tmp_path / "copilot.duckdb"),
                    "--symbols",
                    "AAA",
                ]
            )


def _stored_row(symbol: str, day: date, close: float = 10.5) -> dict[str, object]:
    """A row as it sits in Parquet: the provider's shape plus the stamps."""
    return {
        **_bar_row(symbol, day),
        "close": close,
        "provider": "yfinance",
        "fetched_at": _NOW,
    }


def _tree_snapshot(root: Path) -> dict[str, tuple[int, float]]:
    """Every file under `root` with its size and mtime, for a no-write proof."""
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class TestBackfillBarsCorporateActions:
    """Issue #413: the splits a response reports are persisted beside the bars."""

    def test_records_the_actions_before_the_bars_they_rebase(
        self, market_store: MarketStore
    ) -> None:
        # Order is the contract: `read_bars` adjusts from `corporate_actions`,
        # so a split written after its own history would be invisible to the
        # very next read.
        counting = _CountingStore(market_store)
        provider = _RecordingProvider(splits=(("AAA", date(2024, 6, 3), 2.0),))

        backfill_bars(_deps(provider, counting, []), ["AAA"], _START, _END)

        assert counting.call_order == ["actions", "bars"]
        assert market_store.read_splits(["AAA"], as_of=_END)["AAA"][0].factor == 2.0

    def test_reports_the_symbols_the_store_quarantined(
        self, market_store: MarketStore
    ) -> None:
        # A re-fetch that contradicts a stored raw close by more than the
        # correction tolerance is a change of basis, not a correction: the
        # symbol is skipped, the rest of the batch is written, and the
        # operator is told — the same fail-soft shape as a fetch failure.
        stored_day = date(2025, 6, 2)
        market_store.write_bars(
            pd.DataFrame(
                [_stored_row("AAA", stored_day), _stored_row("BBB", stored_day)]
            )
        )
        provider = _RecordingProvider(
            rows_by_symbol={
                "AAA": [{**_bar_row("AAA", stored_day), "close": 20.0}],
                "BBB": [{**_bar_row("BBB", stored_day), "close": 10.53}],
            }
        )

        result = backfill_bars(
            _deps(provider, market_store, []), ["AAA", "BBB"], _START, _END
        )

        assert result.quarantined_symbols == ("AAA",)
        stored = market_store.read_raw_bars(["AAA", "BBB"])
        assert stored["close"].tolist() == pytest.approx([10.5, 10.53])


class TestRebuildBars:
    """Issue #413: the one sanctioned way to change a stored symbol's basis."""

    def _deps_for(
        self, provider: _RecordingProvider, store: MarketStore
    ) -> BarsBackfillDeps:
        return _deps(provider, store, [])

    def test_replaces_the_symbols_rows_in_every_year_partition(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2019, 1, 2)),
                    _stored_row("AAA", date(2020, 1, 2)),
                ]
            )
        )
        provider = _RecordingProvider(
            rows_by_symbol={
                "AAA": [{**_bar_row("AAA", date(2021, 1, 4)), "close": 20.0}]
            }
        )

        result = rebuild_bars(self._deps_for(provider, market_store), ["AAA"])

        assert result.replaced_symbols == ("AAA",)
        # Not merged with what was there: the 2019 and 2020 rows are gone.
        stored = market_store.read_raw_bars(["AAA"])
        assert stored["date"].tolist() == [date(2021, 1, 4)]
        assert stored["close"].tolist() == pytest.approx([20.0])

    def test_requests_the_whole_history_up_to_tomorrow(
        self, market_store: MarketStore
    ) -> None:
        provider = _RecordingProvider()

        rebuild_bars(self._deps_for(provider, market_store), ["AAA"])

        _, start, end = provider.calls[0]
        assert start == REBUILD_START
        # The provider's end is exclusive, so today's own bar is included.
        assert end == _NOW.date() + timedelta(days=1)

    def test_a_rejected_symbol_keeps_the_rows_it_already_had(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2019, 1, 2)),
                    _stored_row("BBB", date(2019, 1, 2)),
                ]
            )
        )
        provider = _RecordingProvider(
            failing_symbols=frozenset({"BBB"}),
            rows_by_symbol={
                "AAA": [{**_bar_row("AAA", date(2021, 1, 4)), "close": 20.0}]
            },
        )

        result = rebuild_bars(self._deps_for(provider, market_store), ["AAA", "BBB"])

        assert (result.replaced_symbols, result.rejected_symbols) == (
            ("AAA",),
            ("BBB",),
        )
        # Old rows are better than none: a symbol whose response could not be
        # normalized is left exactly as it was, and named in the result.
        assert market_store.read_raw_bars(["BBB"])["date"].tolist() == [
            date(2019, 1, 2)
        ]

    def test_stamps_the_format_marker_onto_an_unmigrated_store(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(pd.DataFrame([_stored_row("AAA", date(2019, 1, 2))]))
        marker = market_store.parquet_root / "_format.json"
        marker.unlink()
        provider = _RecordingProvider(
            rows_by_symbol={
                "AAA": [{**_bar_row("AAA", date(2021, 1, 4)), "close": 20.0}]
            }
        )

        rebuild_bars(self._deps_for(provider, market_store), ["AAA"])

        assert json.loads(marker.read_text(encoding="utf-8")) == {
            "basis": "raw",
            "version": 2,
        }

    def test_upserts_the_corporate_actions_it_fetched(
        self, market_store: MarketStore
    ) -> None:
        provider = _RecordingProvider(splits=(("AAA", date(2020, 6, 1), 2.0),))

        rebuild_bars(self._deps_for(provider, market_store), ["AAA"])

        splits = market_store.read_splits(["AAA"], as_of=date(2026, 12, 31))["AAA"]
        assert [(split.ex_date, split.factor) for split in splits] == [
            (date(2020, 6, 1), 2.0)
        ]


class TestCheckBars:
    """The read-only audit: never writes, and names the session to look at."""

    def _plant(
        self,
        market_store: MarketStore,
        closes: dict[date, float],
        *,
        split: float | None = None,
    ) -> None:
        """Plant a series past `write_bars`' gate, the way a bad day left one.

        `split` records the corporate action the audit reads a flip against:
        a step is only a basis flip if some split is that size (Issue #421),
        so a series planted without one is volatility by definition.
        """
        if split is not None:
            market_store.write_corporate_actions(
                pd.DataFrame(
                    [
                        {
                            "symbol": "AAA",
                            "ex_date": date(2026, 8, 11),
                            "kind": "split",
                            "value": split,
                        }
                    ]
                ),
                provider="yfinance",
                fetched_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        market_store.replace_symbol_bars(
            ["AAA"],
            pd.DataFrame(
                [_stored_row("AAA", day, close) for day, close in closes.items()]
            ),
        )

    def _series(self, closes: list[float]) -> dict[date, float]:
        return {
            date(2026, 7, 1) + timedelta(days=offset): close
            for offset, close in enumerate(closes)
        }

    def test_reports_ok_for_a_single_basis_store(
        self, market_store: MarketStore
    ) -> None:
        self._plant(market_store, self._series([100.0, 101.0, 99.0, 100.0]))

        result = check_bars(market_store, [])

        assert (result.format_problem, result.findings) == (None, ())
        assert result.scanned_symbols == ("AAA",)

    def test_names_the_first_session_quoted_on_the_other_basis(
        self, market_store: MarketStore
    ) -> None:
        # The Issue #413 shape: one adjusted row dropped into an otherwise
        # unadjusted series. The jump down and the jump back multiply to 1,
        # and both are the size of the symbol's own 2:1 split.
        self._plant(
            market_store, self._series([100.0, 100.0, 50.0, 100.0, 100.0]), split=2.0
        )

        result = check_bars(market_store, ["AAA"])

        assert result.format_problem is None
        assert [(f.symbol, f.first_jump_date) for f in result.findings] == [
            ("AAA", date(2026, 7, 3))
        ]

    def test_the_same_series_is_no_finding_for_a_symbol_with_no_split(
        self, market_store: MarketStore
    ) -> None:
        """Issue #421: `^VIX` halving and doubling back is not a mixed basis."""
        self._plant(market_store, self._series([100.0, 100.0, 50.0, 100.0, 100.0]))

        result = check_bars(market_store, ["AAA"])

        assert (result.format_problem, result.findings) == (None, ())

    def test_a_flip_that_matches_no_split_of_the_symbol_is_no_finding(
        self, market_store: MarketStore
    ) -> None:
        """A 2x round trip under a 3:1 split explains nothing about the basis."""
        self._plant(
            market_store, self._series([100.0, 100.0, 50.0, 100.0, 100.0]), split=3.0
        )

        result = check_bars(market_store, ["AAA"])

        assert result.findings == ()

    def test_a_symbol_with_no_stored_rows_is_simply_not_a_finding(
        self, market_store: MarketStore
    ) -> None:
        self._plant(market_store, self._series([100.0, 101.0]))

        result = check_bars(market_store, ["AAA", "ZZZ"])

        assert result.scanned_symbols == ("AAA", "ZZZ")
        assert result.findings == ()

    def test_reports_a_store_that_predates_the_raw_bar_model(
        self, market_store: MarketStore
    ) -> None:
        self._plant(market_store, self._series([100.0, 101.0]))
        (market_store.parquet_root / "_format.json").unlink()

        result = check_bars(market_store, ["AAA"])

        assert result.format_problem is not None
        assert "copilot-backfill rebuild" in result.format_problem
        # The scan is not attempted at all: those partitions cannot be read
        # as raw, so any finding from them would be meaningless.
        assert result.findings == ()

    def test_reports_a_symbol_missing_a_session_the_rest_of_the_store_has(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1)),
                    _stored_row("AAA", date(2026, 7, 3)),
                    _stored_row("BBB", date(2026, 7, 1)),
                    _stored_row("BBB", date(2026, 7, 2)),
                    _stored_row("BBB", date(2026, 7, 3)),
                    _stored_row("CCC", date(2026, 7, 1)),
                    _stored_row("CCC", date(2026, 7, 2)),
                    _stored_row("CCC", date(2026, 7, 3)),
                ]
            )
        )

        result = check_bars(market_store, [])

        assert [(f.symbol, f.missing_dates) for f in result.missing_sessions] == [
            ("AAA", (date(2026, 7, 2),))
        ]

    def test_does_not_flag_a_date_no_symbol_traded(
        self, market_store: MarketStore
    ) -> None:
        """A real market holiday: nobody has a bar, so it is not a session."""
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1)),
                    _stored_row("AAA", date(2026, 7, 3)),
                    _stored_row("BBB", date(2026, 7, 1)),
                    _stored_row("BBB", date(2026, 7, 3)),
                ]
            )
        )

        result = check_bars(market_store, [])

        assert result.missing_sessions == ()

    def test_does_not_flag_a_session_before_a_symbol_first_bar(
        self, market_store: MarketStore
    ) -> None:
        """An IPO: sessions before the symbol's own listed span are not gaps."""
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1)),
                    _stored_row("AAA", date(2026, 7, 2)),
                    _stored_row("AAA", date(2026, 7, 3)),
                    _stored_row("BBB", date(2026, 7, 1)),
                    _stored_row("BBB", date(2026, 7, 2)),
                    _stored_row("BBB", date(2026, 7, 3)),
                    _stored_row("CCC", date(2026, 7, 3)),
                ]
            )
        )

        result = check_bars(market_store, [])

        assert result.missing_sessions == ()

    def test_does_not_flag_a_session_after_a_symbol_last_bar(
        self, market_store: MarketStore
    ) -> None:
        """A delisting: sessions after the symbol's own listed span are not gaps."""
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1)),
                    _stored_row("BBB", date(2026, 7, 1)),
                    _stored_row("BBB", date(2026, 7, 2)),
                    _stored_row("BBB", date(2026, 7, 3)),
                    _stored_row("CCC", date(2026, 7, 1)),
                    _stored_row("CCC", date(2026, 7, 2)),
                    _stored_row("CCC", date(2026, 7, 3)),
                ]
            )
        )

        result = check_bars(market_store, [])

        assert result.missing_sessions == ()

    def test_does_not_treat_a_lone_bar_as_a_session(
        self, market_store: MarketStore
    ) -> None:
        """Issue #421's lesson applied to sessions.

        One stray bar out of many listed symbols must not manufacture a
        session the rest get flagged for missing.
        """
        common_dates = [
            date(2026, 7, 1),
            date(2026, 7, 2),
            date(2026, 7, 3),
            date(2026, 7, 5),
        ]
        rows = [
            _stored_row(symbol, day)
            for symbol in ("AAA", "BBB", "CCC")
            for day in common_dates
        ]
        rows.append(_stored_row("AAA", date(2026, 7, 4)))
        market_store.write_bars(pd.DataFrame(rows))

        result = check_bars(market_store, [])

        assert result.missing_sessions == ()

    def test_skips_the_bond_calendar_symbol_when_enumerating_the_store(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1)),
                    _stored_row("AAA", date(2026, 7, 2)),
                    _stored_row("AAA", date(2026, 7, 3)),
                    _stored_row("BBB", date(2026, 7, 1)),
                    _stored_row("BBB", date(2026, 7, 2)),
                    _stored_row("BBB", date(2026, 7, 3)),
                    _stored_row("^TNX", date(2026, 7, 1)),
                    _stored_row("^TNX", date(2026, 7, 3)),
                ]
            )
        )

        result = check_bars(market_store, [])

        assert "^TNX" in result.scanned_symbols
        assert result.missing_sessions == ()

    def test_scans_the_bond_calendar_symbol_when_named_explicitly(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1)),
                    _stored_row("AAA", date(2026, 7, 2)),
                    _stored_row("AAA", date(2026, 7, 3)),
                    _stored_row("BBB", date(2026, 7, 1)),
                    _stored_row("BBB", date(2026, 7, 2)),
                    _stored_row("BBB", date(2026, 7, 3)),
                    _stored_row("^TNX", date(2026, 7, 1)),
                    _stored_row("^TNX", date(2026, 7, 3)),
                ]
            )
        )

        result = check_bars(market_store, ["^TNX"])

        assert [(f.symbol, f.missing_dates) for f in result.missing_sessions] == [
            ("^TNX", (date(2026, 7, 2),))
        ]

    def test_builds_the_session_calendar_from_the_whole_store_even_with_symbols(
        self, market_store: MarketStore
    ) -> None:
        """Auditing one symbol must not shrink the calendar down to it."""
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1)),
                    _stored_row("AAA", date(2026, 7, 3)),
                    _stored_row("BBB", date(2026, 7, 1)),
                    _stored_row("BBB", date(2026, 7, 2)),
                    _stored_row("BBB", date(2026, 7, 3)),
                    _stored_row("CCC", date(2026, 7, 1)),
                    _stored_row("CCC", date(2026, 7, 2)),
                    _stored_row("CCC", date(2026, 7, 3)),
                ]
            )
        )

        result = check_bars(market_store, ["AAA"])

        assert [(f.symbol, f.missing_dates) for f in result.missing_sessions] == [
            ("AAA", (date(2026, 7, 2),))
        ]

    def test_reports_no_gap_for_a_complete_store(
        self, market_store: MarketStore
    ) -> None:
        market_store.write_bars(
            pd.DataFrame(
                [
                    _stored_row(symbol, day)
                    for symbol in ("AAA", "BBB")
                    for day in (date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3))
                ]
            )
        )

        result = check_bars(market_store, [])

        assert result.missing_sessions == ()

    def test_writes_nothing_at_all(
        self, market_store: MarketStore, tmp_path: Path
    ) -> None:
        self._plant(
            market_store, self._series([100.0, 100.0, 50.0, 100.0, 100.0]), split=2.0
        )
        # A second symbol with a real session gap, so the missing-session
        # scan this test guards has actual work to do (Issue #449) --
        # extending the existing "writes nothing" pin rather than adding a
        # parallel one.
        market_store.replace_symbol_bars(
            ["BBB"],
            pd.DataFrame(
                [
                    _stored_row("BBB", date(2026, 7, 1)),
                    _stored_row("BBB", date(2026, 7, 3)),
                ]
            ),
        )
        # Read-only, as the CLI opens it: a write connection would ensure its
        # tables on open and touch the database file (Issue #421).
        auditor = MarketStore(
            Database(tmp_path / "copilot.duckdb", read_only=True),
            parquet_root=market_store.parquet_root,
        )
        before = _tree_snapshot(tmp_path)

        result = check_bars(auditor, [])

        # The scan did real work (the mixed-basis finding plus a genuine
        # session gap), and still touched nothing.
        assert result.findings != ()
        assert result.missing_sessions != ()
        # The DuckDB file included: opened for the splits, but byte- and
        # mtime-identical afterwards.
        assert _tree_snapshot(tmp_path) == before

    def test_an_empty_store_never_opens_the_database_at_all(
        self, tmp_path: Path
    ) -> None:
        """Nothing stored means no chunk to scan, so no split to look up."""
        store = MarketStore(
            Database(tmp_path / "copilot.duckdb", read_only=True),
            parquet_root=tmp_path / "bars",
        )

        result = check_bars(store, [])

        assert (result.scanned_symbols, result.findings) == ((), ())
        assert result.missing_sessions == ()
        assert not (tmp_path / "copilot.duckdb").exists()


class TestRebuildAndCheckCli:
    def test_rebuild_reports_replaced_and_rejected_symbols(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        provider = _RecordingProvider(failing_symbols=frozenset({"BBB"}))
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        backfill_main(
            [
                "rebuild",
                "--db",
                str(tmp_path / "copilot.duckdb"),
                "--symbols",
                "aaa,bbb",
            ]
        )

        out = capsys.readouterr().out
        assert "rebuild: 対象 2 銘柄 / 置換 1 / 拒否 1 / 書き込み 1 行" in out
        assert "既存行を維持した銘柄: BBB" in out

    def test_rebuild_exits_non_zero_and_writes_no_marker_when_all_fail(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Nothing was replaced, so the store still holds whatever basis it
        # had — stamping it "raw" would bless history nobody rebuilt.
        provider = _RecordingProvider(failing_symbols=frozenset({"AAA"}))
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        with pytest.raises(SystemExit) as excinfo:
            backfill_main(
                [
                    "rebuild",
                    "--db",
                    str(tmp_path / "copilot.duckdb"),
                    "--symbols",
                    "AAA",
                ]
            )

        assert excinfo.value.code == 1
        assert "全銘柄の取得に失敗" in capsys.readouterr().err
        assert not (tmp_path / "bars" / "_format.json").exists()

    def test_check_prints_ok_for_a_clean_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = MarketStore(
            Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
        )
        store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1), 100.0),
                    _stored_row("AAA", date(2026, 7, 2), 101.0),
                ]
            )
        )

        backfill_main(["check", "--db", str(tmp_path / "copilot.duckdb")])

        out = capsys.readouterr().out
        assert "形式マーカー: ok" in out
        assert "check: ok（対象 1 銘柄、混在署名なし、欠損セッションなし）" in out

    def test_check_lists_every_symbol_with_a_mixed_basis_series(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = MarketStore(
            Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
        )
        store.write_corporate_actions(
            pd.DataFrame(
                [
                    {
                        "symbol": "AAA",
                        "ex_date": date(2026, 8, 11),
                        "kind": "split",
                        "value": 2.0,
                    }
                ]
            ),
            provider="yfinance",
            fetched_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
        store.replace_symbol_bars(
            ["AAA"],
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1) + timedelta(days=offset), close)
                    for offset, close in enumerate([100.0, 100.0, 50.0, 100.0, 100.0])
                ]
            ),
        )

        backfill_main(
            ["check", "--db", str(tmp_path / "copilot.duckdb"), "--symbols", "aaa"]
        )

        out = capsys.readouterr().out
        assert "混在署名 1 銘柄" in out
        assert "混在署名: AAA（最初のジャンプ 2026-07-03）" in out

    def test_check_reports_ok_for_a_run_that_predates_every_split(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Issue #425: an AIG-type shape is not reported as a finding.

        Same reversing arithmetic as the mixed-basis case above, but the
        only split sized for it has an `ex_date` before the run instead of
        after, so it is not eligible and the store reports clean.
        """
        store = MarketStore(
            Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
        )
        store.write_corporate_actions(
            pd.DataFrame(
                [
                    {
                        "symbol": "AAA",
                        "ex_date": date(2008, 1, 1),
                        "kind": "split",
                        "value": 2.0,
                    }
                ]
            ),
            provider="yfinance",
            fetched_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
        store.replace_symbol_bars(
            ["AAA"],
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 7, 1) + timedelta(days=offset), close)
                    for offset, close in enumerate([100.0, 100.0, 50.0, 100.0, 100.0])
                ]
            ),
        )

        backfill_main(
            ["check", "--db", str(tmp_path / "copilot.duckdb"), "--symbols", "aaa"]
        )

        out = capsys.readouterr().out
        assert "check: ok（対象 1 銘柄、混在署名なし、欠損セッションなし）" in out

    def test_rebuild_reports_the_sessions_the_refetch_dropped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        db_path = tmp_path / "copilot.duckdb"
        store = MarketStore(Database(db_path), parquet_root=tmp_path / "bars")
        store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 8, 7)),
                    _stored_row("AAA", date(2026, 8, 10)),
                    _stored_row("AAA", date(2026, 8, 11)),
                ]
            )
        )
        provider = _RecordingProvider(
            rows_by_symbol={
                "AAA": [
                    _bar_row("AAA", date(2026, 8, 7)),
                    _bar_row("AAA", date(2026, 8, 11)),
                ]
            }
        )
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        backfill_main(["rebuild", "--db", str(db_path), "--symbols", "AAA"])

        out = capsys.readouterr().out
        assert (
            "供給元が返さなくなったセッション（rebuild により削除済み）: "
            "AAA（2026-08-10）" in out
        )

    def test_rebuild_exits_zero_when_sessions_were_dropped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Design decision 1.

        A dropped session never quarantines or fails the run, so `rebuild`
        exits 0 even though it reports one.
        """
        db_path = tmp_path / "copilot.duckdb"
        store = MarketStore(Database(db_path), parquet_root=tmp_path / "bars")
        store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 8, 7)),
                    _stored_row("AAA", date(2026, 8, 10)),
                    _stored_row("AAA", date(2026, 8, 11)),
                ]
            )
        )
        provider = _RecordingProvider(
            rows_by_symbol={
                "AAA": [
                    _bar_row("AAA", date(2026, 8, 7)),
                    _bar_row("AAA", date(2026, 8, 11)),
                ]
            }
        )
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        # Would raise SystemExit if `rebuild` treated the drop as a failure.
        backfill_main(["rebuild", "--db", str(db_path), "--symbols", "AAA"])


class TestBarsCliQuarantineReport:
    def test_bars_command_names_the_symbols_the_store_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store = MarketStore(
            Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
        )
        stored_day = date(2025, 6, 2)
        store.write_bars(pd.DataFrame([_stored_row("AAA", stored_day)]))
        provider = _RecordingProvider(
            rows_by_symbol={"AAA": [{**_bar_row("AAA", stored_day), "close": 20.0}]}
        )
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        backfill_main(
            [
                "bars",
                "--start",
                "2019-01-01",
                "--end",
                "2026-07-30",
                "--db",
                str(tmp_path / "copilot.duckdb"),
                "--symbols",
                "AAA",
            ]
        )

        assert "隔離した銘柄: AAA" in capsys.readouterr().out

    def test_bars_backfill_reports_dropped_sessions_without_quarantining(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Issue #449.

        The daily/backfill write path never loses the row, so its wording
        must say "kept", never "rebuild".
        """
        store = MarketStore(
            Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
        )
        store.write_bars(
            pd.DataFrame(
                [
                    _stored_row("AAA", date(2026, 8, 7)),
                    _stored_row("AAA", date(2026, 8, 10)),
                    _stored_row("AAA", date(2026, 8, 11)),
                ]
            )
        )
        provider = _RecordingProvider(
            rows_by_symbol={
                "AAA": [
                    _bar_row("AAA", date(2026, 8, 7)),
                    _bar_row("AAA", date(2026, 8, 11)),
                ]
            }
        )
        monkeypatch.setattr(
            "swing_copilot.pipeline.backfill.YFinanceProvider", lambda: provider
        )

        backfill_main(
            [
                "bars",
                "--start",
                "2019-01-01",
                "--end",
                "2026-08-15",
                "--db",
                str(tmp_path / "copilot.duckdb"),
                "--symbols",
                "AAA",
            ]
        )

        out = capsys.readouterr().out
        assert (
            "供給元が返さなくなったセッション（既存行は保持）: AAA（2026-08-10）" in out
        )
        assert "隔離した銘柄" not in out
        # The old row is still there -- `write_bars` never lost it.
        assert date(2026, 8, 10) in store.read_raw_bars(["AAA"])["date"].tolist()


class TestCheckCliUnmigratedStore:
    def test_check_reports_the_missing_marker_and_stops(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = MarketStore(
            Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
        )
        store.write_bars(pd.DataFrame([_stored_row("AAA", date(2026, 7, 1))]))
        (tmp_path / "bars" / "_format.json").unlink()

        backfill_main(["check", "--db", str(tmp_path / "copilot.duckdb")])

        out = capsys.readouterr().out
        assert "形式マーカー: NG" in out
        assert "copilot-backfill rebuild" in out
        assert "混在署名" not in out


def _seed_run(database: Database, run_id: UUID, run_date: date) -> None:
    """Register one `runs` row, the entry_price audit's date source.

    Through `tests.support.runs.seed_run` (Issue #398's public write path)
    rather than a hand-written raw SQL insert against the `runs` table, which
    `test_quality_contracts.py` bans outside `tests/storage/`/`tests/support/`.
    """
    support_seed_run(StateStore(database), run_id, run_date)


def _seed_frozen_entry_price(
    database: Database, run_id: UUID, symbol: str, entry_price: float | None
) -> None:
    """Insert one `risk_assessments` row -- the audit's own read target."""
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO risk_assessments (
                run_id, symbol, status, entry_price,
                reasons_json, warnings_json, sizing_warnings_json
            ) VALUES (?, ?, 'approved', ?, '[]', '[]', '[]')
            """,
            [str(run_id), symbol, entry_price],
        )


class TestCheckEntryPrices:
    """Issue #427: `risk_assessments.entry_price` audited against its own day's raw bar."""

    RUN_DATE = date(2026, 8, 19)
    RUN_ID = UUID("66666666-6666-6666-6666-666666666666")

    def _deps(self, tmp_path: Path) -> tuple[Database, MarketStore]:
        database = Database(tmp_path / "copilot.duckdb")
        StateStore(database).init_schema()
        return database, MarketStore(database, parquet_root=tmp_path / "bars")

    def test_a_matching_entry_price_is_not_a_finding(self, tmp_path: Path) -> None:
        database, store = self._deps(tmp_path)
        _seed_run(database, self.RUN_ID, self.RUN_DATE)
        _seed_frozen_entry_price(database, self.RUN_ID, "APH", 100.0)
        store.write_bars(pd.DataFrame([_stored_row("APH", self.RUN_DATE, 100.0)]))

        result = check_entry_prices(database, store, [])

        assert result.scanned_rows == 1
        assert result.findings == ()

    def test_the_aph_shape_frozen_at_double_the_raw_close_is_a_finding(
        self, tmp_path: Path
    ) -> None:
        database, store = self._deps(tmp_path)
        _seed_run(database, self.RUN_ID, self.RUN_DATE)
        _seed_frozen_entry_price(database, self.RUN_ID, "APH", 156.039993)
        store.write_bars(pd.DataFrame([_stored_row("APH", self.RUN_DATE, 78.019997)]))

        result = check_entry_prices(database, store, [])

        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.symbol == "APH"
        assert finding.run_date == self.RUN_DATE
        assert finding.run_id == self.RUN_ID
        assert finding.frozen_entry_price == pytest.approx(156.039993)
        assert finding.bar_close == pytest.approx(78.019997)

    def test_a_rounding_level_difference_is_not_a_finding(self, tmp_path: Path) -> None:
        database, store = self._deps(tmp_path)
        _seed_run(database, self.RUN_ID, self.RUN_DATE)
        # 0.01 on a 100.00 close is 0.01%, well inside the 0.5% tolerance.
        _seed_frozen_entry_price(database, self.RUN_ID, "APH", 100.01)
        store.write_bars(pd.DataFrame([_stored_row("APH", self.RUN_DATE, 100.0)]))

        result = check_entry_prices(database, store, [])

        assert result.findings == ()
        assert result.unresolved_rows == 0

    def test_a_missing_same_day_bar_is_unresolved_not_a_finding(
        self, tmp_path: Path
    ) -> None:
        database, store = self._deps(tmp_path)
        _seed_run(database, self.RUN_ID, self.RUN_DATE)
        _seed_frozen_entry_price(database, self.RUN_ID, "APH", 100.0)
        # No bars stored for APH at all.

        result = check_entry_prices(database, store, [])

        assert result.scanned_rows == 1
        assert result.findings == ()
        assert result.unresolved_rows == 1

    def test_a_split_after_the_run_date_never_produces_a_finding(
        self, tmp_path: Path
    ) -> None:
        """The audit never reads splits at all (design decision, Issue #427).

        A regression guard against the factor-driven false positives #425
        tuned `check_bars` for: this audit has no equivalent tuning because it
        has no split-based comparison to tune in the first place.
        """
        database, store = self._deps(tmp_path)
        _seed_run(database, self.RUN_ID, self.RUN_DATE)
        _seed_frozen_entry_price(database, self.RUN_ID, "APH", 100.0)
        store.write_bars(pd.DataFrame([_stored_row("APH", self.RUN_DATE, 100.0)]))
        store.write_corporate_actions(
            pd.DataFrame(
                [
                    {
                        "symbol": "APH",
                        "ex_date": self.RUN_DATE + timedelta(days=5),
                        "kind": "split",
                        "value": 2.0,
                    }
                ]
            ),
            provider="yfinance",
            fetched_at=_NOW,
        )

        result = check_entry_prices(database, store, [])

        assert result.findings == ()

    def test_a_store_with_no_frozen_rows_returns_an_all_zero_result(
        self, tmp_path: Path
    ) -> None:
        database, store = self._deps(tmp_path)

        result = check_entry_prices(database, store, [])

        assert (result.scanned_rows, result.scanned_symbols) == (0, ())
        assert (result.findings, result.unresolved_rows) == ((), 0)

    def test_writes_nothing_at_all(self, tmp_path: Path) -> None:
        database, store = self._deps(tmp_path)
        _seed_run(database, self.RUN_ID, self.RUN_DATE)
        _seed_frozen_entry_price(database, self.RUN_ID, "APH", 156.039993)
        store.write_bars(pd.DataFrame([_stored_row("APH", self.RUN_DATE, 78.019997)]))
        before = _tree_snapshot(tmp_path)

        auditor_db = Database(tmp_path / "copilot.duckdb", read_only=True)
        auditor_store = MarketStore(auditor_db, parquet_root=tmp_path / "bars")
        result = check_entry_prices(auditor_db, auditor_store, [])

        # The scan did real work (a genuine finding), and still touched nothing.
        assert result.findings != ()
        assert _tree_snapshot(tmp_path) == before


class TestCheckCliEntryPriceAudit:
    """Issue #427: `check`'s second audit, appended after the bar audit."""

    RUN_DATE = date(2026, 8, 19)
    RUN_ID = UUID("77777777-7777-7777-7777-777777777777")

    def _seed(self, tmp_path: Path, rows: dict[str, tuple[float, float]]) -> None:
        """Seed one run plus a frozen entry_price/raw bar pair per symbol.

        `rows` maps `symbol -> (frozen_entry_price, raw_bar_close)`.
        """
        database = Database(tmp_path / "copilot.duckdb")
        StateStore(database).init_schema()
        store = MarketStore(database, parquet_root=tmp_path / "bars")
        _seed_run(database, self.RUN_ID, self.RUN_DATE)
        for symbol, (entry_price, _) in rows.items():
            _seed_frozen_entry_price(database, self.RUN_ID, symbol, entry_price)
        store.write_bars(
            pd.DataFrame(
                [
                    _stored_row(symbol, self.RUN_DATE, bar_close)
                    for symbol, (_, bar_close) in rows.items()
                ]
            )
        )

    def test_prints_ok_when_no_row_disagrees(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._seed(tmp_path, {"AAA": (100.0, 100.0)})

        backfill_main(["check", "--db", str(tmp_path / "copilot.duckdb")])

        out = capsys.readouterr().out
        assert "entry_price: ok（対象 1 行 / 1 銘柄、基準ずれなし）" in out

    def test_lists_every_basis_mismatch_and_the_remediation_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._seed(tmp_path, {"APH": (156.039993, 78.019997)})

        backfill_main(["check", "--db", str(tmp_path / "copilot.duckdb")])

        out = capsys.readouterr().out
        assert "entry_price: 対象 1 行 / 1 銘柄 / 基準ずれ 1 行" in out
        assert (
            "基準ずれ: APH 2026-08-19（凍結 156.039993 / 生バー終値 78.019997、"
            "比 2.0000）" in out
        )
        assert (
            "基準ずれの建玉は `copilot-track rebuild --symbol <SYMBOL>` "
            "で是正すること（凍結値ではなく同日バー終値が使われる）。" in out
        )

    def test_findings_do_not_change_the_exit_code(self, tmp_path: Path) -> None:
        self._seed(tmp_path, {"APH": (156.039993, 78.019997)})

        # Would raise SystemExit if a finding turned `check` non-zero.
        backfill_main(["check", "--db", str(tmp_path / "copilot.duckdb")])

    def test_an_unmigrated_store_skips_the_entry_price_section(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._seed(tmp_path, {"APH": (156.039993, 78.019997)})
        (tmp_path / "bars" / "_format.json").unlink()

        backfill_main(["check", "--db", str(tmp_path / "copilot.duckdb")])

        out = capsys.readouterr().out
        assert "形式マーカー: NG" in out
        assert "entry_price" not in out

    def test_a_store_with_no_risk_assessments_table_reports_zero_rows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = MarketStore(
            Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
        )
        store.write_bars(pd.DataFrame([_stored_row("AAA", self.RUN_DATE, 100.0)]))

        backfill_main(["check", "--db", str(tmp_path / "copilot.duckdb")])

        out = capsys.readouterr().out
        assert "entry_price: ok（対象 0 行 / 0 銘柄、基準ずれなし）" in out

    def test_symbols_narrows_the_entry_price_audit_too(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._seed(tmp_path, {"AAA": (200.0, 100.0), "BBB": (50.0, 50.0)})

        backfill_main(
            ["check", "--db", str(tmp_path / "copilot.duckdb"), "--symbols", "aaa"]
        )

        out = capsys.readouterr().out
        assert "基準ずれ 1 行" in out
        assert "基準ずれ: AAA" in out
        assert "BBB" not in out
