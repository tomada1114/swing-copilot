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

Two further subcommands exist for the raw-bar storage model (Issue #413):
`rebuild` re-fetches a symbol's *entire* history and replaces it wholesale --
the only sanctioned way to change the adjustment basis of stored bars, and
the migration path off a store written before that model -- while `check`
audits the store read-only, reporting the format marker and any symbol whose
stored series still interleaves two adjustment bases.
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
from swing_copilot.data.adjustments import first_mixed_basis_jump
from swing_copilot.data.base import BARS_COLUMNS, empty_actions_frame
from swing_copilot.data.edgar import EdgarClient
from swing_copilot.data.yfinance_provider import YFinanceProvider
from swing_copilot.exceptions import ConfigError, SwingCopilotError
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    BarsFormatError,
    MarketStore,
    NonFiniteBarsError,
    validate_bars_format,
)
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
#: Where `rebuild` starts. Deliberately older than any S&P 500 member's
#: listing rather than a per-symbol bound: the whole point of a rebuild is
#: that *every* stored row for a symbol is replaced, so the fetch window has
#: to cover everything the store could possibly hold.
REBUILD_START = date(1990, 1, 1)
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
    #: Symbols the store refused (mixed-basis signature, or a re-fetch that
    #: contradicts stored raw closes). Fail-soft like `failures`: their old
    #: rows stand, and `rebuild` is the sanctioned way to accept new ones.
    quarantined_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BarsRebuildResult:
    """What one `rebuild` replaced, and what it deliberately left alone."""

    replaced_symbols: tuple[str, ...]
    rejected_symbols: tuple[str, ...]
    written_rows: int
    failures: tuple[FetchFailure, ...]


@dataclass(frozen=True, slots=True)
class MixedBasisFinding:
    """One stored series that still interleaves two adjustment bases."""

    symbol: str
    first_jump_date: date


@dataclass(frozen=True, slots=True)
class BarsCheckResult:
    """What the read-only store audit saw."""

    #: `None` when the marker is present and current; otherwise the operator
    #: -facing reason it is not, taken from `BarsFormatError`.
    format_problem: str | None
    scanned_symbols: tuple[str, ...]
    findings: tuple[MixedBasisFinding, ...]


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


@dataclass(frozen=True, slots=True)
class _FetchedBatch:
    """Everything the chunked walk collected, concatenated once."""

    bars: pd.DataFrame
    actions: pd.DataFrame
    failures: tuple[FetchFailure, ...]

    def symbols_with_bars(self) -> set[str]:
        """The symbols that actually produced at least one row."""
        if self.bars.empty:
            return set()
        return set(self.bars["symbol"].unique())


