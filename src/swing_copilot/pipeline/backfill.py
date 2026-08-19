"""`copilot-backfill`: one-off historical loading of bars and fundamentals.

The daily pipeline only ever fetches a rolling ~400-day price window, so a
freshly seeded database cannot support a multi-regime backtest. This module
is the deliberate one-off counterpart: it walks the configured universe once,
pulls history back to an explicit `--start`, and persists it through the same
adapters and repositories the daily run uses — never a raw provider call that
would bypass their timeout/retry contracts.

Two behaviors matter more than throughput here:

* **Chunked, paced fetching.** `YFinanceProvider` issues one bulk request per
  call and has no rate limiter of its own, so symbols are fetched in fixed
  chunks with a fixed pause between them.
* **One write.** `MarketStore.write_bars` rewrites each affected year
  partition wholesale, so every chunk's bars are accumulated in memory and
  written exactly once at the end rather than once per chunk.

Reruns are safe and cheap: a symbol whose stored history already reaches back
to `--start` is skipped without a network call. "Reaches back to `--start`"
allows `COVERAGE_TOLERANCE_DAYS` of slack, because `--start` is a calendar
date the operator picks (the documented example, 2019-01-01, is a market
holiday) while the oldest bar that can exist is the first *trading* day at or
after it. Without that slack no symbol would ever qualify and every rerun
would refetch the whole universe. A symbol that simply did not trade that
early (a later IPO) still never satisfies the test and is refetched on every
run — accepted, because the alternative is recording per-symbol "known-empty"
state this one-off tool has no reason to own.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pandas as pd

from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.clock import SystemClock
from swing_copilot.config import Secrets, load_settings
from swing_copilot.data.edgar import EdgarClient
from swing_copilot.data.yfinance_provider import YFinanceProvider
from swing_copilot.exceptions import ConfigError, SwingCopilotError
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import MarketStore, NonFiniteBarsError
from swing_copilot.universe import UniverseFetchOptions, get_sp500_universe
from swing_copilot.universe_sampling import select_universe_sample

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from datetime import datetime

    from swing_copilot.clock import Clock
    from swing_copilot.data.base import DataProvider, FetchFailure
    from swing_copilot.storage.market_store import FundamentalsRecord

SYMBOL_CHUNK_SIZE = 50
CHUNK_SLEEP_SECONDS = 2.0
# Slack allowed between `--start` and a symbol's oldest stored bar before the
# symbol counts as uncovered. Sized to exceed the longest US-market closure
# (a holiday adjoining a weekend is 4 calendar days) without being large
# enough to mask a genuinely short history.
COVERAGE_TOLERANCE_DAYS = 7
_PROVIDER_NAME = "yfinance"
_DEFAULT_SETTINGS_PATH = "config/settings.yaml"

logger = logging.getLogger(__name__)


class BackfillError(SwingCopilotError):
    """Raised for fail-fast argument/configuration errors, before any I/O."""


#: One line on stderr, exit 1 — a bad argument, an unusable settings file, or
#: a batch the store refused. `NonFiniteBarsError` belongs here (Issue #250,
#: folded into #249) because `write_bars`' rejection is batch-wide and runs
#: before the first partition is touched: nothing was written, so exiting `0`
#: would let a chained `copilot-backfill ... && copilot-backtest ...` run
#: against a store that gained no history. It stays **fatal**, exactly as
#: Issue #227 settled it — this only replaces the traceback with the one
#: operator-facing line Issue #221 standardized across the `--db` CLIs.
_EXIT_POLICY = ExitPolicy(
    errors=(BackfillError, ConfigError, NonFiniteBarsError), code=1
)


class _EdgarClientLike(Protocol):
    """Structural stand-in for `data.edgar.EdgarClient`, for fake injection."""

    def fetch_fundamentals(
        self, symbol: str, as_of: datetime, *, lookback_days: int
    ) -> list[FundamentalsRecord]:
        """See `EdgarClient.fetch_fundamentals`."""
        ...  # pragma: no cover


@dataclass(frozen=True, slots=True)
class BarsBackfillDeps:
    """Collaborators for `backfill_bars` (composition root wires the real ones)."""

    data_provider: DataProvider
    market_store: MarketStore
    clock: Clock
    provider_name: str = _PROVIDER_NAME
    sleep_fn: Callable[[float], None] = time.sleep


@dataclass(frozen=True, slots=True)
class BarsBackfillResult:
    """What one bars backfill actually did, for the operator's report."""

    skipped_symbols: tuple[str, ...]
    fetched_symbols: tuple[str, ...]
    written_rows: int
    failures: tuple[FetchFailure, ...]


@dataclass(frozen=True, slots=True)
class FundamentalsBackfillDeps:
    """Collaborators for `backfill_fundamentals`."""

    edgar_client: _EdgarClientLike
    market_store: MarketStore
    clock: Clock


@dataclass(frozen=True, slots=True)
class FundamentalsBackfillResult:
    """What one fundamentals backfill actually did."""

    written_records: int
    failed_symbols: tuple[str, ...]


