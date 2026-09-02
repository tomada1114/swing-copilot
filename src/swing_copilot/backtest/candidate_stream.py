"""Candidate generation, separated from engine execution (Issue #185).

`copilot-backtest grid` runs the same screening 25 times: every cell varies
only engine-side parameters such as `exit_atr_multiple`/`max_hold_days`; the
entry-limit experiment also varies `entry_limit_atr_multiple`. The *screening
pipeline* never reads any of them. Screening is the dominant cost of a
multi-year backtest, so the grid paid for it 25 times over to produce 25
byte-identical candidate streams.

This module makes that stream a first-class value. `load_market_frame` reads
the point-in-time inputs once, `generate_candidate_stream` screens every
trading day once, and `run_backtest` accepts the result so an engine sweep
only pays for the engine. `compute_cache_key` fingerprints exactly the inputs
screening depends on -- never `settings.backtest`, `settings.risk`, or
`initial_cash` -- so a cached stream stays valid across an exit-parameter or
cost sweep, and `save_candidate_stream`/`load_candidate_stream` persist it to
Parquet so the reuse survives across CLI invocations.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pandas.api.types import is_numeric_dtype

from swing_copilot.backtest.policy import REGIME_SYMBOLS
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.io_atomic import write_bytes_atomically
from swing_copilot.screening.base import Candidate, ScreeningInput
from swing_copilot.screening.pipeline import (
    ScreeningPipeline,
    price_history_lookback_days,
    strategy_required_bars,
)
from swing_copilot.storage.json_guard import dumps_safe

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date
    from pathlib import Path

    from swing_copilot.backtest.runner import BacktestDependencies, BacktestRequest
    from swing_copilot.storage.market_store import MarketStore

#: Bump when the persisted layout or the cache-key composition changes, so an
#: on-disk cache written by an older build is rejected as a mismatch instead of
#: being silently reused against different semantics.
CACHE_KEY_VERSION = "1"

_CACHE_KEY_METADATA = b"swing_copilot.cache_key"
_CACHE_VERSION_METADATA = b"swing_copilot.cache_version"

_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("as_of", pa.date32()),
        pa.field("symbol", pa.string()),
        pa.field("rank", pa.int64()),
        pa.field("signal_names_json", pa.string()),
        pa.field("metrics_json", pa.string()),
        pa.field("execution_state", pa.string()),
        pa.field("execution_distance", pa.float64()),
    ]
)


class CandidateStreamError(SwingCopilotError):
    """Raised when a candidate-stream cache cannot be written or read."""


class CandidateStreamMismatchError(CandidateStreamError):
    """Raised when a supplied stream was not generated from these inputs."""


@dataclass(frozen=True, slots=True)
class MarketFrame:
    """One backtest's point-in-time inputs, plus their content digests.

    Loaded once and shared by every engine run over the same window, so a
    sweep reads bars/fundamentals from storage a single time. The digests are
    precomputed here because `compute_cache_key` is called once per engine run
    and must not re-hash the frames each time.
    """

    trading_days: tuple[date, ...]
    bars: pd.DataFrame
    fundamentals: pd.DataFrame
    benchmark_symbol: str
    bars_digest: str
    fundamentals_digest: str


@dataclass(frozen=True, slots=True)
class CandidateStream:
    """Every trading day's ranked candidates, precomputed.

    `candidates_by_day` holds each day's candidates in `ScreeningPipeline.run`
    order (rank ascending). A day whose screen produced nothing has no entry
    at all, so a lookup miss and an empty day are the same thing.
    """

    cache_key: str
    candidates_by_day: Mapping[date, tuple[Candidate, ...]]


def _trading_days(
    market_store: MarketStore, benchmark_symbol: str, start: date, end: date
) -> list[date]:
    bars = market_store.read_bars([benchmark_symbol], start, end, as_of=end)
    return sorted(bars["date"].unique().tolist())


def _frame_digest(frame: pd.DataFrame) -> str:
    """Fingerprint a DataFrame's content, independent of row order or process.

    Columns are taken in sorted order, and the per-row hashes are sorted rather
    than the rows themselves: the digest is then a function of the row
    *multiset*, so however storage happened to order the frame is irrelevant,
    and no whole-frame sort is paid for. Non-numeric columns are stringified
    first (dates, timestamps, and object columns otherwise hash by an unstable
    internal representation), while numeric columns keep their exact bits.
    `pandas` fixes its default `hash_key`, so the result is stable across
    processes too.

    Args:
        frame: Any tidy DataFrame; an empty one is handled.

    Returns:
        A hex digest of the column names and every row's content.
    """
    columns = sorted(frame.columns, key=str)
    hasher = hashlib.blake2b()
    hasher.update("\x1f".join(str(column) for column in columns).encode("utf-8"))
    if frame.empty:
        return hasher.hexdigest()

    normalized = pd.DataFrame(
        {
            str(name): series if is_numeric_dtype(series) else series.astype(str)
            for name, series in frame[columns].items()
        }
    )
    row_hashes = pd.util.hash_pandas_object(normalized, index=False).sort_values()
    hasher.update(row_hashes.to_numpy().tobytes())
    return hasher.hexdigest()


def load_market_frame(
    request: BacktestRequest,
    deps: BacktestDependencies,
    benchmark_symbol: str | None = None,
) -> MarketFrame:
    """Read one backtest window's trading days, bars, and fundamentals.

    Args:
        request: What to backtest (symbols, window, strategy).
        deps: Real collaborators (store, universe, settings, strategies).
        benchmark_symbol: Trading-day calendar source; defaults to
            `settings.backtest.benchmark`.

    Returns:
        The loaded frame, with both content digests already computed.
    """
    resolved_benchmark = benchmark_symbol or deps.settings.backtest.benchmark
    trading_days = _trading_days(
        deps.market_store, resolved_benchmark, request.start, request.end
    )
    # `REGIME_SYMBOLS` are loaded unconditionally, not only when a regime
    # policy is requested (Issue #184): they are part of the frame's content
    # digest, so making them conditional would give each `--policy` arm a
    # different cache key and force the A/B to re-screen per arm — the exact
    # opposite of what the comparison needs. Screening ignores them (it
    # iterates `universe`, not the bars frame).
    all_symbols = sorted({*request.symbols, resolved_benchmark, *REGIME_SYMBOLS})
    # Warmup sized from the strategy's own declared bar requirement, so the
    # backtest and the daily pipeline can never again screen the same code
    # over structurally different history windows (Issue #186).
    lookback_days = price_history_lookback_days(
        strategy_required_bars(
            deps.strategies_config, deps.settings, request.strategy_key
        )
    )
    bars_start = request.start - timedelta(days=lookback_days)
    bars = deps.market_store.read_bars(
        all_symbols, bars_start, request.end, as_of=request.end
    )
    fundamentals = deps.market_store.read_fundamentals(request.end)
    return MarketFrame(
        trading_days=tuple(trading_days),
        bars=bars,
        fundamentals=fundamentals,
        benchmark_symbol=resolved_benchmark,
        bars_digest=_frame_digest(bars),
        fundamentals_digest=_frame_digest(fundamentals),
    )


def compute_cache_key(
    request: BacktestRequest, deps: BacktestDependencies, frame: MarketFrame
) -> str:
    """Fingerprint everything screening depends on, and nothing else.

    The exclusions are the contract, not an optimization: `settings.trade_plan`,
    the simulation-only sizing values in `settings.backtest`, its costs,
    `settings.risk`, and `request.initial_cash` are consumed by
    `BacktestEngine`, never by `ScreeningPipeline`, so a sensitivity grid or a
    cost sweep must reuse one stream across all of its cells. Changing any of
    them deliberately leaves this key unchanged.

    `benchmark_symbol` *is* included: it is the source of the trading-day
    calendar the stream is keyed by.

    Args:
        request: What to backtest.
        deps: Real collaborators, including universe and settings.
        frame: The loaded market frame, supplying the content digests.

    Returns:
        A hex digest identifying this screening input exactly.

    Raises:
        KeyError: `request.strategy_key` is not present in the strategies
            configuration.
    """
    payload = {
        "version": CACHE_KEY_VERSION,
        "strategy_key": request.strategy_key,
        "strategy_spec": deps.strategies_config.strategies[
            request.strategy_key
        ].model_dump(),
        "technical_signals": deps.settings.technical_signals.model_dump(),
        "fundamental_filters": deps.settings.fundamental_filters.model_dump(),
        "universe": sorted(
            json.dumps(dataclasses.asdict(member), sort_keys=True, default=str)
            for member in deps.universe
        ),
        "symbols": sorted(request.symbols),
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "benchmark_symbol": frame.benchmark_symbol,
        "bars_digest": frame.bars_digest,
        "fundamentals_digest": frame.fundamentals_digest,
    }
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.blake2b(canonical.encode("utf-8")).hexdigest()


def generate_candidate_stream(
    request: BacktestRequest, deps: BacktestDependencies, frame: MarketFrame
) -> CandidateStream:
    """Screen every trading day in `frame` once, eagerly.

    The pipeline is built from `deps.settings` rather than from any
    cost/exit-override copy of it: screening reads only
    `settings.technical_signals` and `settings.fundamental_filters`, so an
    overridden `settings.backtest` cannot change a single candidate.

    Args:
        request: What to backtest.
        deps: Real collaborators (store, universe, settings, strategies).
        frame: The loaded market frame to screen over.

    Returns:
        The full day -> ranked-candidates mapping, tagged with its cache key.
    """
    pipeline = ScreeningPipeline(
        deps.strategies_config,
        deps.market_store,
        deps.settings,
        request.strategy_key,
    )
    fundamentals = frame.fundamentals
    candidates_by_day: dict[date, tuple[Candidate, ...]] = {}
    for day in frame.trading_days:
        # `filed_at` is TIMESTAMPTZ; a bare `date` can't be compared against
        # it directly (pandas raises TypeError). Match
        # `screening/fundamental_filters.py`'s end-of-day-UTC cutoff idiom for
        # an inclusive as-of boundary.
        day_cutoff = datetime.combine(day, time.max, tzinfo=UTC)
        point_in_time_fundamentals = (
            fundamentals[fundamentals["filed_at"] <= day_cutoff]
            if not fundamentals.empty
            else fundamentals
        )
        data = ScreeningInput(
            as_of=day,
            universe=deps.universe,
            fundamentals=point_in_time_fundamentals,
            # Bars are handed over whole, not pre-sliced to `day`. Screening
            # reads price history only through `indicators.symbol_window` /
            # `symbol_bars`, which always apply the `as_of` cutoff themselves,
            # so this cannot leak look-ahead --
            # `TestNoLookAheadFromPrecomputedIndicators` below pins that
            # equivalence day by day. Reusing one frame also lets those
            # functions cache the per-symbol index *and* the full-history
            # indicator columns across the whole run; re-slicing per day would
            # rebuild both on every simulated day and was the dominant cost of
            # a multi-year backtest (Issues #185, #214).
            bars=frame.bars,
        )
        candidates = pipeline.run(data)
        if candidates:
            candidates_by_day[day] = tuple(candidates)
    return CandidateStream(
        cache_key=compute_cache_key(request, deps, frame),
        candidates_by_day=candidates_by_day,
    )


def _stream_table(stream: CandidateStream) -> pa.Table:
    """Flatten a stream into the persisted `(as_of, rank)`-ordered table."""
    rows = sorted(
        (
            (day, candidate)
            for day, candidates in stream.candidates_by_day.items()
            for candidate in candidates
        ),
        key=lambda row: (row[0], row[1].rank),
    )
    try:
        signal_names_json = [
            dumps_safe(list(candidate.signal_names)) for _day, candidate in rows
        ]
        metrics_json = [dumps_safe(dict(candidate.metrics)) for _day, candidate in rows]
    except ValueError as exc:
        msg = f"候補ストリームに JSON 化できない値が含まれています: {exc}"
        raise CandidateStreamError(msg) from exc

    schema = _PARQUET_SCHEMA.with_metadata(
        {
            _CACHE_KEY_METADATA: stream.cache_key.encode("utf-8"),
            _CACHE_VERSION_METADATA: CACHE_KEY_VERSION.encode("utf-8"),
        }
    )
    return pa.Table.from_pydict(
        {
            "as_of": [day for day, _candidate in rows],
            "symbol": [candidate.symbol for _day, candidate in rows],
            "rank": [candidate.rank for _day, candidate in rows],
            "signal_names_json": signal_names_json,
            "metrics_json": metrics_json,
            "execution_state": [candidate.execution_state for _day, candidate in rows],
            "execution_distance": [
                candidate.execution_distance for _day, candidate in rows
            ],
        },
        schema=schema,
    )


def save_candidate_stream(stream: CandidateStream, path: Path) -> None:
    """Persist `stream` to `path` atomically (REQ-008).

    The cache key travels in the Parquet schema metadata, so a loaded stream
    can be validated against freshly computed inputs without re-screening.
    The Parquet bytes are staged in memory and then written through
    `io_atomic.write_bytes_atomically`, matching
    `storage/market_store.py::_write_partition`: a failed write leaves any
    previous cache untouched and removes the temporary file. The parent
    directory must already exist; the caller owns creating it.

    `--candidate-cache` (`backtest/cli.py`) exists precisely so the same
    path can be shared across separate `copilot-backtest` invocations, so
    the staging path is unique per call rather than `io_atomic`'s own
    deterministic `.{name}.tmp`: two processes racing a cache miss on the
    same destination must never stage into the same temporary file, or one
    could publish the other's partially-written body.

    Args:
        stream: The stream to persist.
        path: Destination Parquet file.

    Raises:
        CandidateStreamError: A candidate carries a value that cannot be
            serialized (e.g. a non-finite metric).
        OSError: The write or the replacement failed.
    """
    table = _stream_table(stream)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    write_bytes_atomically(
        path, buffer.getvalue(), temporary_path=_unique_temporary_path(path)
    )


def _unique_temporary_path(destination: Path) -> Path:
    """A same-directory staging path unique to this call, not just this name.

    `io_atomic`'s own default `.{name}.tmp` is deterministic, which is fine
    when only one writer ever targets a destination -- it is not fine here,
    where a shared `--candidate-cache` path can be written by two concurrent
    `copilot-backtest` processes.
    """
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")


def load_candidate_stream(path: Path) -> CandidateStream:
    """Read a stream previously written by `save_candidate_stream`.

    Args:
        path: The Parquet file to read.

    Returns:
        The reconstructed stream, carrying the cache key it was saved with.

    Raises:
        CandidateStreamError: The file is missing, unreadable, not a
            candidate-stream Parquet file, or carries no cache key.
    """
    try:
        raw_table = pq.read_table(path)
    except (OSError, pa.ArrowException) as exc:
        msg = f"候補ストリームキャッシュを読み込めません: {path}"
        raise CandidateStreamError(msg) from exc

    metadata = raw_table.schema.metadata or {}
    raw_cache_key = metadata.get(_CACHE_KEY_METADATA)
    if raw_cache_key is None:
        msg = f"候補ストリームキャッシュに cache_key メタデータがありません: {path}"
        raise CandidateStreamError(msg)

    by_day: defaultdict[date, list[Candidate]] = defaultdict(list)
    try:
        cache_key = raw_cache_key.decode("utf-8")
        table = raw_table.select(_PARQUET_SCHEMA.names).cast(_PARQUET_SCHEMA)
        for row in table.to_pylist():
            as_of = row["as_of"]
            distance = row["execution_distance"]
            by_day[as_of].append(
                Candidate(
                    symbol=row["symbol"],
                    as_of=as_of,
                    signal_names=tuple(json.loads(row["signal_names_json"])),
                    metrics={
                        key: float(value)
                        for key, value in json.loads(row["metrics_json"]).items()
                    },
                    rank=int(row["rank"]),
                    execution_state=row["execution_state"],
                    execution_distance=None
                    if distance is None or math.isnan(distance)
                    else float(distance),
                )
            )
    except (pa.ArrowException, KeyError, TypeError, ValueError) as exc:
        msg = f"候補ストリームキャッシュの形式が不正です: {path}"
        raise CandidateStreamError(msg) from exc

    return CandidateStream(
        cache_key=cache_key,
        candidates_by_day={
            day: tuple(sorted(candidates, key=lambda candidate: candidate.rank))
            for day, candidates in by_day.items()
        },
    )