def _fetch_in_chunks(
    deps: BarsBackfillDeps, symbols: Sequence[str], start: date, end_exclusive: date
) -> _FetchedBatch:
    """Walk `symbols` in paced chunks, accumulating bars, actions and failures.

    Shared by `backfill_bars` and `rebuild_bars` so the provider's rate-limit
    posture (one bulk request per chunk, a fixed pause between them) is
    described in exactly one place.

    Args:
        deps: Provider, store, clock, and the injectable inter-chunk pause.
        symbols: Tickers to fetch, in request order.
        start: Inclusive first calendar date.
        end_exclusive: The provider's exclusive end — never a bar on this day.

    Returns:
        One `_FetchedBatch`; its frames are empty when nothing came back.
    """
    bar_frames: list[pd.DataFrame] = []
    action_frames: list[pd.DataFrame] = []
    failures: list[FetchFailure] = []
    for index, chunk in enumerate(_chunks(symbols, SYMBOL_CHUNK_SIZE)):
        if index:
            deps.sleep_fn(CHUNK_SLEEP_SECONDS)
        result = deps.data_provider.get_daily_bars(chunk, start, end_exclusive)
        failures.extend(result.failures)
        if not result.bars.empty:
            bar_frames.append(result.bars)
        if not result.actions.empty:
            action_frames.append(result.actions)
    return _FetchedBatch(
        bars=(
            pd.concat(bar_frames, ignore_index=True)
            if bar_frames
            else pd.DataFrame(columns=list(BARS_COLUMNS))
        ),
        actions=(
            pd.concat(action_frames, ignore_index=True)
            if action_frames
            else empty_actions_frame()
        ),
        failures=tuple(failures),
    )


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
        how many rows were written, every per-symbol fetch failure, and every
        symbol the store quarantined.
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

    batch = _fetch_in_chunks(deps, pending, start, end + timedelta(days=1))
    fetched_at = deps.clock.now()
    # Splits before bars: `read_bars` adjusts from `corporate_actions`, so a
    # split recorded after its own history would be invisible to the very
    # next read.
    deps.market_store.write_corporate_actions(
        batch.actions, provider=deps.provider_name, fetched_at=fetched_at
    )

    if batch.bars.empty:
        return BarsBackfillResult(
            skipped_symbols=skipped,
            fetched_symbols=(),
            written_rows=0,
            failures=batch.failures,
        )

    write_result = deps.market_store.write_bars(
        _stamp_bars(batch.bars, deps.provider_name, fetched_at)
    )
    # Build the symbol set once: the comprehension would otherwise rebuild it
    # from every fetched row for each of the ~500 pending symbols.
    fetched_symbols = batch.symbols_with_bars()
    fetched = tuple(symbol for symbol in pending if symbol in fetched_symbols)
    return BarsBackfillResult(
        skipped_symbols=skipped,
        fetched_symbols=fetched,
        written_rows=len(batch.bars),
        failures=batch.failures,
        quarantined_symbols=tuple(
            quarantine.symbol for quarantine in write_result.quarantined
        ),
    )


def rebuild_bars(deps: BarsBackfillDeps, symbols: Sequence[str]) -> BarsRebuildResult:
    """Re-fetch each symbol's whole history and replace its stored rows.

    The migration and repair path for Issue #413. `write_bars`' immutability
    gate exists to stop a provider glitch from silently changing the basis of
    stored history; a rebuild is the operator deciding to change it on
    purpose, so it goes through `MarketStore.replace_symbol_bars`, which drops
    every stored row of the symbol across every year partition before writing
    the new ones.

    A symbol whose response could not be normalized (`NormalizationRejection`,
    surfaced as a non-retryable `FetchFailure`) is left strictly alone: its
    old rows are better than none, and it is named in the result so the
    operator can retry it once the provider's history is sane again. Because
    `replace_symbol_bars` stamps the format marker, a partial rebuild does
    mark the store as raw while those symbols still hold their old basis --
    accepted, since the alternative is refusing to migrate any store with one
    bad ticker in it, and `check` is what finds them afterwards.

    Args:
        deps: Provider, store, clock, and the injectable inter-chunk pause.
        symbols: Tickers to rebuild, in the order they should be fetched.

    Returns:
        Which symbols were replaced, which were rejected and left untouched,
        how many rows were written, and every per-symbol fetch failure.
    """
    end_exclusive = deps.clock.today() + timedelta(days=1)
    batch = _fetch_in_chunks(deps, symbols, REBUILD_START, end_exclusive)
    fetched_at = deps.clock.now()
    deps.market_store.write_corporate_actions(
        batch.actions, provider=deps.provider_name, fetched_at=fetched_at
    )

    rebuilt = batch.symbols_with_bars()
    replaced = tuple(symbol for symbol in symbols if symbol in rebuilt)
    rejected = tuple(symbol for symbol in symbols if symbol not in rebuilt)
    if replaced:
        deps.market_store.replace_symbol_bars(
            list(replaced), _stamp_bars(batch.bars, deps.provider_name, fetched_at)
        )
    return BarsRebuildResult(
        replaced_symbols=replaced,
        rejected_symbols=rejected,
        written_rows=len(batch.bars),
        failures=batch.failures,
    )