def _chunks(symbols: Sequence[str], size: int) -> Iterator[list[str]]:
    for offset in range(0, len(symbols), size):
        yield list(symbols[offset : offset + size])


def _stamp_bars(
    bars: pd.DataFrame, provider_name: str, fetched_at: datetime
) -> pd.DataFrame:
    stamped = bars.copy()
    stamped["provider"] = provider_name
    stamped["fetched_at"] = fetched_at
    return stamped


def backfill_bars(
    deps: BarsBackfillDeps,
    symbols: Sequence[str],
    start: date,
    end: date,
) -> BarsBackfillResult:
    """Fetch and persist daily bars over `[start, end]` for `symbols`.

    Args:
        deps: Provider, store, clock, and the injectable inter-chunk pause.
        symbols: Tickers to cover, in the order they should be fetched.
        start: Inclusive first calendar date of history to obtain.
        end: Inclusive last calendar date (converted to the provider's
            exclusive end internally).

    Returns:
        Which symbols were skipped as already covered, which produced bars,
        how many rows were written, and every per-symbol fetch failure.
    """
    covered = deps.market_store.earliest_bar_dates(list(symbols))
    coverage_bound = start + timedelta(days=COVERAGE_TOLERANCE_DAYS)
    skipped = tuple(
        symbol
        for symbol in symbols
        if symbol in covered and covered[symbol] <= coverage_bound
    )
    already_covered = set(skipped)
    pending = [symbol for symbol in symbols if symbol not in already_covered]

    frames: list[pd.DataFrame] = []
    failures: list[FetchFailure] = []
    for index, chunk in enumerate(_chunks(pending, SYMBOL_CHUNK_SIZE)):
        if index:
            deps.sleep_fn(CHUNK_SLEEP_SECONDS)
        result = deps.data_provider.get_daily_bars(
            chunk, start, end + timedelta(days=1)
        )
        failures.extend(result.failures)
        if not result.bars.empty:
            frames.append(result.bars)

    if not frames:
        return BarsBackfillResult(
            skipped_symbols=skipped,
            fetched_symbols=(),
            written_rows=0,
            failures=tuple(failures),
        )

    combined = pd.concat(frames, ignore_index=True)
    deps.market_store.write_bars(
        _stamp_bars(combined, deps.provider_name, deps.clock.now())
    )
    # Build the symbol set once: the comprehension would otherwise rebuild it
    # from every fetched row for each of the ~500 pending symbols.
    fetched_symbols = set(combined["symbol"].unique())
    fetched = tuple(symbol for symbol in pending if symbol in fetched_symbols)
    return BarsBackfillResult(
        skipped_symbols=skipped,
        fetched_symbols=fetched,
        written_rows=len(combined),
        failures=tuple(failures),
    )


def backfill_fundamentals(
    deps: FundamentalsBackfillDeps,
    symbols: Sequence[str],
    start: date,
    as_of: date,
) -> FundamentalsBackfillResult:
    """Fetch and upsert every 10-K/10-Q filed between `start` and `as_of`.

    One symbol's EDGAR failure never aborts the run: the symbol is recorded
    and the walk continues, matching the daily pipeline's fail-soft treatment
    of the same boundary.

    Args:
        deps: EDGAR client, store, and clock.
        symbols: Tickers to cover.
        start: Inclusive oldest filing date to retain.
        as_of: Inclusive newest filing date to retain.

    Returns:
        How many records were persisted and which symbols failed outright.
    """
    # `fetch_fundamentals` bounds the window at `as_of_cutoff - lookback_days`,
    # and `as_of_cutoff` is an end-of-day instant. Counting whole days would
    # therefore put the lower bound at the *end* of `start` and drop every
    # filing made during the boundary day the operator explicitly asked for;
    # the extra day moves the bound to just before `start`'s midnight.
    lookback_days = (as_of - start).days + 1
    as_of_cutoff = _end_of_day(as_of)
    written = 0
    failed: list[str] = []
    for symbol in symbols:
        try:
            records = deps.edgar_client.fetch_fundamentals(
                symbol, as_of_cutoff, lookback_days=lookback_days
            )
        except SwingCopilotError, OSError, ValueError:
            # Fail-soft per symbol, but never silent: an EDGAR outage and a
            # programming error land in the same branch and are otherwise
            # indistinguishable in the operator's summary line.
            logger.exception("fundamentals backfill failed for %s", symbol)
            failed.append(symbol)
            continue
        deps.market_store.upsert_fundamentals(records)
        written += len(records)
    return FundamentalsBackfillResult(
        written_records=written, failed_symbols=tuple(failed)
    )


