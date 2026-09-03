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
history is skipped and noted (design 3.24.3-3). `update_tracking` never
revisits an already-marked session -- `last_marked_date` is a resume point --
so replaying a position against corrected bars is a separate, explicit act:
`rebuild_positions` below deletes the position and reopens it from its
`verdicts` row (design 3.24.3-5, `copilot-track rebuild`).

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
    from uuid import UUID

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

#: How far a risk assessment's frozen `entry_price` may drift from the stored
#: bar's own close on that *same* entry date before it is treated as wrong.
#: This is not "how much can a real move differ" (there is no elapsed time
#: between the two figures to move in) -- both numbers claim to be the same
#: single session's close, so this is rounding tolerance between two paths
#: that recorded it, mirroring `market_store.py`'s `_MAX_CORRECTION_RATIO`.
_ENTRY_PRICE_BAR_TOLERANCE = 0.005


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

    # One splits read for the whole ledger, like `_read_bars`: `read_splits`
    # opens a connection per call, and DuckDB's file lock is exclusive. The
    # candidates are in here too, not just the open positions: a verdict being
    # opened for the first time carries frozen prices of its own (Issue #413).
    splits = market_store.read_splits(
        sorted(
            {position.symbol for position in open_positions}
            | {candidate.symbol for candidate in candidates}
        ),
        as_of=as_of,
    )

    pending: list[_Work] = []
    for candidate in candidates:
        work = _seed_position(bars, trade_plan, candidate, splits, notes)
        if work is not None:
            pending.append(work)
    opened_count = len(pending)

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


@dataclass(frozen=True, slots=True)
class RebuildTarget:
    """Which tracked positions one `rebuild_positions` call covers.

    `symbol` is required and `run_id` optional on purpose: a repair is always
    about one symbol's prices, and naming a single position is the narrowing
    step, not the normal case.
    """

    symbol: str
    run_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """One position's headline figures, for the before/after comparison.

    `return_pct` is the realized result on a closed position and the latest
    mark's unrealized figure on an open one -- the number the ledger would
    show for it either way, which is what a reader compares across a rebuild.
    `None` means an open position with no mark at all (no bars ever arrived).
    """

    status: str
    entry_price: float
    exit_date: date | None
    exit_reason: str | None
    return_pct: float | None


@dataclass(frozen=True, slots=True)
class RebuiltPosition:
    """One deleted-and-replayed position, before and after.

    `after` is `None` when the position did not come back: `update_tracking`
    reopens from the `verdicts` row, so this means the entry price could no
    longer be resolved (its `risk_assessments` row and its entry-day bar are
    both gone), and the note explaining it is on `RebuildResult.update`.
    """

    run_id: UUID
    symbol: str
    entry_date: date
    recommendation: str
    before: PositionSnapshot
    after: PositionSnapshot | None


@dataclass(frozen=True, slots=True)
class RebuildResult:
    """What one `rebuild_positions` call replaced.

    Empty `positions` means the target matched nothing -- a legitimate no-op
    (nothing was deleted and no replay was run), not an error.
    """

    target: RebuildTarget
    as_of: date
    positions: tuple[RebuiltPosition, ...]
    update: TrackingUpdateResult


