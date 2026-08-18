"""Replay every tracked verdict forward one trading day at a time.

A verdict is treated as a purchase at that run's closing price, and from then
on the position is carried with **the backtest's own exit rules**:
`backtest/exits.py`'s `next_trailing_stop` and `evaluate_exit`, imported rather
than reimplemented, so the ledger the human reads every morning cannot drift
away from what the simulator would have done.

This layer is deliberately not `retro/verdict_outcomes` (a two-point 5/20
session classification of whether the verdict was right) and deliberately not
`paper/positions` (what a human actually decided to hold, the FR-11/CON-04
gate). It answers a third question: if this verdict had been followed
mechanically, where would the position stand today, and what would close it.

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
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from swing_copilot.analysis.safety import ForbiddenLanguageError, check_display_texts
from swing_copilot.backtest.exits import (
    ATR_PERIOD,
    atr14_as_of,
    atr14_by_date,
    evaluate_exit,
    next_trailing_stop,
)
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.storage.tracking_records import (
    CLOSED,
    OPEN,
    VerdictPosition,
    VerdictPositionMark,
    VerdictPositionNote,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from uuid import UUID

    import pandas as pd

    from swing_copilot.config import BacktestConfig
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore
    from swing_copilot.storage.tracking_records import TrackableVerdict

logger = logging.getLogger(__name__)

#: Exit reason reserved for a human overriding the mechanical rules.
MANUAL = "manual"

#: Calendar days of history read before a position's entry date. ATR(14) needs
#: 14 sessions plus the one before them for the first true range; 90 calendar
#: days covers roughly 60 sessions, which clears that even across holidays.
_LOOKBACK_DAYS = 90

_OHLC_KEYS = ("open", "low", "close")

#: Ratio deviation on the entry-date close above which a price move is
#: treated as a stock split rather than dividend-adjustment drift (exclusive:
#: exactly 10% does not rebase). `auto_adjust=True` also re-scales history for
#: every ex-dividend date, and US large-cap quarterly dividends run under 2%,
#: while even the smallest ordinary splits (3-for-2, 5-for-4) clear 10%.
_REBASE_THRESHOLD = 0.10


class TrackingError(SwingCopilotError):
    """Raised when a manual tracking write names a position it cannot act on."""


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
    backtest_config: BacktestConfig,
    *,
    as_of: date,
) -> TrackingUpdateResult:
    """Open new tracked positions and carry every open one forward to `as_of`.

    Args:
        state_store: Verdict source and tracking-ledger target.
        market_store: Stored bars; nothing is fetched.
        backtest_config: `exit_atr_multiple` and `max_hold_days`, the same
            values the simulator uses.
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
        work = _seed_position(bars, backtest_config, candidate, notes)
        if work is not None:
            pending.append(work)
    opened_count = len(pending)

    pending.extend(
        _rebased_work(state_store, bars, position, notes) for position in open_positions
    )

    advanced_count = closed_count = 0
    for work in pending:
        advanced = _advance(backtest_config, work, as_of, notes)
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
    notes: list[str],
) -> _Work:
    """Build one already-open position's `_Work`, rebasing it first if needed.

    Detection is self-contained here rather than depending on `write_bars`'s
    execution order (design P8-116): it only reads bars and the position's
    own stored state, both already in hand by the time `update_tracking`
    calls this.
    """
    position_bars = _position_bars(bars, position.symbol, position.entry_date)
    existing_marks = state_store.get_verdict_position_marks(
        position.run_id, position.symbol
    )
    rebased_position, rebased_marks = _rebase_position(
        position, position_bars, existing_marks, notes
    )
    return _Work(
        position=rebased_position, bars=position_bars, seed_marks=rebased_marks
    )


