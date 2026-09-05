"""Split arithmetic: Yahoo response -> raw bars, and raw bars -> as-of prices.

Pure functions over pandas frames, with no I/O and no clock: everything is
decided by the arguments, so the same inputs always give the same series.
Two directions live here, and they are inverses of each other:

* `unadjust_yahoo_bars` turns one provider response into **as-traded** bars.
  `yfinance.download(..., auto_adjust=False)` is documented to hand back
  split-adjusted (dividend-unadjusted) closes, so the raw price of a row is
  `close x cum`, where `cum` is the product of every split that took effect
  *after* that row. Yahoo does not always keep that promise, and the way it
  breaks has a shape (Issue #421): it fails to push **one** split back
  through the history, uniformly, and then patchily applies it to a handful
  of recent rows. So the response is read as one question -- *which splits
  has Yahoo propagated?* -- answered per split from the boundary at its own
  ex-date, rather than as a per-row guess over the whole series.
* `adjust_bars` turns stored raw bars back into the prices that were
  *visible* at an `as_of`: every split with `ex_date <= as_of` divides the
  prices (and multiplies the volume) of every row dated before its ex-date.
  Splits after `as_of` are invisible, which is what makes a stored bar's
  meaning independent of when it was read.

Dividends are recorded as events but never applied to a price
(`design-pit-prices.md` 3): holding periods here are capped at 25 sessions
and every fill is quoted in as-traded dollars, so a dividend-adjusted basis
would buy nothing and would silently disagree with `entry_price`.

`has_mixed_basis_signature` and `first_mixed_basis_jump` narrow a reversing
jump pair with two further, purely arithmetic checks (Issue #425), on top of
the split-sized-jump requirement above: **eligibility** -- only a split whose
`ex_date` falls *after* the pair's run can explain it, since Yahoo's mismatch
can only appear on rows before a split's own boundary -- and a **run-length
ceiling** (`_MAX_FLIP_RUN_SESSIONS`) -- a pair whose run holds for months or
years is a real, sustained price level, not a few misadjusted rows. Both
conditions are measured, not tuned: against this repository's 510-symbol
store they took the detector's false positives from 19 symbols to 2, with
every known true positive (MNST, Issue #421) still detected. The two symbols
that remain -- JKHY 1990-06-06 and WDC 2002-07-22 -- have a factor small
enough that a real round-trip and a basis flip are arithmetically
indistinguishable; narrowing further is out of this module's scope (Issue
#425's own "rejected alternatives" section).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: A one-session |log return| above this counts as a "jump". log(1.25) sits
#: above any plausible single-session move for a liquid US large cap and well
#: below the smallest split factor this module will classify.
_JUMP_LOG_THRESHOLD = math.log(1.25)
#: Two consecutive jumps whose ratios multiply back to ~1 are a basis flip,
#: not two real moves: a genuine +77% day (MRNA, 2026-08-19) is not followed
#: by a matching -44% day, and a real split shows up exactly once.
_REVERSAL_PRODUCT_LOW = 0.9
_REVERSAL_PRODUCT_HIGH = 1.1
#: Below this factor the "propagated" and "not propagated" readings of a
#: split's own boundary overlap, so the split is taken at Yahoo's word rather
#: than guessed at. Deliberately clear of `_PROPAGATION_TOLERANCE`: the two
#: bands must not touch, or an ordinary session could vote either way.
_MIN_CLASSIFIABLE_FACTOR = 1.2
#: How close a jump must sit to a split factor to count as a *basis flip*
#: rather than a price move. A flip is exact arithmetic, so the only slack
#: needed is the one session's real return that rides along with it: the six
#: flips in Issue #413's MNST response land within 4.1%.
_FLIP_FACTOR_TOLERANCE = math.log(1.06)
#: The same slack, for accepting the alternative hypothesis when the backward
#: walk changes its mind about a row.
_STRAY_FLIP_TOLERANCE = math.log(1.06)
#: How close a split's own boundary ratio must sit to its factor before the
#: split is called un-propagated. Looser than a flip's tolerance because the
#: ex-date session's return is unconstrained.
_PROPAGATION_TOLERANCE = math.log(1.12)
#: Sessions a reversing pair's run (the rows between the two jumps) may span
#: before it counts as a basis flip rather than a real sustained price level.
#: Issue #421's only observed defect (MNST) was 1-3 sessions scattered across
#: the three weeks before its ex-date; a run that holds for months or years
#: is the market actually pricing there for that long -- a crash and its
#: recovery, not a misadjusted row (Issue #425).
_MAX_FLIP_RUN_SESSIONS = 25
#: Sessions before an ex-date that vote on whether the split was propagated.
#: More than one, so a single row Yahoo quoted on the other basis -- exactly
#: what this module exists to handle -- cannot decide the question alone.
_PROPAGATION_SAMPLE = 5
#: Price columns a split divides. `volume` moves the other way.
_PRICE_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True, slots=True)
class SplitEvent:
    """One split: `factor` shares afterwards for every share before.

    2.0 for a 2:1 split, 0.1 for a 1:10 reverse split. `ex_date` is the first
    session that trades on the new basis, so a row dated *before* it is the
    one that needs adjusting.
    """

    ex_date: date
    factor: float


@dataclass(frozen=True, slots=True)
class NormalizationRejection:
    """Why one symbol's response could not be turned into raw bars.

    Fail-closed: the caller turns this into a non-retryable per-symbol
    failure, so a response whose adjustment basis cannot be resolved is left
    out of storage entirely rather than half-normalized.
    """

    symbol: str
    reason: str


def has_mixed_basis_signature(bars: pd.DataFrame, splits: Sequence[SplitEvent]) -> bool:
    """Whether `bars` looks like adjusted and unadjusted rows interleaved.

    The signature is a jump immediately followed (in the *jump* sequence, with
    any number of ordinary sessions in between) by a jump that undoes it,
    where **both jumps are the size of one of `splits`' factors**, the pair's
    run (the rows between the two jumps) is no longer than
    `_MAX_FLIP_RUN_SESSIONS` sessions, and some **single** split's `ex_date`
    falls after that run and whose `{factor, 1/factor}` explains both jumps
    (Issue #425). A real corporate action moves the series once and leaves it
    there; a real price shock is not mirrored; a mirrored pair that is not
    split-sized is ordinary volatility, which is all a long history ever
    offers; and a mirrored pair that *is* split-sized but whose run outlasts
    the ceiling, or whose only candidate split predates the run, is a real
    sustained price level (a crash and its recovery), not a misadjusted row.

    That last clause is what makes the gate usable over decades rather than
    over one rolling window (Issue #421). Scanned split-blind, 153 of this
    repository's 510 stored symbols carry a "mixed basis" that is nothing but
    2008 and the dot-com years -- `^VIX` and `^TNX` among them, which have no
    splits at all and therefore cannot have a basis to mix.

    Args:
        bars: One symbol's rows, ascending by `date`, with `date` and `close`
            columns. Non-positive and non-finite `close` values contribute no
            ratio rather than raising. Column presence is not defensively
            checked; every caller already carries both.
        splits: The splits that could have produced a flip in this series. An
            empty sequence means no flip is possible, so the answer is
            `False` without inspecting a single ratio.

    Returns:
        `True` when at least one eligible pair of consecutive split-sized
        jumps reverses.
    """
    return _first_reversing_flip(bars, splits) is not None


def first_mixed_basis_jump(
    bars: pd.DataFrame, splits: Sequence[SplitEvent]
) -> int | None:
    """Where `has_mixed_basis_signature` first sees the basis flip.

    The reporting counterpart of the boolean gate: `copilot-backfill check`
    has to tell an operator *which session* to look at, not merely that a
    symbol is broken. The two share one walk, so the gate every write depends
    on and the audit that explains it cannot drift apart.

    Args:
        bars: One symbol's rows, ascending by `date`, with `date` and `close`
            columns.
        splits: The splits that could have produced a flip in this series.

    Returns:
        The positional index of the *later* row of the first eligible jump
        that takes part in a reversing pair — the first session quoted on the
        other basis — or `None` when the series carries no signature.
    """
    return _first_reversing_flip(bars, splits)


def _flip_ratios(splits: Sequence[SplitEvent]) -> tuple[float, ...]:
    """Every jump ratio a basis flip could produce, both directions."""
    ratios: set[float] = set()
    for split in splits:
        if _is_usable(split.factor):
            ratios.add(split.factor)
            ratios.add(1.0 / split.factor)
    return tuple(ratios)


def _is_split_sized(ratio: float, flip_ratios: Sequence[float]) -> bool:
    """Whether one jump is the size of some split, within a flip's slack."""
    return any(
        abs(math.log(ratio / candidate)) <= _FLIP_FACTOR_TOLERANCE
        for candidate in flip_ratios
    )


def _bar_dates(bars: pd.DataFrame) -> list[date]:
    """`bars["date"]` as plain `date` values, accepting `date` or `Timestamp`."""
    return [pd.Timestamp(value).date() for value in bars["date"].to_numpy()]


def _explaining_split(
    first: float,
    second: float,
    splits: Sequence[SplitEvent],
    run_end: date,
) -> bool:
    """Whether some single split's `{factor, 1/factor}` explains both jumps.

    Eligibility (Issue #425): only a split whose `ex_date` falls *after*
    `run_end` (the run's last row) can be the cause. Yahoo's mismatch can
    only show up on rows *before* a split's own ex-date -- every row at or
    after it is already on the new basis under both readings, so it cannot be
    the flipped side of a pair. A split with `ex_date <= run_end` is
    therefore not a candidate at all, regardless of its factor.

    Both jumps of one pair must match the *same* split's factor pair, not two
    different splits' factors: one run's mismatch is one factor's worth of
    arithmetic.

    Args:
        first: The earlier jump's ratio.
        second: The later jump's ratio.
        splits: Every split known for the symbol.
        run_end: The date of the run's last row (`dates[j - 1]`).

    Returns:
        `True` once a qualifying split is found.
    """
    for split in splits:
        if not _is_usable(split.factor):
            continue
        if split.ex_date <= run_end:
            continue
        pair = (split.factor, 1.0 / split.factor)
        if _is_split_sized(first, pair) and _is_split_sized(second, pair):
            return True
    return False


def _first_reversing_flip(
    bars: pd.DataFrame, splits: Sequence[SplitEvent]
) -> int | None:
    """The shared walk behind the boolean gate and its reporting counterpart."""
    flip_ratios = _flip_ratios(splits)
    if not flip_ratios:
        return None
    values = pd.to_numeric(pd.Series(bars["close"]), errors="coerce").to_numpy(
        dtype=float
    )
    dates = _bar_dates(bars)
    jumps = [
        (position, later / earlier)
        for position, (earlier, later) in enumerate(pairwise(values), start=1)
        if _is_usable(earlier)
        and _is_usable(later)
        and abs(math.log(later / earlier)) > _JUMP_LOG_THRESHOLD
        and _is_split_sized(later / earlier, flip_ratios)
    ]
    for (i, first), (j, second) in pairwise(jumps):
        if not (_REVERSAL_PRODUCT_LOW <= first * second <= _REVERSAL_PRODUCT_HIGH):
            continue
        if j - i > _MAX_FLIP_RUN_SESSIONS:
            continue
        if _explaining_split(first, second, splits, dates[j - 1]):
            return i
    return None


def cumulative_split_factors(
    dates: pd.Series, splits: Sequence[SplitEvent], as_of: date
) -> pd.Series:
    """Per-row product of the splits that took effect after that row.

    Args:
        dates: One symbol's bar dates (any date-like dtype). The result keeps
            this index, so it can be used against the same frame.
        splits: That symbol's splits, in any order. Duplicates are multiplied
            in, so the caller owns de-duplication.
        as_of: Point-in-time cutoff — a split with `ex_date > as_of` did not
            exist yet and contributes nothing.

    Returns:
        A float series of cumulative factors, `1.0` where no split applies.
    """
    factors = pd.Series(1.0, index=dates.index, dtype=float)
    if not splits:
        return factors
    timestamps = pd.to_datetime(pd.Series(dates.to_numpy(), index=dates.index))
    for split in splits:
        if split.ex_date > as_of:
            continue
        earlier = timestamps < pd.Timestamp(split.ex_date)
        factors = factors.mask(earlier, factors * split.factor)
    return factors


def adjust_bars(
    bars: pd.DataFrame,
    splits_by_symbol: Mapping[str, Sequence[SplitEvent]],
    as_of: date,
) -> pd.DataFrame:
    """Return `bars` on the adjustment basis a reader saw at `as_of`.

    Args:
        bars: Raw bars, one or many symbols, with `symbol`, `date` and the
            OHLCV columns. Never mutated.
        splits_by_symbol: Each symbol's splits. A symbol absent from the
            mapping is returned unchanged.
        as_of: Point-in-time cutoff for which splits are visible.

    Returns:
        A new frame: prices divided by the cumulative factor, volume
        multiplied by it. An integer `volume` column stays integral.
    """
    if bars.empty or not splits_by_symbol:
        return bars.copy()

    factors = pd.Series(1.0, index=bars.index, dtype=float)
    for symbol, splits in splits_by_symbol.items():
        rows = bars["symbol"] == symbol
        if not rows.any():
            continue
        factors.loc[rows] = cumulative_split_factors(
            bars.loc[rows, "date"], splits, as_of
        )
    if bool((factors == 1.0).all()):
        return bars.copy()

    adjusted = bars.copy()
    for column in _PRICE_COLUMNS:
        if column in adjusted.columns:
            adjusted[column] = adjusted[column] / factors
    if "volume" in adjusted.columns:
        adjusted["volume"] = _scaled_volume(adjusted["volume"], factors)
    return adjusted


def unadjust_yahoo_bars(
    symbol: str, bars: pd.DataFrame, splits: Sequence[SplitEvent]
) -> pd.DataFrame | NormalizationRejection:
    """Turn one symbol's Yahoo response into as-traded (raw) bars.

    Yahoo's contract is that `close` already carries every split, so the raw
    price of a row is `close x cum`. Issue #413 showed the contract can break;
    Issue #421 showed *how* it breaks, and the shape is what makes it
    tractable. For MNST, Yahoo had propagated all five splits from 2005 to
    2023 through 36 years of history and had **not** propagated the sixth
    (2026-08-11), except to five scattered sessions in the three weeks before
    its ex-date. So the response is resolved in two steps:

    1. **Which splits did Yahoo propagate?** Asked per split, at its own
       ex-date. A propagated split leaves no step there — that is the entire
       point of an adjusted series — while an un-propagated one leaves a step
       the size of its factor. When every split is propagated, `raw = close x
       cum` is the answer and the function stops. That is where all but one
       of this repository's 510 stored symbols land.
    2. **Which individual rows did Yahoo get right anyway?** Only rows that an
       un-propagated split should have moved can disagree, and only those are
       walked, backwards from the newest bar — whose basis is forced, because
       no split in the response postdates it.

    Nothing is rejected for want of evidence: a split whose boundary says
    neither "propagated" nor "not propagated" is taken at Yahoo's word, which
    is what the pre-Issue-#413 code did unconditionally. Reverse splits and
    spin-offs land here routinely, because the economic move that comes with
    them swamps the mechanical one (AIG 2009, EXPE/TripAdvisor 2011). A
    symbol is rejected only on *positive* evidence that cannot be resolved:
    an un-propagated split whose rows still flip after classification.

    `cum` is deliberately *not* cut off at any `as_of`: Yahoo adjusts its
    history with every split known today, so undoing that has to use them
    all. This is safe only because the response window ends at "today" — both
    the daily run (`as_of + 1 day`) and a full rebuild do that, so a split
    after the window, which the response could not report, cannot exist.

    Args:
        symbol: The ticker, used in the rejection reason.
        bars: That symbol's rows, `data/base.BARS_COLUMNS`-shaped.
        splits: Splits observed in the same response.

    Returns:
        Raw bars in ascending date order, or a `NormalizationRejection`.
    """
    if bars.empty:
        return bars.copy()

    # Filtered once, so `cum`, the classification and the verification cannot
    # disagree about which splits exist. `yfinance_provider` already drops
    # zero and non-finite action values; this keeps a hand-built or
    # database-sourced split list from dividing by one.
    usable = [split for split in splits if _is_usable(split.factor)]
    working = bars.sort_values("date").reset_index(drop=True)
    working["date"] = pd.to_datetime(working["date"]).dt.date
    cumulative = cumulative_split_factors(working["date"], usable, as_of=date.max)

    unpropagated = _unpropagated_splits(working, usable)
    if not unpropagated:
        return _apply_basis(working, cumulative)

    # The factor Yahoo is missing on a *baseline* row: everything it failed to
    # push back past that row. A row it corrected anyway is missing nothing.
    missing = cumulative_split_factors(working["date"], unpropagated, as_of=date.max)
    is_corrected = _classify_corrected_rows(working["close"], missing)
    # What Yahoo actually applied to each row, which is what undoing it needs.
    applied = cumulative.where(is_corrected, cumulative / missing)
    # Yahoo's own domain, with the classification undone: continuous if the
    # classification explained every row, still flipping if it did not.
    reconstructed = working["close"] / missing.where(~is_corrected, 1.0)
    if has_mixed_basis_signature(working.assign(close=reconstructed), usable):
        return NormalizationRejection(
            symbol=symbol,
            reason=(
                "分割調整の混在を解消できない（未伝播の分割 "
                f"{_describe_splits(unpropagated)}）"
            ),
        )
    return _apply_basis(working, applied)


def _unpropagated_splits(
    bars: pd.DataFrame, splits: Sequence[SplitEvent]
) -> tuple[SplitEvent, ...]:
    """The splits Yahoo has *not* pushed back through this response's history.

    Read at each split's own ex-date, where the two readings are furthest
    apart: an adjusted series steps by nothing there, an unadjusted one steps
    by the factor. Several pre-ex sessions vote against the first post-ex
    session, so one row quoted on the other basis — the very defect being
    resolved — cannot swing the answer by itself.

    Args:
        bars: One symbol's rows, ascending, with `date` and `close`.
        splits: Splits observed in the same response, factors already known
            usable.

    Returns:
        The un-propagated splits, ascending by ex-date. Empty is the normal
        answer and means `close x cum` can be trusted.
    """
    dates = bars["date"].to_numpy()
    closes = [
        float(value)
        for value in pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)
    ]
    found: list[SplitEvent] = []
    for split in sorted(splits, key=lambda event: event.ex_date):
        if 1.0 / _MIN_CLASSIFIABLE_FACTOR < split.factor < _MIN_CLASSIFIABLE_FACTOR:
            # Indistinguishable from an ordinary session either way; Yahoo's
            # contract wins, exactly as it did before Issue #413.
            continue
        before = [
            position for position, day in enumerate(dates) if day < split.ex_date
        ][-_PROPAGATION_SAMPLE:]
        after = [position for position, day in enumerate(dates) if day >= split.ex_date]
        if not before or not after:
            continue
        if _votes_unpropagated(closes, before, after[0], split.factor):
            found.append(split)
    return tuple(found)


def _votes_unpropagated(
    closes: Sequence[float], before: Sequence[int], after: int, factor: float
) -> bool:
    """Whether a majority of pre-ex sessions see the factor still in the price."""
    votes = 0
    counted = 0
    for position in before:
        earlier, later = closes[position], closes[after]
        if not (_is_usable(earlier) and _is_usable(later)):
            continue
        counted += 1
        ratio = earlier / later
        # Both halves matter: "as far from 1 as a jump" rules out a propagated
        # split, "within tolerance of the factor" rules in an un-propagated one.
        if (
            abs(math.log(ratio / factor)) <= _PROPAGATION_TOLERANCE
            and abs(math.log(ratio)) > _PROPAGATION_TOLERANCE
        ):
            votes += 1
    return counted > 0 and votes * 2 > counted


def _classify_corrected_rows(closes: pd.Series, missing: pd.Series) -> pd.Series:
    """Which rows Yahoo adjusted correctly despite failing to propagate.

    Walks backwards from the newest bar, in Yahoo's *adjusted* domain rather
    than the as-traded one. That choice is the whole reason this terminates
    on a real series: an adjusted price series is continuous even across an
    ex-date, so a step in it means a basis flip, whereas the as-traded series
    steps by the factor at every split and offers the walk nothing to hold on
    to (Issue #421).

    The state carried is "did Yahoo correct this row", not a factor, so it
    survives a plateau boundary where the factor itself changes. It flips only
    when keeping it would leave a jump *and* flipping lands within a flip's
    slack — a genuine +45% session (MNST, 1996-05-06) satisfies the first and
    fails the second.

    Args:
        closes: Yahoo's closes, ascending, positionally indexed.
        missing: Per row, the factor Yahoo failed to propagate past it. `1.0`
            where no un-propagated split applies, which forces "baseline".

    Returns:
        A boolean series: `True` where Yahoo had already applied the
        un-propagated splits to that row.
    """
    values = pd.to_numeric(closes, errors="coerce").to_numpy(dtype=float)
    factors = missing.to_numpy(dtype=float)
    count = len(values)
    corrected = [False] * count
    settled = values[-1] / factors[-1] if _is_usable(factors[-1]) else values[-1]
    for position in range(count - 2, -1, -1):
        if factors[position] == 1.0:
            corrected[position] = False
            settled = values[position]
            continue
        carried = corrected[position + 1] and factors[position + 1] != 1.0
        keep = values[position] / (1.0 if carried else factors[position])
        flip = values[position] / (factors[position] if carried else 1.0)
        if (
            _log_distance(keep, settled) > _JUMP_LOG_THRESHOLD
            and _log_distance(flip, settled) <= _STRAY_FLIP_TOLERANCE
        ):
            corrected[position] = not carried
            settled = flip
        else:
            corrected[position] = carried
            settled = keep
    # A corrected run that reaches the first row has no flip *into* it, so
    # nothing says it is one: Yahoo cannot have applied a split it never
    # propagated to the oldest rows alone. It is a real price move misread.
    position = 0
    while position < count and corrected[position]:
        corrected[position] = False
        position += 1
    return pd.Series(corrected, index=closes.index)


def _describe_splits(splits: Sequence[SplitEvent]) -> str:
    """The splits a rejection message names, for an operator reading a report.

    Only ever called with the un-propagated splits, which a rejection has at
    least one of, so there is no empty case to word.
    """
    return ", ".join(
        f"ex_date={split.ex_date.isoformat()}, factor={split.factor}"
        for split in splits
    )


def _is_usable(value: float) -> bool:
    """Whether a price can take part in a ratio (finite and strictly positive)."""
    return math.isfinite(value) and value > 0.0


def _scaled_volume(volume: pd.Series, factors: pd.Series) -> pd.Series:
    """`volume * factors`, keeping an integer column integral."""
    scaled = volume.astype(float) * factors
    if pd.api.types.is_integer_dtype(volume.dtype):
        return scaled.round().astype(volume.dtype)
    return scaled


def _apply_basis(bars: pd.DataFrame, applied: pd.Series) -> pd.DataFrame:
    """Undo the split adjustment Yahoo actually applied to each row.

    Args:
        bars: One symbol's rows, ascending.
        applied: Per row, the factor Yahoo divided that row's prices by, so
            multiplying by it gives the as-traded price back. `1.0` for a row
            Yahoo left alone.

    Returns:
        A new frame on the as-traded basis.
    """
    raw = bars.copy()
    for column in _PRICE_COLUMNS:
        if column in raw.columns:
            raw[column] = raw[column] * applied
    if "volume" in raw.columns:
        raw["volume"] = _scaled_volume(raw["volume"], 1.0 / applied)
    return raw


def _log_distance(candidate: float, reference: float) -> float:
    """|log(candidate / reference)|, or infinity when the ratio is undefined."""
    if not (_is_usable(candidate) and _is_usable(reference)):
        return math.inf
    return abs(math.log(candidate / reference))