def rebuild_positions(
    state_store: StateStore,
    market_store: MarketStore,
    trade_plan: TradePlanConfig,
    target: RebuildTarget,
    *,
    as_of: date,
) -> RebuildResult:
    """Delete the targeted positions and replay them from entry against stored bars.

    The repair path for a ledger built on prices that have since been
    corrected (Issue #413). `update_tracking` deliberately never revisits an
    already-marked session -- `last_marked_date` is a resume point, and a
    closed position is never advanced again -- so a stop that fired at a price
    nothing ever traded at stays in the ledger forever unless the position is
    rebuilt. Deleting it puts its `verdicts` row back in
    `get_untracked_verdicts`, and the ordinary open-and-advance path then
    reproduces it from `risk_assessments.entry_price` against today's bars.

    **This is not one transaction, and cannot be.** The delete commits, then
    the replay runs as its own set of writes. A failure in between leaves the
    positions deleted -- recoverable, because the `verdicts` rows are
    untouched, so the next `copilot-track update` reopens them exactly as this
    replay would have. What is lost in that window is only the old figures,
    which were the ones being discarded anyway.

    Every other open position advances to the same `as_of` as a side effect of
    the shared replay. That is idempotent by construction (`last_marked_date`
    is the resume point), so a rerun at the same `as_of` adds no marks.

    Args:
        state_store: Ledger to rebuild, and the verdict source it reopens from.
        market_store: Stored bars; nothing is fetched.
        trade_plan: Shared plan values used by production advice and the
            simulator.
        target: The symbol, and optionally the single run, to rebuild.
        as_of: Inclusive point-in-time cutoff for the replay, exactly as
            `update_tracking` uses it.

    Returns:
        The before/after figures per rebuilt position, plus the replay's own
        counts and notes. `positions` is empty when nothing matched.
    """
    selected = tuple(
        position
        for position in state_store.get_verdict_positions()
        if position.symbol == target.symbol
        and (target.run_id is None or position.run_id == target.run_id)
    )
    if not selected:
        return RebuildResult(
            target=target,
            as_of=as_of,
            positions=(),
            update=TrackingUpdateResult(0, 0, 0, ()),
        )

    marks_before = state_store.get_latest_verdict_position_marks()
    before = {
        (position.run_id, position.symbol): _snapshot(position, marks_before)
        for position in selected
    }
    state_store.delete_verdict_positions(
        [(position.run_id, position.symbol) for position in selected]
    )

    update = update_tracking(state_store, market_store, trade_plan, as_of=as_of)

    marks_after = state_store.get_latest_verdict_position_marks()
    rebuilt = {
        (position.run_id, position.symbol): position
        for position in state_store.get_verdict_positions()
    }
    return RebuildResult(
        target=target,
        as_of=as_of,
        positions=tuple(
            _rebuilt(position, before, rebuilt, marks_after) for position in selected
        ),
        update=update,
    )


def _rebuilt(
    position: VerdictPosition,
    before: Mapping[tuple[UUID, str], PositionSnapshot],
    rebuilt: Mapping[tuple[UUID, str], VerdictPosition],
    marks_after: Mapping[tuple[UUID, str], VerdictPositionMark],
) -> RebuiltPosition:
    """Pair one position's pre-delete figures with what the replay produced."""
    key = (position.run_id, position.symbol)
    after = rebuilt.get(key)
    return RebuiltPosition(
        run_id=position.run_id,
        symbol=position.symbol,
        entry_date=position.entry_date,
        recommendation=position.recommendation,
        before=before[key],
        after=None if after is None else _snapshot(after, marks_after),
    )


def _snapshot(
    position: VerdictPosition,
    marks: Mapping[tuple[UUID, str], VerdictPositionMark],
) -> PositionSnapshot:
    """Reduce one position to the figures a rebuild report compares."""
    mark = marks.get((position.run_id, position.symbol))
    return PositionSnapshot(
        status=position.status,
        entry_price=position.entry_price,
        exit_date=position.exit_date,
        exit_reason=position.exit_reason,
        return_pct=(
            position.realized_return_pct
            if position.status == CLOSED
            else (None if mark is None else mark.unrealized_return_pct)
        ),
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
        splits.get(position.symbol, ()),
        position.last_marked_date or position.entry_date,
        position_bars,
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
    splits: Sequence[SplitEvent], marked_through: date, bars: pd.DataFrame
) -> tuple[SplitEvent, ...]:
    """The splits this update has to re-base a position's frozen prices for.

    A split counts when its ex-date is newer than the last session already
    reflected in those figures -- everything on or before that date is
    already in them -- **and** the position has a stored session on or after
    it. That second clause is what makes the rebase idempotent: replaying
    that session moves `last_marked_date` to the ex-date or past it, so the
    next run with the same `as_of` finds nothing left to apply. Without it a
    symbol whose bars have stopped arriving would be re-scaled on every run.

    Args:
        splits: The symbol's splits visible at `as_of` (`read_splits` already
            dropped anything later).
        marked_through: The last session the frozen figures already reflect --
            `last_marked_date` for an open position, and the entry date for a
            verdict being opened for the first time, whose `risk_assessments`
            prices are quoted in the dollars that traded that day.
        bars: That position's own bar window, no row newer than `as_of`.

    Returns:
        The applicable splits, ascending by ex-date.
    """
    if not splits or bars.empty:
        return ()
    newest_session = max(bars["date"])
    return tuple(
        split
        for split in sorted(splits, key=lambda event: event.ex_date)
        if marked_through < split.ex_date <= newest_session
    )


