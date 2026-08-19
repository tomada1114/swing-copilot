"""`copilot-backfill` contract tests: chunking, resume, fail-soft, single write."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from swing_copilot.data.base import BarFetchResult, FetchFailure
from swing_copilot.pipeline.backfill import (
    CHUNK_SLEEP_SECONDS,
    COVERAGE_TOLERANCE_DAYS,
    SYMBOL_CHUNK_SIZE,
    BarsBackfillDeps,
    FundamentalsBackfillDeps,
    backfill_bars,
    backfill_fundamentals,
)
from swing_copilot.pipeline.backfill import main as backfill_main
from swing_copilot.pipeline.daily import _select_symbols
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import (
    FundamentalsRecord,
    MarketStore,
    NonFiniteBarsError,
)
from swing_copilot.universe import UniverseMember
from swing_copilot.universe_sampling import select_universe_sample

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
    ) -> None:
        self.calls: list[tuple[list[str], date, date]] = []
        self._failing_symbols = failing_symbols
        #: Symbols returned with a NaN close, standing in for a provider whose
        #: own normalization does not drop them (`data/base.py`'s contract).
        self._non_finite_symbols = non_finite_symbols

    def get_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> BarFetchResult:
        self.calls.append((list(symbols), start, end))
        succeeded = [s for s in symbols if s not in self._failing_symbols]
        bars = pd.DataFrame(
            [
                _bar_row(symbol, _START)
                | (
                    {"close": float("nan")}
                    if symbol in self._non_finite_symbols
                    else {}
                )
                for symbol in succeeded
            ]
        )
        failures = tuple(
            FetchFailure(symbol=symbol, reason="no data returned", retryable=True)
            for symbol in symbols
            if symbol in self._failing_symbols
        )
        return BarFetchResult(bars=bars, failures=failures)

    def get_latest_bars(self, symbols: list[str], as_of: date) -> BarFetchResult:
        """Never used by the backfill path; present only to satisfy the port."""
        msg = f"backfill must not call get_latest_bars ({symbols}, {as_of})"
        raise AssertionError(msg)


class _CountingStore:
    """Wraps a real `MarketStore` to count `write_bars` calls."""

    def __init__(self, inner: MarketStore) -> None:
        self._inner = inner
        self.write_calls: list[pd.DataFrame] = []

    def earliest_bar_dates(self, symbols: list[str]) -> dict[str, date]:
        return self._inner.earliest_bar_dates(symbols)

    def write_bars(self, df: pd.DataFrame) -> None:
        self.write_calls.append(df.copy())
        self._inner.write_bars(df)


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

        with pytest.raises(NonFiniteBarsError):
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
        assert _select_symbols(universe, set(), 6) == expected

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
