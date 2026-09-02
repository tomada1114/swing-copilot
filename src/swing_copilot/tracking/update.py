"""Replay every tracked verdict forward one trading day at a time.

This ledger measures whether a judgement was right, not what actually got
traded (design decision #327): a verdict is treated as a purchase at that
run's closing price, unconditionally -- there is no fill simulation and no
gate on the planned `limit_price`. From then on the position is carried with
**the backtest's own exit rules**: `backtest/exits.py`'s `next_trailing_stop`
and `evaluate_exit`, imported rather than reimplemented, so the ledger the
human reads every morning cannot drift away from what the simulator would
have done.

This layer is deliberately not `retro/verdict_outcomes` (a two-point 5/20
session classification of whether the verdict was right). It answers a
different question: if this verdict had been followed mechanically, where
would the position stand today, and what would close it.

Both verdict sides are replayed (Issue #190). A `skip` position is a shadow:
nobody was ever told to buy it, and it exists only so the ledger can state
what the rejected candidates would have done under exactly the rules the
accepted ones were carried under. Identical rules are the whole point --
a counterfactual measured any other way is not one.

Everything here takes an explicit `as_of` and reads only stored bars: no
clock, no network. Bars are whatever `copilot-daily`'s price step already
persisted, and a symbol without them is reported as data quality rather than
guessed at: a position that has no bars at all is retried on every update
until one arrives, while a single unusable session inside an otherwise good
history is skipped and noted (design 3.24.3-2; replaying already-marked days
against corrected bars is the out-of-scope `--rebuild`, 3.24.3-4).

A position's `entry_price` and `stop_price` are frozen in the dollars that
traded on the day it opened, while `MarketStore.read_bars` hands back prices
on the basis visible at `as_of` -- so a split between the two re-bases the
bars underneath a stop that did not move, and the position would be closed
out at a price nothing ever traded at. The ledger therefore re-bases from the
*events* (`MarketStore.read_splits`), never from a price ratio: every split
whose ex-date falls after the last marked session divides the frozen prices
and every published mark by its factor. This is exact where the old ratio
heuristic was a guess -- it cannot mistake a dividend or a real 12% gap for a
corporate action, and it is not fooled by a store whose entry-day close never
moved (Issue #413, where the pre-split rows had simply been left unadjusted).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from swing_copilot.backtest.entries import initial_stop_price
from swing_copilot.backtest.exits import (
    atr_as_of,
    atr_by_date,
    evaluate_exit,
    next_trailing_stop,
)
from swing_copilot.storage.tracking_records import (
    CLOSED,
    OPEN,
    VerdictPosition,
    VerdictPositionMark,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

    import pandas as pd

    from swing_copilot.config import TradePlanConfig
    from swing_copilot.data.adjustments import SplitEvent
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore
    from swing_copilot.storage.tracking_records import TrackableVerdict

logger = logging.getLogger(__name__)

#: Calendar days of history read before a position's entry date. ATR(14) needs
#: 14 sessions plus the one before them for the first true range; 90 calendar
#: days covers roughly 60 sessions, which clears that even across holidays.
_LOOKBACK_DAYS = 90

_OHLC_KEYS = ("open", "low", "close")


@dataclass(frozen=True, slots=True)
class TrackingUpdateResult:
    """What one `update_tracking` pass opened, advanced, and closed.

    `notes` carries the per-symbol data-quality reasons (an unresolvable entry
    price, a missing or malformed bar). They are reported rather than raised:
    one symbol without prices must not stop the rest of the ledger, and the
    next update retries it.
    """

    opened_count: int
    advanced_count: int
    closed_count: int
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Work:
    """One position to advance, with the bars and seed marks already in hand."""

    position: VerdictPosition
    bars: pd.DataFrame
    seed_marks: tuple[VerdictPositionMark, ...]


@dataclass(frozen=True, slots=True)
class _Advanced:
    """The outcome of replaying one position up to `as_of`."""

    position: VerdictPosition
    marks: tuple[VerdictPositionMark, ...]
    day_count: int


def update_tracking(
    state_store: StateStore,
    market_store: MarketStore,
    trade_plan: TradePlanConfig,
    *,
    as_of: date,
) -> TrackingUpdateResult:
    """Open new tracked positions and carry every open one forward to `as_of`.

    Args:
        state_store: Verdict source and tracking-ledger target.
        market_store: Stored bars; nothing is fetched.
        trade_plan: Shared plan values used by production advice and the
            simulator.
        as_of: Inclusive point-in-time cutoff. No bar dated later is read, and
            no verdict from a later run is opened.

    Returns:
        Counts plus one note per symbol that could not be processed.
    """
    notes: list[str] = []
    for run_id, symbol in state_store.delete_orphaned_verdict_positions():
        notes.append(
            f"{symbol} ({run_id}): verdict 行が消えたため追跡ポジションを削除した"
        )
    for (
        run_id,
        symbol,
        recommendation,
    ) in state_store.sync_verdict_position_recommendations():
        notes.append(
            f"{symbol} ({run_id}): verdict が {recommendation} に訂正されたため"
            "追跡ポジションの区分を追随させた"
        )

    candidates = state_store.get_untracked_verdicts(as_of)
    open_positions = state_store.get_verdict_positions(OPEN)
    if not candidates and not open_positions:
        return TrackingUpdateResult(0, 0, 0, tuple(notes))
    bars = _read_bars(market_store, candidates, open_positions, as_of)

    pending: list[_Work] = []
    for candidate in candidates:
        work = _seed_position(bars, trade_plan, candidate, notes)
        if work is not None:
            pending.append(work)
    opened_count = len(pending)

    # One splits read for the whole ledger, like `_read_bars`: `read_splits`
    # opens a connection per call, and DuckDB's file lock is exclusive.
    splits = market_store.read_splits(
        sorted({position.symbol for position in open_positions}), as_of=as_of
    )
    pending.extend(
        _rebased_work(state_store, bars, position, splits, notes)
        for position in open_positions
    )

    advanced_count = closed_count = 0
    for work in pending:
        advanced = _advance(trade_plan, work, as_of, notes)
        state_store.upsert_verdict_position(advanced.position, advanced.marks)
        if advanced.day_count > 0:
            advanced_count += 1
        if advanced.position.status == CLOSED:
            closed_count += 1

    return TrackingUpdateResult(
        opened_count=opened_count,
        advanced_count=advanced_count,
        closed_count=closed_count,
        notes=tuple(notes),
    )


def _read_bars(
    market_store: MarketStore,
    candidates: Sequence[TrackableVerdict],
    positions: Sequence[VerdictPosition],
    as_of: date,
) -> pd.DataFrame:
    """Read every tracked symbol's bars in a single query.

    One read rather than one per position: `MarketStore.read_bars` opens a
    connection and rebuilds the `bars` view on each call, a cost a ledger
    holding a hundred positions would otherwise pay a hundred times over
    inside the daily batch's time budget. Each position's own window is cut
    back out of the result by `_position_bars`.
    """
    starts = [candidate.as_of for candidate in candidates]
    starts.extend(position.entry_date for position in positions)
    symbols = {candidate.symbol for candidate in candidates}
    symbols.update(position.symbol for position in positions)
    return market_store.read_bars(
        sorted(symbols), min(starts) - timedelta(days=_LOOKBACK_DAYS), as_of, as_of
    )


def _position_bars(bars: pd.DataFrame, symbol: str, entry_date: date) -> pd.DataFrame:
    """Cut one position's own window out of the batched frame.

    The start bound stays per position rather than shared. Wilder ATR is
    smoothed over whatever history it is handed, so letting a late entrant see
    an earlier position's warm-up would move the very stop the ledger has to
    agree with the backtest on.
    """
    return bars[
        (bars["symbol"] == symbol)
        & (bars["date"] >= entry_date - timedelta(days=_LOOKBACK_DAYS))
    ]


def _rebased_work(
    state_store: StateStore,
    bars: pd.DataFrame,
    position: VerdictPosition,
    splits: Mapping[str, Sequence[SplitEvent]],
    notes: list[str],
) -> _Work:
    """Build one already-open position's `_Work`, rebasing it first if needed.

    Args:
        state_store: Source of the marks already published for this position.
        bars: The batched frame every tracked symbol was read into.
        position: The open position to carry forward.
        splits: Every tracked symbol's splits visible at `as_of`.
        notes: Data-quality/rebase notes, appended to in place.

    Returns:
        The work item `_advance` replays, already on the current basis.
    """
    position_bars = _position_bars(bars, position.symbol, position.entry_date)
    applicable = _applicable_splits(
        splits.get(position.symbol, ()), position, position_bars
    )
    if not applicable:
        return _Work(position=position, bars=position_bars, seed_marks=())
    existing_marks = state_store.get_verdict_position_marks(
        position.run_id, position.symbol
    )
    rebased_position, rebased_marks = _rebase_position(
        position, applicable, existing_marks, notes
    )
    return _Work(
        position=rebased_position, bars=position_bars, seed_marks=rebased_marks
    )


def _applicable_splits(
    splits: Sequence[SplitEvent], position: VerdictPosition, bars: pd.DataFrame
) -> tuple[SplitEvent, ...]:
    """The splits this update has to re-base `position` for.

    A split counts when its ex-date is newer than the last session already
    marked -- everything on or before that date is already reflected in the
    stored figures -- **and** the position has a stored session on or after
    it. That second clause is what makes the rebase idempotent: replaying
    that session moves `last_marked_date` to the ex-date or past it, so the
    next run with the same `as_of` finds nothing left to apply. Without it a
    symbol whose bars have stopped arriving would be re-scaled on every run.

    Args:
        splits: The symbol's splits visible at `as_of` (`read_splits` already
            dropped anything later).
        position: The open position being carried forward.
        bars: That position's own bar window, no row newer than `as_of`.

    Returns:
        The applicable splits, ascending by ex-date.
    """
    if not splits or bars.empty:
        return ()
    marked_through = position.last_marked_date or position.entry_date
    newest_session = max(bars["date"])
    return tuple(
        split
        for split in sorted(splits, key=lambda event: event.ex_date)
        if marked_through < split.ex_date <= newest_session
    )


def _rebase_position(
    position: VerdictPosition,
    splits: Sequence[SplitEvent],
    marks: Sequence[VerdictPositionMark],
    notes: list[str],
) -> tuple[VerdictPosition, tuple[VerdictPositionMark, ...]]:
    """Rescale the position's frozen dollar figures onto the current basis.

    `entry_price`, `stop_price` and every published mark are quoted in the
    dollars that traded when they were written, while `read_bars` returns the
    basis visible at `as_of`. Dividing them all by the product of the splits
    since the last mark puts the whole position back on the bars' own scale,
    so `_advance` can never test a post-split low against a pre-split stop
    (REQ-008). A `None` stop stays `None`: it means "no stop was ever set",
    which no amount of rescaling changes.

    Args:
        position: The open position, on its pre-split basis.
        splits: The splits to apply, from `_applicable_splits` (never empty).
        marks: Every mark already published for this position.
        notes: Rebase notes, appended to in place.

    Returns:
        The rebased position and its rebased marks.
    """
    cumulative = math.prod(split.factor for split in splits)
    if cumulative == 1.0:
        # A 1-for-1 "split" (or two that cancel): nothing to rescale, and
        # rewriting every mark for a no-op would only add churn.
        return position, ()

    before_entry_price = position.entry_price
    rebased_position = replace(
        position,
        entry_price=position.entry_price / cumulative,
        stop_price=(
            None if position.stop_price is None else position.stop_price / cumulative
        ),
    )
    rebased_marks = tuple(
        replace(
            mark,
            close=mark.close / cumulative,
            stop_price=(
                None if mark.stop_price is None else mark.stop_price / cumulative
            ),
        )
        for mark in marks
    )
    described = "、".join(
        f"ex_date={split.ex_date.isoformat()}, factor={split.factor:g}"
        for split in splits
    )
    notes.append(
        f"{position.symbol} {position.entry_date.isoformat()}: "
        f"株式分割（{described}）により再基準化"
        f"（entry_price {before_entry_price:.6f} → "
        f"{rebased_position.entry_price:.6f}）"
    )
    return rebased_position, rebased_marks


def _seed_position(
    all_bars: pd.DataFrame,
    config: TradePlanConfig,
    candidate: TrackableVerdict,
    notes: list[str],
) -> _Work | None:
    """Build the entry state for a `proceed`/`skip` verdict not yet tracked.

    The entry price is the risk assessment's reference close (the run day's
    close), taken unconditionally -- never its planned `limit_price`, and
    never gated on whether that limit would have filled (design decision
    #327). When the reference close is missing -- a `CASH_PRIORITY` regime or
    a `not_calculable` assessment leaves it unset -- the run day's stored
    close stands in. With neither, the verdict simply stays untracked and the
    next update tries again.
    """
    bars = _position_bars(all_bars, candidate.symbol, candidate.as_of)
    entry_price = candidate.entry_price
    if entry_price is None:
        entry_price = _close_on(bars, candidate.symbol, candidate.as_of)
    if entry_price is None or entry_price <= 0:
        notes.append(
            f"{candidate.symbol} {candidate.as_of.isoformat()}: "
            "エントリー価格を解決できないため追跡を開始しない（次回再試行）"
        )
        return None

    stop_price = candidate.stop_price
    if stop_price is None:
        atr = atr_as_of(bars, candidate.symbol, candidate.as_of, config.exit_atr_period)
        if atr is None:
            notes.append(
                f"{candidate.symbol} {candidate.as_of.isoformat()}: "
                f"ATR({config.exit_atr_period})を算出できずストップ未設定で追跡する"
                "（最大保有日数のみで手仕舞い判定）"
            )
        else:
            stop_price = initial_stop_price(entry_price, atr, config.exit_atr_multiple)

    position = VerdictPosition(
        run_id=candidate.run_id,
        symbol=candidate.symbol,
        strategy_key=candidate.strategy_key,
        recommendation=candidate.recommendation,
        no_trade=candidate.no_trade,
        entry_date=candidate.as_of,
        entry_price=entry_price,
        stop_price=stop_price,
        days_held=0,
        status=OPEN,
        last_marked_date=candidate.as_of,
        # Freeze the holding rule at entry for both replay and public display.
        max_hold_days=config.max_hold_days,
    )
    seed_mark = VerdictPositionMark(
        run_id=candidate.run_id,
        symbol=candidate.symbol,
        as_of_date=candidate.as_of,
        close=entry_price,
        stop_price=stop_price,
        unrealized_return_pct=0.0,
    )
    return _Work(position=position, bars=bars, seed_marks=(seed_mark,))


def _advance(
    config: TradePlanConfig, work: _Work, as_of: date, notes: list[str]
) -> _Advanced:
    """Replay one position from its last marked day up to `as_of`.

    The engine's ordering is preserved exactly: a day is first tested against
    the stop that was already in force, and only a day that survives ratchets
    the stop from its own close -- so the stop computed on day *d* can never
    close the position on day *d* itself.
    """
    position = work.position
    marks = list(work.seed_marks)
    resume_after = position.last_marked_date or position.entry_date
    day_count = 0
    if work.bars.empty:
        notes.append(
            f"{position.symbol} {position.entry_date.isoformat()}: "
            "保存済みバーが1本も無いため前進も手仕舞い判定もできない"
            "（上場廃止・ユニバース離脱の可能性。手動クローズを検討する）"
        )
    sessions = _sessions(work.bars, position.symbol, resume_after, as_of)
    # One smoothing pass for the whole replay, and none at all on the common
    # rerun where `last_marked_date` already sits on `as_of`.
    atr_per_session = (
        atr_by_date(work.bars, position.symbol, as_of, config.exit_atr_period)
        if sessions
        else {}
    )

    for record in sessions:
        session_date: date = record["date"]
        ohlc = _ohlc(record)
        if ohlc is None:
            notes.append(
                f"{position.symbol} {session_date.isoformat()}: "
                "バーが欠損しているため当日を飛ばした"
            )
            continue
        open_price, low, close = ohlc
        day_count += 1

        decision = evaluate_exit(
            open_price=open_price,
            low=low,
            close=close,
            stop_price=position.stop_price,
            days_held=position.days_held,
            max_hold_days=position.max_hold_days,
        )
        if decision is not None:
            position = replace(
                position,
                status=CLOSED,
                exit_date=session_date,
                exit_price=decision.exit_price,
                exit_reason=decision.reason,
                realized_return_pct=_return_pct(
                    decision.exit_price, position.entry_price
                ),
                last_marked_date=session_date,
            )
            marks.append(_mark(position, session_date, close, position.stop_price))
            break

        stop_price = position.stop_price
        atr = atr_per_session.get(session_date)
        if atr is not None:
            stop_price = next_trailing_stop(
                current_stop=stop_price,
                close=close,
                atr=atr,
                exit_atr_multiple=config.exit_atr_multiple,
            )
        position = replace(
            position,
            days_held=position.days_held + 1,
            stop_price=stop_price,
            last_marked_date=session_date,
        )
        marks.append(_mark(position, session_date, close, stop_price))

    return _Advanced(position=position, marks=tuple(marks), day_count=day_count)


def _sessions(
    bars: pd.DataFrame, symbol: str, resume_after: date, as_of: date
) -> list[dict[Any, Any]]:  # Any: pandas records are heterogeneous by column
    """Return the symbol's stored sessions in `(resume_after, as_of]`, in order."""
    if bars.empty:
        return []
    selected = bars[
        (bars["symbol"] == symbol)
        & (bars["date"] > resume_after)
        & (bars["date"] <= as_of)
    ].sort_values("date")
    return selected.to_dict("records")


def _ohlc(record: dict[Any, Any]) -> tuple[float, float, float] | None:
    """Return `(open, low, close)`, or `None` when any of them is unusable.

    A stored bar always has every column (`MarketStore` writes the full tidy
    schema); a missing price arrives as `NaN`, which is what makes the day
    unusable rather than zero.
    """
    open_price, low, close = (float(record[key]) for key in _OHLC_KEYS)
    if not all(math.isfinite(value) for value in (open_price, low, close)):
        return None
    return open_price, low, close


def _mark(
    position: VerdictPosition,
    session_date: date,
    close: float,
    stop_price: float | None,
) -> VerdictPositionMark:
    """Build one day's mark, always measured close-to-close from the entry.

    Even the day a stop fills intraday is marked at its close: a mark is the
    position's mark-to-market series, while the realized figure the exit
    produced lives on the position row.
    """
    return VerdictPositionMark(
        run_id=position.run_id,
        symbol=position.symbol,
        as_of_date=session_date,
        close=close,
        stop_price=stop_price,
        unrealized_return_pct=_return_pct(close, position.entry_price),
    )


def _return_pct(price: float, entry_price: float) -> float:
    """Return the percentage move from `entry_price` to `price`."""
    return (price - entry_price) / entry_price * 100.0


def _close_on(bars: pd.DataFrame, symbol: str, session_date: date) -> float | None:
    """Return one session's stored close, or `None` when it is unusable."""
    if bars.empty:
        return None
    selected = bars[(bars["symbol"] == symbol) & (bars["date"] == session_date)]
    if selected.empty:
        return None
    close = float(selected["close"].to_numpy()[-1])
    return close if math.isfinite(close) else None