def _split_factor(splits: Sequence[SplitEvent]) -> float:
    """The cumulative divisor `splits` put between frozen prices and the bars."""
    return math.prod(split.factor for split in splits)


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
    cumulative = _split_factor(splits)
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
    splits: Mapping[str, Sequence[SplitEvent]],
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

    Only the figures the *risk assessment* froze are re-based onto the bars'
    basis, and only for splits between the entry date and `as_of`: those are
    quoted in the dollars that traded on the run day, while `read_bars` has
    already divided the bars by every split visible at `as_of`. The two
    fallbacks are deliberately left alone -- a stand-in entry close read out
    of `bars`, and an ATR-derived stop computed from `bars`, are both on the
    bars' own basis already, so dividing them would re-base them twice.

    Rebasing a *newly opened* position matters because `copilot-track
    rebuild` reopens from the `verdicts` row (Issue #413): without this, a
    position whose symbol split after entry is replayed with a pre-split stop
    against post-split bars and stops out instantly at a loss the size of the
    split -- reproducing the very corruption the rebuild exists to remove.

    Once rebased onto today's basis, a frozen `entry_price` is also checked
    against the stored bar's own close for that same entry date (Issue #423):
    `risk_assessments.entry_price` is documented above to *be* the run day's
    close, so a same-day disagreement between the two is not a market move to
    interpret via a ratio -- it is definitionally wrong, whatever the ratio
    happens to be, because both numbers claim to be one single day's close.
    This is a same-day consistency check against the stored bar, structurally
    different from the ratio-based *rebase* this module's own docstring
    rejects: a mismatch beyond `_ENTRY_PRICE_BAR_TOLERANCE` falls back to the
    bar's close (the same value the None-entry-price path below already
    uses) and is recorded in `notes`, never silently substituted.
    """
    bars = _position_bars(all_bars, candidate.symbol, candidate.as_of)
    factor = _split_factor(
        _applicable_splits(splits.get(candidate.symbol, ()), candidate.as_of, bars)
    )
    entry_price = candidate.entry_price
    if entry_price is None:
        entry_price = _close_on(bars, candidate.symbol, candidate.as_of)
    else:
        if factor != 1.0:
            entry_price /= factor
        bar_close = _close_on(bars, candidate.symbol, candidate.as_of)
        if (
            bar_close is not None
            and bar_close > 0
            and abs(entry_price - bar_close) > bar_close * _ENTRY_PRICE_BAR_TOLERANCE
        ):
            notes.append(
                f"{candidate.symbol} {candidate.as_of.isoformat()}: "
                f"リスク評価の凍結エントリー価格（{entry_price:.6f}）が"
                f"同日の保存済みバー終値（{bar_close:.6f}）と一致しないため、"
                "バーの終値を使用した"
            )
            entry_price = bar_close
    if entry_price is None or entry_price <= 0:
        notes.append(
            f"{candidate.symbol} {candidate.as_of.isoformat()}: "
            "エントリー価格を解決できないため追跡を開始しない（次回再試行）"
        )
        return None

    stop_price = candidate.stop_price
    if stop_price is not None and factor != 1.0:
        stop_price /= factor
    if factor != 1.0:
        notes.append(
            f"{candidate.symbol} {candidate.as_of.isoformat()}: "
            f"エントリー後の分割（累積 {factor:g}倍）に合わせて"
            "建玉時の凍結価格を再基準化した"
        )
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