def _rebase_position(
    position: VerdictPosition,
    bars: pd.DataFrame,
    marks: Sequence[VerdictPositionMark],
    notes: list[str],
) -> tuple[VerdictPosition, tuple[VerdictPositionMark, ...]]:
    """Detect a stock split and rescale the position's frozen dollar figures.

    Bars are re-fetched with `auto_adjust=True` every run, so a split rewrites
    the whole stored history to post-split terms while `entry_price` /
    `stop_price`, frozen as absolute dollars at open, do not move on their
    own. This compares the stored `entry_price` against the (possibly
    rewritten) bar close on the same session; a deviation past
    `_REBASE_THRESHOLD` rescales `entry_price`, `stop_price`, and every mark
    already published for this position by the same ratio, so a rebased
    position is never stopped out against its pre-rebase basis. Called before
    `_advance` replays any session (REQ-008).

    Returns:
        The (possibly rebased) position, and the (possibly rebased) marks --
        empty when no rebase was needed, since nothing then needs rewriting.
    """
    if position.entry_price <= 0:
        notes.append(
            f"{position.symbol} {position.entry_date.isoformat()}: "
            "entry_priceが0以下のため価格再調整の判定をスキップした"
        )
        return position, ()

    bar_close = _close_on(bars, position.symbol, position.entry_date)
    if bar_close is None:
        notes.append(
            f"{position.symbol} {position.entry_date.isoformat()}: "
            "entry_dateのバーが参照窓に無いため価格再調整の判定をスキップした"
        )
        return position, ()

    ratio = bar_close / position.entry_price
    if abs(ratio - 1.0) <= _REBASE_THRESHOLD:
        return position, ()

    before_entry_price = position.entry_price
    rebased_position = replace(
        position,
        entry_price=position.entry_price * ratio,
        stop_price=(
            None if position.stop_price is None else position.stop_price * ratio
        ),
    )
    rebased_marks = tuple(
        replace(
            mark,
            close=mark.close * ratio,
            stop_price=None if mark.stop_price is None else mark.stop_price * ratio,
        )
        for mark in marks
    )
    notes.append(
        f"{position.symbol} {position.entry_date.isoformat()}: "
        f"価格再調整を検出（比率 {ratio:.6f}）、"
        f"entry_price {before_entry_price:.6f} → "
        f"{rebased_position.entry_price:.6f} に再基準化"
    )
    return rebased_position, rebased_marks