def _end_of_day(day: date) -> datetime:
    """Inclusive end-of-day UTC cutoff for a filing-date comparison."""
    return pd.Timestamp(day, tz="UTC").to_pydatetime() + timedelta(
        hours=23, minutes=59, seconds=59
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="copilot-backfill",
        description=(
            "バックテスト用に過去のバー/ファンダメンタルズを一括取得する。"
            "日次パイプラインとは別の一回限りの補充ツール。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("bars", "日足バーを --start までさかのぼって取得する"),
        ("fundamentals", "10-K/10-Q を --start までさかのぼって取得する"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--start", type=date.fromisoformat, required=True)
        sub.add_argument("--end", type=date.fromisoformat, default=None)
        sub.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
        sub.add_argument("--settings", default=_DEFAULT_SETTINGS_PATH)
        sub.add_argument("--symbols", default=None)
        sub.add_argument("--limit", type=int, default=None)

    return parser.parse_args(argv)


def _resolve_symbols(args: argparse.Namespace, end: date) -> list[str]:
    """Resolve the symbols to backfill: `--symbols`, or a `--limit` sample.

    `--limit` used to be `symbols[: args.limit]` over an `ORDER BY symbol`
    universe, i.e. "the N tickers starting with A" — the same defect class
    already fixed for `copilot-backtest` (Issue #194) and `copilot-daily`
    (Issue #205), left behind on the third `--limit` (Issue #206). Warming
    only the A-side of the cache biases nothing numerically here, but it does
    decide which symbols a later smoke run or backtest finds already cached.
    Sharing `select_universe_sample()` — and therefore its fixed salt — makes
    `--limit N` cover the same symbol set across all three CLIs.
    """
    if args.symbols:
        symbols = [token.strip().upper() for token in args.symbols.split(",")]
        return [symbol for symbol in symbols if symbol]

    settings = load_settings(args.settings)
    universe = get_sp500_universe(
        end,
        options=UniverseFetchOptions(
            snapshot_path=settings.universe.snapshot_path,
            manual_include=settings.universe.manual_include,
            manual_exclude=settings.universe.manual_exclude,
        ),
    )
    sample = select_universe_sample(universe, args.limit)
    if sample.is_stratified_sample:
        logger.info("universe sampled by --limit: %s / %s", *sample.summary_lines())
    return list(sample.symbols)


def _validate(args: argparse.Namespace, end: date) -> None:
    if args.start > end:
        msg = f"--start ({args.start}) は --end ({end}) より後ろにできません。"
        raise BackfillError(msg)
    if args.limit is not None and args.limit <= 0:
        msg = "--limit は1以上の整数で指定してください。"
        raise BackfillError(msg)


def _run_bars(args: argparse.Namespace, end: date, symbols: list[str]) -> None:
    database = Database(args.db)
    market_store = MarketStore(database, parquet_root=Path(args.db).parent / "bars")
    deps = BarsBackfillDeps(
        data_provider=YFinanceProvider(),
        market_store=market_store,
        clock=SystemClock(),
    )
    result = backfill_bars(deps, symbols, args.start, end)
    sys.stdout.write(
        f"bars: 対象 {len(symbols)} 銘柄 / スキップ済み "
        f"{len(result.skipped_symbols)} / 取得 {len(result.fetched_symbols)} / "
        f"書き込み {result.written_rows} 行\n"
    )
    if result.failures:
        failed = ", ".join(sorted({failure.symbol for failure in result.failures}))
        sys.stdout.write(f"失敗した銘柄: {failed}\n")
    if result.failures and not result.fetched_symbols and not result.skipped_symbols:
        # Nothing was covered already and nothing could be fetched: the store
        # is exactly as empty as before. Exiting 0 here would let a chained
        # `copilot-backfill ... && copilot-backtest ...` run against it.
        msg = "bars: 全銘柄の取得に失敗したため書き込みは行われませんでした。"
        raise BackfillError(msg)


def _run_fundamentals(args: argparse.Namespace, end: date, symbols: list[str]) -> None:
    secrets = Secrets()
    if not secrets.edgar_identity:
        msg = "EDGAR_IDENTITY が未設定のため fundamentals は取得できません。"
        raise BackfillError(msg)
    database = Database(args.db)
    market_store = MarketStore(database, parquet_root=Path(args.db).parent / "bars")
    deps = FundamentalsBackfillDeps(
        edgar_client=EdgarClient(secrets.edgar_identity),
        market_store=market_store,
        clock=SystemClock(),
    )
    result = backfill_fundamentals(deps, symbols, args.start, end)
    sys.stdout.write(
        f"fundamentals: 対象 {len(symbols)} 銘柄 / "
        f"書き込み {result.written_records} 件\n"
    )
    if result.failed_symbols:
        sys.stdout.write(f"失敗した銘柄: {', '.join(result.failed_symbols)}\n")


def _backfill(args: argparse.Namespace) -> None:
    end = args.end if args.end is not None else SystemClock().today()
    _validate(args, end)
    symbols = _resolve_symbols(args, end)
    if args.command == "bars":
        _run_bars(args, end, symbols)
    else:
        _run_fundamentals(args, end, symbols)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: resolve the universe, then backfill bars or fundamentals."""
    args = _parse_args(argv)
    run_cli(lambda: _backfill(args), _EXIT_POLICY)


if __name__ == "__main__":  # pragma: no cover
    main()