def check_bars(market_store: MarketStore, symbols: Sequence[str]) -> BarsCheckResult:
    """Audit stored bars for a mixed adjustment basis. Never writes.

    Reads the *bars* from the Parquet partitions directly
    (`MarketStore.read_raw_bars`) rather than through DuckDB, because the
    signature is only visible in the values as stored -- `read_bars` would
    hand back an adjusted series in which it can no longer appear.

    The splits do come from DuckDB, in one short query per chunk: a flip is a
    split-sized step, and asking the question without them flags 153 of this
    repository's 510 symbols on nothing but 2008 and the dot-com years
    (Issue #421). Pass a store opened read-only, as the CLI does — a write
    connection ensures its tables on open, which would make an audit that
    writes nothing write something.

    Args:
        market_store: The store to audit; open it read-only.
        symbols: Tickers to scan; empty means every symbol with stored bars.

    Returns:
        The marker's state, what was scanned, and one finding per symbol
        whose series still flips between two bases.
    """
    scanned = tuple(symbols) if symbols else market_store.stored_symbols()
    try:
        validate_bars_format(market_store.parquet_root)
    except BarsFormatError as exc:
        # Reported, not raised: "this store predates the raw-bar model" is
        # precisely one of the answers the audit exists to give, and the
        # series scan below could not be trusted on those partitions anyway.
        return BarsCheckResult(
            format_problem=str(exc), scanned_symbols=scanned, findings=()
        )

    # Chunked rather than one symbol at a time: every read re-scans every
    # year partition, so a per-symbol loop over a 500-name universe would
    # walk 26 years of Parquet 500 times over. One chunk is one pass.
    flagged: dict[str, date] = {}
    for chunk in _chunks(scanned, SYMBOL_CHUNK_SIZE):
        rows = market_store.read_raw_bars(chunk)
        if rows.empty:
            continue
        splits_by_symbol = market_store.read_splits(chunk, as_of=date.max)
        for symbol, series in rows.groupby("symbol", sort=False):
            position = first_mixed_basis_jump(
                series["close"], splits_by_symbol.get(str(symbol), ())
            )
            if position is not None:
                flagged[str(symbol)] = series["date"].to_numpy()[position]
    return BarsCheckResult(
        format_problem=None,
        scanned_symbols=scanned,
        # Reported in the order the operator asked for, not Parquet's.
        findings=tuple(
            MixedBasisFinding(symbol=symbol, first_jump_date=flagged[symbol])
            for symbol in scanned
            if symbol in flagged
        ),
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

    rebuild = subparsers.add_parser(
        "rebuild",
        help="全履歴を取り直して保存済みの行を丸ごと置き換える（Issue #413 の移行/修復）",
    )
    rebuild.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    rebuild.add_argument("--settings", default=_DEFAULT_SETTINGS_PATH)
    rebuild.add_argument("--symbols", default=None)
    rebuild.add_argument("--limit", type=int, default=None)

    # No `--settings`/`--limit`: with no `--symbols` the audit enumerates the
    # store itself, so it never needs the universe (and therefore never needs
    # a settings file that a fresh worktree may not have).
    check = subparsers.add_parser(
        "check",
        help="保存済みバーの形式マーカーと調整基準の混在を読み取り専用で点検する",
    )
    check.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    check.add_argument("--symbols", default=None)

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
    if explicit := _resolve_explicit_symbols(args):
        return explicit

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
    _validate_limit(args)


def _validate_limit(args: argparse.Namespace) -> None:
    if args.limit is not None and args.limit <= 0:
        msg = "--limit は1以上の整数で指定してください。"
        raise BackfillError(msg)


def _run_bars(args: argparse.Namespace, end: date, symbols: list[str]) -> None:
    deps = BarsBackfillDeps(
        data_provider=YFinanceProvider(),
        market_store=_market_store(args),
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
    if result.quarantined_symbols:
        sys.stdout.write(
            f"隔離した銘柄: {', '.join(sorted(result.quarantined_symbols))}\n"
        )
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
    deps = FundamentalsBackfillDeps(
        edgar_client=EdgarClient(secrets.edgar_identity),
        market_store=_market_store(args),
        clock=SystemClock(),
    )
    result = backfill_fundamentals(deps, symbols, args.start, end)
    sys.stdout.write(
        f"fundamentals: 対象 {len(symbols)} 銘柄 / "
        f"書き込み {result.written_records} 件\n"
    )
    if result.failed_symbols:
        sys.stdout.write(f"失敗した銘柄: {', '.join(result.failed_symbols)}\n")


def _market_store(args: argparse.Namespace, *, read_only: bool = False) -> MarketStore:
    """The store both bar commands address: `<db>`'s sibling `bars/` root.

    Args:
        args: The parsed command line; `--db` names the database file.
        read_only: Open the database read-only. `check` does, so that reading
            a symbol's splits cannot ensure a table, and the audit keeps the
            "writes nothing" property its own test pins.
    """
    return MarketStore(
        Database(args.db, read_only=read_only),
        parquet_root=Path(args.db).parent / "bars",
    )


def _run_rebuild(args: argparse.Namespace, clock: SystemClock) -> None:
    _validate_limit(args)
    symbols = _resolve_symbols(args, clock.today())
    deps = BarsBackfillDeps(
        data_provider=YFinanceProvider(),
        market_store=_market_store(args),
        clock=clock,
    )
    result = rebuild_bars(deps, symbols)
    sys.stdout.write(
        f"rebuild: 対象 {len(symbols)} 銘柄 / 置換 "
        f"{len(result.replaced_symbols)} / 拒否 {len(result.rejected_symbols)} / "
        f"書き込み {result.written_rows} 行\n"
    )
    if result.rejected_symbols:
        sys.stdout.write(
            f"既存行を維持した銘柄: {', '.join(result.rejected_symbols)}\n"
        )
    if not result.replaced_symbols:
        # Nothing was replaced, so the store still holds whatever basis it
        # had -- and, crucially, no format marker was written over it.
        msg = "rebuild: 全銘柄の取得に失敗したため置き換えは行われませんでした。"
        raise BackfillError(msg)


def _run_check(args: argparse.Namespace) -> None:
    symbols = _resolve_explicit_symbols(args)
    result = check_bars(_market_store(args, read_only=True), symbols)
    if result.format_problem is not None:
        sys.stdout.write(f"形式マーカー: NG\n{result.format_problem}\n")
        return
    sys.stdout.write("形式マーカー: ok（basis=raw, version=2）\n")
    if not result.findings:
        sys.stdout.write(
            f"check: ok（対象 {len(result.scanned_symbols)} 銘柄、混在署名なし）\n"
        )
        return
    sys.stdout.write(
        f"check: 対象 {len(result.scanned_symbols)} 銘柄 / "
        f"混在署名 {len(result.findings)} 銘柄\n"
    )
    for finding in result.findings:
        sys.stdout.write(
            f"混在署名: {finding.symbol}（最初のジャンプ "
            f"{finding.first_jump_date.isoformat()}）\n"
        )


def _resolve_explicit_symbols(args: argparse.Namespace) -> list[str]:
    """`--symbols` as a ticker list; empty when it was not given."""
    if not args.symbols:
        return []
    symbols = [token.strip().upper() for token in args.symbols.split(",")]
    return [symbol for symbol in symbols if symbol]


def _backfill(args: argparse.Namespace) -> None:
    if args.command == "check":
        _run_check(args)
        return
    clock = SystemClock()
    if args.command == "rebuild":
        _run_rebuild(args, clock)
        return
    end = args.end if args.end is not None else clock.today()
    _validate(args, end)
    symbols = _resolve_symbols(args, end)
    if args.command == "bars":
        _run_bars(args, end, symbols)
    else:
        _run_fundamentals(args, end, symbols)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: backfill, rebuild, or audit the stored history."""
    args = _parse_args(argv)
    run_cli(lambda: _backfill(args), _EXIT_POLICY)


if __name__ == "__main__":  # pragma: no cover
    main()
