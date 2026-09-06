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
audits the store read-only, running two independent scans in one pass: the
format marker and any symbol whose stored series still interleaves two
adjustment bases (`check_bars`), and, since Issue #427, every frozen
`risk_assessments.entry_price` that no longer agrees with its own day's raw
bar (`check_entry_prices`) -- the corruption a `rebuild` can leave behind in
history that was already frozen before it ran. Neither scan writes, and
findings from either never change `check`'s exit code.
"""

from __future__ import annotations

import argparse
import bisect
import logging
import math
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
from swing_copilot.storage.tracking_records import get_frozen_entry_prices
from swing_copilot.tracking.update import is_entry_price_basis_mismatch
from swing_copilot.universe import UniverseFetchOptions, get_sp500_universe
from swing_copilot.universe_sampling import select_universe_sample

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from datetime import datetime
    from uuid import UUID

    from swing_copilot.clock import Clock
    from swing_copilot.data.base import DataProvider, FetchFailure
    from swing_copilot.storage.market_store import (
        DroppedSessions,
        FundamentalsRecord,
        SessionCoverage,
    )

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
#: A stored date counts as a real trading session only when at least this
#: fraction of that date's *listed* symbols (`SessionCoverage.listed_span`)
#: actually have a bar on it (Issue #449, 3-B). Chosen without measuring the
#: real store -- this run was not permitted to touch the shared R2 data to
#: check it against actual coverage -- so it is a single, trivially re-tuned
#: module constant rather than something spread across the predicate.
_SESSION_QUORUM_RATIO = 0.5
#: Symbols that never trade on a real *equity* market holiday because they
#: follow a different calendar (`^TNX` tracks the US Treasury market, which
#: sits open on some equity-only holidays such as Columbus Day/Veterans Day).
#: Excluded only from the missing-session *scan* when it is auto-enumerated
#: from the store (`--symbols` omitted) -- never from the session calendar
#: itself, and never when named explicitly via `--symbols`.
_NON_EQUITY_CALENDAR_SYMBOLS = ("^TNX",)
#: How many missing/dropped session dates a report names per symbol before
#: summarizing the rest, so one badly-covered symbol cannot flood the output.
_MAX_REPORTED_MISSING_SESSIONS = 5

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
    #: Stored sessions the provider stopped returning, per symbol -- report
    #: -only (Issue #449). The old rows are still written: `write_bars` never
    #: loses a row, it only stops re-affirming one this batch's own window
    #: could have re-affirmed.
    dropped_sessions: tuple[DroppedSessions, ...] = ()


@dataclass(frozen=True, slots=True)
class BarsRebuildResult:
    """What one `rebuild` replaced, and what it deliberately left alone."""

    replaced_symbols: tuple[str, ...]
    rejected_symbols: tuple[str, ...]
    written_rows: int
    failures: tuple[FetchFailure, ...]
    #: Stored sessions the rebuild's re-fetch dropped, per symbol -- and,
    #: unlike `BarsBackfillResult.dropped_sessions`, genuinely gone
    #: afterwards: `replace_symbol_bars` erases what it replaces (Issue #449).
    dropped_sessions: tuple[DroppedSessions, ...] = ()


@dataclass(frozen=True, slots=True)
class MixedBasisFinding:
    """One stored series that still interleaves two adjustment bases."""

    symbol: str
    first_jump_date: date


@dataclass(frozen=True, slots=True)
class MissingSessionFinding:
    """One symbol missing a session the rest of the store has (Issue #449).

    Read-only and re-derived on every `check` -- never persisted -- because
    "does the store have a hole today" is a fact `check` can always recompute
    from the store itself; only "when did the provider drop it" needs the
    daily run's own `run_steps.detail` history to answer.
    """

    symbol: str
    #: Ascending.
    missing_dates: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class BarsCheckResult:
    """What the read-only store audit saw."""

    #: `None` when the marker is present and current; otherwise the operator
    #: -facing reason it is not, taken from `BarsFormatError`.
    format_problem: str | None
    scanned_symbols: tuple[str, ...]
    findings: tuple[MixedBasisFinding, ...]
    #: Sessions the rest of the store has that a scanned symbol lacks. Empty
    #: whenever `format_problem` is set: those partitions cannot be trusted
    #: as raw, so any gap found in them would be meaningless.
    missing_sessions: tuple[MissingSessionFinding, ...] = ()


@dataclass(frozen=True, slots=True)
class EntryPriceFinding:
    """One `risk_assessments.entry_price` that disagrees with its own day's bar.

    Issue #427: a frozen `entry_price` is, by construction, the run day's raw
    close (see `check_entry_prices`), so a disagreement here is never a
    market move to interpret -- only a corruption already frozen into
    history, most often a store repaired (`copilot-backfill rebuild`) after
    the run that froze this row.
    """

    symbol: str
    run_date: date
    run_id: UUID
    frozen_entry_price: float
    bar_close: float


@dataclass(frozen=True, slots=True)
class EntryPriceCheckResult:
    """What the read-only `risk_assessments` basis audit saw.

    `unresolved_rows` is deliberately separate from `findings`: a row whose
    same-day raw bar cannot be resolved at all (no stored session, or a
    non-finite/non-positive price on either side) is a data-quality gap, not
    proof of a basis mismatch, so it is counted rather than reported as one.
    """

    scanned_rows: int
    scanned_symbols: tuple[str, ...]
    findings: tuple[EntryPriceFinding, ...]
    unresolved_rows: int


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
        dropped_sessions=write_result.dropped,
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
    dropped_sessions: tuple[DroppedSessions, ...] = ()
    if replaced:
        replace_result = deps.market_store.replace_symbol_bars(
            list(replaced), _stamp_bars(batch.bars, deps.provider_name, fetched_at)
        )
        dropped_sessions = replace_result.dropped
    return BarsRebuildResult(
        replaced_symbols=replaced,
        rejected_symbols=rejected,
        written_rows=len(batch.bars),
        failures=batch.failures,
        dropped_sessions=dropped_sessions,
    )


def _session_dates(coverage: SessionCoverage) -> frozenset[date]:
    """Which stored dates count as a real trading session (Issue #449, 3-B).

    A date is a session when at least `_SESSION_QUORUM_RATIO` of the symbols
    *listed* on it (i.e. within their own stored span) actually have a bar on
    it. This single predicate is what absorbs every edge case without a
    special rule: a date with zero stored bars is not even a candidate (a
    real market holiday never got this far), a universe that grows over the
    years grows `L(D)` right along with it so the threshold never drifts, and
    one symbol's stray bad bar cannot manufacture a session on its own
    (`n=1` against `L≈510` never clears the ratio, so it never drags the
    other 509 symbols into being audited against a date that meant nothing).

    Uses sorted-array binary search for `L(D)` (`bisect`) rather than
    counting per date over every symbol: the latter is `O(dates * symbols)`,
    which over a full store (~500 symbols, decades of dates) is the
    difference between a sub-second scan and one that visibly drags.
    """
    if not coverage.bars_per_date:
        return frozenset()
    firsts = sorted(first for first, _ in coverage.listed_span.values())
    lasts = sorted(last for _, last in coverage.listed_span.values())
    sessions: set[date] = set()
    for bar_date, count in coverage.bars_per_date.items():
        # Listed on `bar_date` == first <= bar_date <= last, so the count is
        # "how many symbols started on or before it" minus "how many had
        # already ended before it".
        listed = bisect.bisect_right(firsts, bar_date) - bisect.bisect_left(
            lasts, bar_date
        )
        if count >= _SESSION_QUORUM_RATIO * listed:
            sessions.add(bar_date)
    return frozenset(sessions)


def _session_gaps(
    coverage: SessionCoverage,
    scanned: Sequence[str],
    bar_dates_by_symbol: Mapping[str, frozenset[date]],
) -> tuple[MissingSessionFinding, ...]:
    """Sessions each of `scanned` is missing while it was listed (Issue #449).

    Args:
        coverage: The store-wide calendar (`MarketStore.session_coverage`),
            always built from the *whole* store regardless of `scanned` --
            a calendar derived from one symbol would be meaningless.
        scanned: Symbols to check for gaps; a symbol absent from
            `coverage.listed_span` (no stored bars at all) is silently
            skipped, matching the mixed-basis scan's treatment of the same
            case.
        bar_dates_by_symbol: Each scanned symbol's own stored bar dates.

    Returns:
        One finding per symbol with at least one gap, in `scanned` order.
    """
    sessions = _session_dates(coverage)
    findings: list[MissingSessionFinding] = []
    for symbol in scanned:
        span = coverage.listed_span.get(symbol)
        if span is None:
            continue
        first, last = span
        bar_dates = bar_dates_by_symbol.get(symbol, frozenset())
        missing = tuple(
            sorted(
                session
                for session in sessions
                if first <= session <= last and session not in bar_dates
            )
        )
        if missing:
            findings.append(MissingSessionFinding(symbol=symbol, missing_dates=missing))
    return tuple(findings)


def check_bars(market_store: MarketStore, symbols: Sequence[str]) -> BarsCheckResult:
    """Audit stored bars for a mixed adjustment basis and dropped sessions.

    Never writes. Two independent read-only checks share the same chunked
    walk over the store:

    1. **Mixed adjustment basis** (Issue #413/#421/#425). Reads the *bars*
       from the Parquet partitions directly (`MarketStore.read_raw_bars`)
       rather than through DuckDB, because the signature is only visible in
       the values as stored -- `read_bars` would hand back an adjusted series
       in which it can no longer appear. The splits do come from DuckDB, in
       one short query per chunk: a flip is a split-sized step, and asking
       the question without them flags 153 of this repository's 510 symbols
       on nothing but 2008 and the dot-com years (Issue #421). A reversing
       pair is further required to have a run no longer than 25 sessions with
       a matching split's `ex_date` after that run, which took the same audit
       from 19 flagged symbols to 2 (Issue #425).
    2. **Sessions the store has that a symbol is missing** (Issue #449). The
       session calendar (`MarketStore.session_coverage`) is always built from
       the *whole* store, regardless of `--symbols` -- a calendar derived
       from one symbol would be meaningless. `^TNX` follows the bond market's
       holiday calendar rather than the equity one, so it is excluded from
       this half of the scan only when `symbols` was not given (i.e. the scan
       enumerated the store itself); naming it explicitly still scans it,
       since the operator asked on purpose.

    Pass a store opened read-only, as the CLI does — a write connection
    ensures its tables on open, which would make an audit that writes nothing
    write something.

    Args:
        market_store: The store to audit; open it read-only.
        symbols: Tickers to scan; empty means every symbol with stored bars.

    Returns:
        The marker's state, what was scanned, one finding per symbol whose
        series still flips between two bases, and one finding per symbol
        missing a session the rest of the store has.
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

    coverage = market_store.session_coverage()
    session_scan_symbols = (
        tuple(
            symbol for symbol in scanned if symbol not in _NON_EQUITY_CALENDAR_SYMBOLS
        )
        if not symbols
        else scanned
    )

    # Chunked rather than one symbol at a time: every read re-scans every
    # year partition, so a per-symbol loop over a 500-name universe would
    # walk 26 years of Parquet 500 times over. One chunk is one pass.
    flagged: dict[str, date] = {}
    bar_dates_by_symbol: dict[str, frozenset[date]] = {}
    for chunk in _chunks(scanned, SYMBOL_CHUNK_SIZE):
        rows = market_store.read_raw_bars(chunk)
        if rows.empty:
            continue
        splits_by_symbol = market_store.read_splits(chunk, as_of=date.max)
        for raw_symbol, series in rows.groupby("symbol", sort=False):
            symbol = str(raw_symbol)
            bar_dates_by_symbol[symbol] = frozenset(series["date"])
            position = first_mixed_basis_jump(series, splits_by_symbol.get(symbol, ()))
            if position is not None:
                flagged[symbol] = series["date"].to_numpy()[position]
    return BarsCheckResult(
        format_problem=None,
        scanned_symbols=scanned,
        # Reported in the order the operator asked for, not Parquet's.
        findings=tuple(
            MixedBasisFinding(symbol=symbol, first_jump_date=flagged[symbol])
            for symbol in scanned
            if symbol in flagged
        ),
        missing_sessions=_session_gaps(
            coverage, session_scan_symbols, bar_dates_by_symbol
        ),
    )


def check_entry_prices(
    database: Database, market_store: MarketStore, symbols: Sequence[str]
) -> EntryPriceCheckResult:
    """Audit every frozen `risk_assessments.entry_price` against its own day's bar.

    Issue #427's second `check` audit, deliberately independent of
    `check_bars`' mixed-basis scan above: a frozen `entry_price` is, by
    construction, the *raw* close of its own run day. `read_bars` only ever
    divides a row dated `as_of` by a split whose `ex_date` also satisfies
    `as_of < ex_date <= as_of` -- which no split can, so the cumulative
    factor on that row is always exactly `1.0`. This audit therefore never
    reads a split at all: it compares the frozen price straight against
    `MarketStore.read_raw_bars`, the same as-stored source `check_bars` uses,
    which is what keeps the factor-driven false positives Issue #425 tuned
    for structurally out of reach here.

    A write-time gate making the same comparison would be a tautology: at
    the moment a daily run freezes `entry_price`, it *is* the same day's
    stored close by construction, so the two could only ever agree. The
    #423 corruption this audit exists to catch is created afterwards, when
    `copilot-backfill rebuild` replaces the bars a `risk_assessments` row
    already froze a value from -- a divergence a write-time check could never
    see, and only a scan over stored history can.

    Never writes, and the shared predicate
    (`tracking.update.is_entry_price_basis_mismatch`) is exactly the one
    `_seed_position` uses to fall back to the bar's close, so a finding here
    names precisely the row `copilot-track rebuild` would replace.

    Args:
        database: Shared DuckDB connection owner, opened read-only as the CLI
            does.
        market_store: Stored bars; nothing is fetched.
        symbols: Tickers to narrow to; empty audits every symbol with a
            frozen price.

    Returns:
        How many rows and symbols were scanned, every basis mismatch found,
        and how many rows could not be resolved either way (no same-day raw
        bar, or a non-finite/non-positive price on either side).
    """
    if not database.db_path.exists():
        # The CLI's format-marker check already returns before this runs for
        # a missing store; this only guards a caller that reaches here first.
        return EntryPriceCheckResult(
            scanned_rows=0, scanned_symbols=(), findings=(), unresolved_rows=0
        )
    rows = get_frozen_entry_prices(database, symbols)
    if not rows:
        return EntryPriceCheckResult(
            scanned_rows=0, scanned_symbols=(), findings=(), unresolved_rows=0
        )

    scanned_symbols = tuple(sorted({row.symbol for row in rows}))
    wanted = {(row.symbol, row.run_date) for row in rows}
    run_dates = [row.run_date for row in rows]
    start, end = min(run_dates), max(run_dates)
    # Only the frozen rows' own (symbol, run_date) pairs are kept: the range
    # read spans every session between the oldest and newest run, which for a
    # multi-year history is orders of magnitude more bars than the audit
    # compares against.
    raw_closes: dict[tuple[str, date], float] = {}
    for chunk in _chunks(scanned_symbols, SYMBOL_CHUNK_SIZE):
        raw = market_store.read_raw_bars(chunk, start=start, end=end)
        if raw.empty:
            continue
        for symbol, bar_date, close in zip(
            raw["symbol"], raw["date"], raw["close"], strict=True
        ):
            key = (str(symbol), bar_date)
            if key in wanted:
                raw_closes[key] = float(close)

    findings: list[EntryPriceFinding] = []
    unresolved_rows = 0
    for row in rows:
        if not math.isfinite(row.entry_price) or row.entry_price <= 0:
            unresolved_rows += 1
            continue
        bar_close = raw_closes.get((row.symbol, row.run_date))
        if bar_close is None or not math.isfinite(bar_close) or bar_close <= 0:
            unresolved_rows += 1
            continue
        if is_entry_price_basis_mismatch(row.entry_price, bar_close):
            findings.append(
                EntryPriceFinding(
                    symbol=row.symbol,
                    run_date=row.run_date,
                    run_id=row.run_id,
                    frozen_entry_price=row.entry_price,
                    bar_close=bar_close,
                )
            )
    return EntryPriceCheckResult(
        scanned_rows=len(rows),
        scanned_symbols=scanned_symbols,
        findings=tuple(findings),
        unresolved_rows=unresolved_rows,
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


def _truncated_dates(dates: Sequence[date]) -> str:
    """Render up to `_MAX_REPORTED_MISSING_SESSIONS` dates, then summarize.

    Both callers hand over an ascending list, so the slice keeps the *oldest*
    dates -- the first session a hole opened on is what an operator chases,
    and `daily.py`'s `run_steps.detail` truncates the same list the same way.
    The label has to say so: reporting the head as "最新" would send that
    operator looking for a gap on the wrong end of the series.

    Args:
        dates: Ascending session dates.

    Returns:
        A comma-joined date list, with a leading count and a trailing `...`
        once there are more dates than fit.
    """
    shown = ", ".join(d.isoformat() for d in dates[:_MAX_REPORTED_MISSING_SESSIONS])
    if len(dates) > _MAX_REPORTED_MISSING_SESSIONS:
        return f"古い順 {_MAX_REPORTED_MISSING_SESSIONS} 件: {shown}, ...（全 {len(dates)} 件）"
    return shown


def _format_dropped_sessions_line(prefix: str, dropped: DroppedSessions) -> str:
    """One line naming a symbol and the sessions the provider stopped returning."""
    return f"{prefix}: {dropped.symbol}（{_truncated_dates(dropped.dates)}）\n"


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
    for dropped in result.dropped_sessions:
        sys.stdout.write(
            _format_dropped_sessions_line(
                "供給元が返さなくなったセッション（既存行は保持）", dropped
            )
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


def _market_store(args: argparse.Namespace) -> MarketStore:
    """The store `bars`/`fundamentals`/`rebuild` write through: `<db>`'s sibling `bars/`.

    Args:
        args: The parsed command line; `--db` names the database file.
    """
    return MarketStore(
        Database(args.db),
        parquet_root=Path(args.db).parent / "bars",
    )


def _read_only_audit_deps(args: argparse.Namespace) -> tuple[Database, MarketStore]:
    """The read-only collaborators `check`'s two audits share.

    Read-only because opening the database write-mode would ensure the
    fundamentals/corporate-actions tables on connect (`MarketStore.
    get_connection`), which would make an audit that writes nothing write
    something -- the property `check`'s own tests pin.

    Args:
        args: The parsed command line; `--db` names the database file.

    Returns:
        The shared `Database` and the `MarketStore` built on top of it.
    """
    database = Database(args.db, read_only=True)
    return database, MarketStore(database, parquet_root=Path(args.db).parent / "bars")


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
    for dropped in result.dropped_sessions:
        sys.stdout.write(
            _format_dropped_sessions_line(
                "供給元が返さなくなったセッション（rebuild により削除済み）", dropped
            )
        )
    if not result.replaced_symbols:
        # Nothing was replaced, so the store still holds whatever basis it
        # had -- and, crucially, no format marker was written over it.
        msg = "rebuild: 全銘柄の取得に失敗したため置き換えは行われませんでした。"
        raise BackfillError(msg)


def _write_entry_price_section(result: EntryPriceCheckResult) -> None:
    """Print `check`'s entry_price basis audit, appended after the bar audit.

    Args:
        result: The audit's own result; never raises and never writes.
    """
    if not result.findings:
        sys.stdout.write(
            f"entry_price: ok（対象 {result.scanned_rows} 行 / "
            f"{len(result.scanned_symbols)} 銘柄、基準ずれなし）\n"
        )
    else:
        sys.stdout.write(
            f"entry_price: 対象 {result.scanned_rows} 行 / "
            f"{len(result.scanned_symbols)} 銘柄 / "
            f"基準ずれ {len(result.findings)} 行\n"
        )
        for finding in result.findings:
            ratio = finding.frozen_entry_price / finding.bar_close
            sys.stdout.write(
                f"基準ずれ: {finding.symbol} {finding.run_date.isoformat()}"
                f"（凍結 {finding.frozen_entry_price:.6f} / "
                f"生バー終値 {finding.bar_close:.6f}、比 {ratio:.4f}）\n"
            )
        sys.stdout.write(
            "基準ずれの建玉は `copilot-track rebuild --symbol <SYMBOL>` "
            "で是正すること（凍結値ではなく同日バー終値が使われる）。\n"
        )
    if result.unresolved_rows > 0:
        sys.stdout.write(
            f"entry_price 判定不能: {result.unresolved_rows} 行"
            "（同日の生バーが store に無い）\n"
        )


def _run_check(args: argparse.Namespace) -> None:
    symbols = _resolve_explicit_symbols(args)
    database, store = _read_only_audit_deps(args)
    bars_result = check_bars(store, symbols)
    if bars_result.format_problem is not None:
        sys.stdout.write(f"形式マーカー: NG\n{bars_result.format_problem}\n")
        # A store predating the raw-bar model cannot be trusted for either
        # audit: the entry_price side does not run against it either.
        return
    sys.stdout.write("形式マーカー: ok（basis=raw, version=2）\n")
    if not bars_result.findings and not bars_result.missing_sessions:
        sys.stdout.write(
            f"check: ok（対象 {len(bars_result.scanned_symbols)} 銘柄、"
            "混在署名なし、欠損セッションなし）\n"
        )
    else:
        sys.stdout.write(
            f"check: 対象 {len(bars_result.scanned_symbols)} 銘柄 / "
            f"混在署名 {len(bars_result.findings)} 銘柄 / "
            f"欠損セッション {len(bars_result.missing_sessions)} 銘柄\n"
        )
        for finding in bars_result.findings:
            sys.stdout.write(
                f"混在署名: {finding.symbol}（最初のジャンプ "
                f"{finding.first_jump_date.isoformat()}）\n"
            )
        for missing in bars_result.missing_sessions:
            sys.stdout.write(
                f"欠損セッション: {missing.symbol} {len(missing.missing_dates)} 件"
                f"（{_truncated_dates(missing.missing_dates)}）\n"
            )
    _write_entry_price_section(check_entry_prices(database, store, symbols))


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