def _seed_position(
    all_bars: pd.DataFrame,
    config: BacktestConfig,
    candidate: TrackableVerdict,
    notes: list[str],
) -> _Work | None:
    """Build the entry state for a `proceed` verdict not yet tracked.

    The entry price is the risk assessment's (the run day's close). When that
    is missing -- a `CASH_PRIORITY` regime or a `not_calculable` assessment
    leaves it unset -- the run day's stored close stands in. With neither, the
    verdict simply stays untracked and the next update tries again.
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
        atr = atr14_as_of(bars, candidate.symbol, candidate.as_of)
        if atr is None:
            notes.append(
                f"{candidate.symbol} {candidate.as_of.isoformat()}: "
                f"ATR({ATR_PERIOD})を算出できずストップ未設定で追跡する"
                "（最大保有日数のみで手仕舞い判定）"
            )
        else:
            stop_price = entry_price - config.exit_atr_multiple * atr

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
    config: BacktestConfig, work: _Work, as_of: date, notes: list[str]
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
    atr_by_date = atr14_by_date(work.bars, position.symbol, as_of) if sessions else {}

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
            max_hold_days=config.max_hold_days,
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
        atr = atr_by_date.get(session_date)
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


# PLR0913: two stores plus the position's identity and the closing session.
# Bundling the identity into a value object would only add construction noise
# at the one call site that matters, the CLI.
def close_manually(  # noqa: PLR0913
    state_store: StateStore,
    market_store: MarketStore,
    *,
    run_id: UUID,
    symbol: str,
    as_of: date,
    note: str | None = None,
) -> VerdictPosition:
    """Close one tracked position by human decision, overriding the exit rules.

    Args:
        state_store: Tracking-ledger source and target.
        market_store: Stored bars used for the closing price.
        run_id: The run whose verdict opened the position.
        symbol: The position's ticker.
        as_of: The closing session; also the note's date.
        note: Optional reasoning, recorded alongside the close.

    Returns:
        The closed position as persisted.

    Raises:
        TrackingError: The position does not exist, is already closed, the
            requested close predates its entry or its last replayed session,
            or the accompanying memo is unusable. The memo is checked before
            anything is written, so a rejected one never leaves the position
            closed without the reasoning that was meant to explain it.
    """
    if note is not None:
        _check_note(note)
    position = _require_position(state_store, run_id, symbol)
    if position.status != OPEN:
        msg = f"{symbol} ({run_id}) は既に {position.exit_date} に手仕舞い済みである"
        raise TrackingError(msg)
    if as_of < position.entry_date:
        msg = (
            f"手仕舞い日 {as_of.isoformat()} は "
            f"エントリー日 {position.entry_date.isoformat()} より前にできない"
        )
        raise TrackingError(msg)
    # Back-dating past an already replayed session would leave the position
    # closed on one date while its marks, `days_held`, and resume position all
    # describe a later one -- a row that contradicts itself in `list`/`show`.
    last_marked_date = position.last_marked_date or position.entry_date
    if as_of < last_marked_date:
        msg = (
            f"手仕舞い日 {as_of.isoformat()} は "
            f"最終マーク日 {last_marked_date.isoformat()} より前にできない"
            "（前進済みの日次マークと矛盾するため）"
        )
        raise TrackingError(msg)

    exit_price = _close_on(
        market_store.read_bars([symbol], as_of, as_of, as_of), symbol, as_of
    )
    if exit_price is None:
        marks = state_store.get_verdict_position_marks(run_id, symbol)
        exit_price = marks[-1].close if marks else position.entry_price
        logger.info(
            "manual close of %s uses the last recorded mark: no bar on %s",
            symbol,
            as_of,
        )

    realized_return_pct = _return_pct(exit_price, position.entry_price)
    closed = replace(
        position,
        status=CLOSED,
        exit_date=as_of,
        exit_price=exit_price,
        exit_reason=MANUAL,
        realized_return_pct=realized_return_pct,
        last_marked_date=as_of,
    )
    state_store.upsert_verdict_position(
        closed,
        (
            VerdictPositionMark(
                run_id=run_id,
                symbol=symbol,
                as_of_date=as_of,
                close=exit_price,
                stop_price=position.stop_price,
                unrealized_return_pct=realized_return_pct,
            ),
        ),
    )
    if note is not None:
        record_note(
            state_store, run_id=run_id, symbol=symbol, note_date=as_of, note=note
        )
    return closed


def record_note(
    state_store: StateStore,
    *,
    run_id: UUID,
    symbol: str,
    note_date: date,
    note: str,
) -> None:
    """Record one dated judgement memo against a tracked position.

    A note is skill-authored text that `copilot-track show` prints verbatim,
    which makes this an output boundary like every other skill-to-user text
    channel: it goes through the same central CON-03 guard
    (`analysis/safety.check_display_texts`) rather than trusting the skill's
    instructions, and a violating memo is rejected outright rather than
    stored and rendered.

    Args:
        state_store: Tracking-ledger source and target.
        run_id: The run whose verdict opened the position.
        symbol: The position's ticker.
        note_date: The note's date, also its correction key.
        note: The memo text.

    Raises:
        TrackingError: The memo is blank, carries prohibited imperative
            trading language (CON-03), or names a position that is not tracked.
    """
    _check_note(note)
    _require_position(state_store, run_id, symbol)
    state_store.upsert_verdict_position_note(
        VerdictPositionNote(
            run_id=run_id, symbol=symbol, note_date=note_date, note=note
        )
    )


def _check_note(note: str) -> None:
    """Reject a memo that must never be stored, before anything is written.

    Shared by `record_note` and `close_manually` so the CON-03 boundary
    documented on the former cannot be bypassed through `close --note`.

    Args:
        note: The memo text.

    Raises:
        TrackingError: The memo is blank or violates CON-03.
    """
    if not note.strip():
        msg = "ノート本文が空である"
        raise TrackingError(msg)
    try:
        check_display_texts([note])
    except ForbiddenLanguageError as exc:
        msg = f"ノート本文が出力ポリシー(CON-03)に違反している: {exc}"
        raise TrackingError(msg) from exc


def _require_position(
    state_store: StateStore, run_id: UUID, symbol: str
) -> VerdictPosition:
    """Return the named tracked position, or explain that it is not tracked."""
    position = state_store.get_verdict_position(run_id, symbol)
    if position is None:
        msg = f"{symbol} ({run_id}) の追跡ポジションが存在しない"
        raise TrackingError(msg)
    return position
