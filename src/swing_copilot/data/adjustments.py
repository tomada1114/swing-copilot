"""Split arithmetic: Yahoo response -> raw bars, and raw bars -> as-of prices.

Pure functions over pandas frames, with no I/O and no clock: everything is
decided by the arguments, so the same inputs always give the same series.
Two directions live here, and they are inverses of each other:

* `unadjust_yahoo_bars` turns one provider response into **as-traded** bars.
  `yfinance.download(..., auto_adjust=False)` is documented to hand back
  split-adjusted (dividend-unadjusted) closes, so the raw price of a row is
  `close x cum`, where `cum` is the product of every split that took effect
  *after* that row. Yahoo does not always keep that promise -- Issue #413's
  MNST response applied the 2026-08-11 2:1 split to some July/August rows and
  not to others, inside one response -- so a mixed series is classified row by
  row and, when that fails, the symbol is rejected rather than stored.
* `adjust_bars` turns stored raw bars back into the prices that were
  *visible* at an `as_of`: every split with `ex_date <= as_of` divides the
  prices (and multiplies the volume) of every row dated before its ex-date.
  Splits after `as_of` are invisible, which is what makes a stored bar's
  meaning independent of when it was read.

Dividends are recorded as events but never applied to a price
(`design-pit-prices.md` 3): holding periods here are capped at 25 sessions
and every fill is quoted in as-traded dollars, so a dividend-adjusted basis
would buy nothing and would silently disagree with `entry_price`.
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
#: Below this factor the "adjusted" and "unadjusted" hypotheses for a row are
#: too close together to choose between, so the symbol is rejected instead of
#: guessed at.
_MIN_CLASSIFIABLE_FACTOR = 1.2
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


def has_mixed_basis_signature(closes: pd.Series) -> bool:
    """Whether `closes` looks like adjusted and unadjusted rows interleaved.

    The signature is a jump immediately followed (in the *jump* sequence, with
    any number of ordinary sessions in between) by a jump that undoes it: the
    two ratios multiply back to roughly 1. A real corporate action moves the
    series once and leaves it there; a real price shock is not mirrored.

    Args:
        closes: Closing prices in ascending date order. Non-positive and
            non-finite values contribute no ratio rather than raising.

    Returns:
        `True` when at least one pair of consecutive jumps reverses.
    """
    values = pd.to_numeric(pd.Series(closes), errors="coerce").to_numpy(dtype=float)
    jumps = [
        later / earlier
        for earlier, later in pairwise(values)
        if _is_usable(earlier)
        and _is_usable(later)
        and abs(math.log(later / earlier)) > _JUMP_LOG_THRESHOLD
    ]
    return any(
        _REVERSAL_PRODUCT_LOW <= first * second <= _REVERSAL_PRODUCT_HIGH
        for first, second in pairwise(jumps)
    )


def first_mixed_basis_jump(closes: pd.Series) -> int | None:
    """Where `has_mixed_basis_signature` first sees the basis flip.

    The reporting counterpart of the boolean gate: `copilot-backfill check`
    has to tell an operator *which session* to look at, not merely that a
    symbol is broken. Deliberately a separate walk rather than a refactor of
    `has_mixed_basis_signature`, so the gate every write depends on keeps
    exactly the behaviour its own tests pin.

    Args:
        closes: Closing prices in ascending date order.

    Returns:
        The positional index of the *later* row of the first jump that takes
        part in a reversing pair — the first session quoted on the other
        basis — or `None` when the series carries no signature.
    """
    values = pd.to_numeric(pd.Series(closes), errors="coerce").to_numpy(dtype=float)
    jumps = [
        (position, later / earlier)
        for position, (earlier, later) in enumerate(pairwise(values), start=1)
        if _is_usable(earlier)
        and _is_usable(later)
        and abs(math.log(later / earlier)) > _JUMP_LOG_THRESHOLD
    ]
    for (position, first), (_, second) in pairwise(jumps):
        if _REVERSAL_PRODUCT_LOW <= first * second <= _REVERSAL_PRODUCT_HIGH:
            return position
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

    Three passes, each falling through to the next only when the cheaper one
    cannot be trusted:

    1. **Fast path.** Assume Yahoo adjusted every row, i.e. `raw = close x
       cum`. If that series carries no mixed-basis signature, it is the
       answer. With no split in the window `cum` is 1 everywhere and this is
       always where the function stops.
    2. **Classification.** Otherwise decide per row which of the two
       hypotheses (`close x cum` or `close`) continues the series most
       smoothly, anchored at the newest bar — the one row that is
       unambiguously as-traded, because no split in the response postdates
       it.
    3. **Rejection.** If the signature survives classification, or a split
       small enough to make the two hypotheses indistinguishable is involved,
       the symbol is rejected.

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

    working = bars.sort_values("date").reset_index(drop=True)
    working["date"] = pd.to_datetime(working["date"]).dt.date
    cumulative = cumulative_split_factors(working["date"], splits, as_of=date.max)

    fast = _apply_basis(working, cumulative, pd.Series(True, index=working.index))
    if not has_mixed_basis_signature(fast["close"]):
        return fast

    ambiguous = _ambiguous_split(working["date"].iloc[0], splits)
    if ambiguous is not None:
        return NormalizationRejection(
            symbol=symbol,
            reason=(
                "分割調整の混在を解消できない（分類不能な分割 "
                f"ex_date={ambiguous.ex_date.isoformat()}, "
                f"factor={ambiguous.factor}）"
            ),
        )

    resolved = _apply_basis(working, cumulative, _classify(working, cumulative))
    if has_mixed_basis_signature(resolved["close"]):
        return NormalizationRejection(
            symbol=symbol,
            reason=f"分割調整の混在を解消できない（{_describe_splits(splits)}）",
        )
    return resolved


def _describe_splits(splits: Sequence[SplitEvent]) -> str:
    """The splits a rejection message names, for an operator reading a report."""
    if not splits:
        return "応答に分割イベントが無い"
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


def _apply_basis(
    bars: pd.DataFrame, cumulative: pd.Series, is_adjusted: pd.Series
) -> pd.DataFrame:
    """Undo Yahoo's adjustment on the rows flagged as adjusted.

    Args:
        bars: One symbol's rows, ascending.
        cumulative: Per-row cumulative split factor.
        is_adjusted: `True` where the row is taken to carry Yahoo's split
            adjustment, so its raw price is `close x factor`.

    Returns:
        A new frame on the as-traded basis.
    """
    factors = cumulative.where(is_adjusted, 1.0)
    raw = bars.copy()
    for column in _PRICE_COLUMNS:
        if column in raw.columns:
            raw[column] = raw[column] * factors
    if "volume" in raw.columns:
        raw["volume"] = _scaled_volume(raw["volume"], 1.0 / factors)
    return raw


def _ambiguous_split(
    first_date: date, splits: Sequence[SplitEvent]
) -> SplitEvent | None:
    """The first split too small to classify rows against, if any.

    Only splits that actually move a row's cumulative factor matter; a split
    on or before the window's first date leaves every row alone.
    """
    for split in splits:
        if split.ex_date <= first_date:
            continue
        if 1.0 / _MIN_CLASSIFIABLE_FACTOR < split.factor < _MIN_CLASSIFIABLE_FACTOR:
            return split
    return None


def _log_distance(candidate: float, reference: float) -> float:
    """|log(candidate / reference)|, or infinity when the ratio is undefined."""
    if not (_is_usable(candidate) and _is_usable(reference)):
        return math.inf
    return abs(math.log(candidate / reference))


def _classify(bars: pd.DataFrame, cumulative: pd.Series) -> pd.Series:
    """Decide, per row, whether Yahoo had applied the split adjustment.

    Walks backwards from the newest bar, which is the only row whose basis is
    known without a guess: no split in the response postdates it, so its two
    hypotheses coincide. Each earlier row then takes whichever hypothesis sits
    closer (in log space) to the raw close already settled for the row after
    it, which is what makes a one-day basis flip visible at all.

    The anchor is seeded as *unadjusted* and re-seeded as adjusted only if
    that removes a jump between the last two rows — the one case where a
    response window ending before a known split would otherwise poison the
    whole backward pass.
    """
    closes = [
        float(value)
        for value in pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)
    ]
    factors = [float(value) for value in cumulative.to_numpy(dtype=float)]
    flags = _classify_from_anchor(closes, factors, anchor_adjusted=False)
    if _has_trailing_jump(closes, factors, flags):
        alternative = _classify_from_anchor(closes, factors, anchor_adjusted=True)
        if not _has_trailing_jump(closes, factors, alternative):
            flags = alternative
    return pd.Series(flags, index=bars.index)


def _classify_from_anchor(
    closes: list[float], factors: list[float], *, anchor_adjusted: bool
) -> list[bool]:
    """One backward classification pass, given the newest row's hypothesis."""
    count = len(closes)
    flags = [False] * count
    flags[-1] = anchor_adjusted
    reference = closes[-1] * (factors[-1] if anchor_adjusted else 1.0)
    for position in range(count - 2, -1, -1):
        adjusted_candidate = closes[position] * factors[position]
        as_is_candidate = closes[position]
        take_adjusted = _log_distance(adjusted_candidate, reference) <= _log_distance(
            as_is_candidate, reference
        )
        flags[position] = take_adjusted
        reference = adjusted_candidate if take_adjusted else as_is_candidate
    return flags


def _has_trailing_jump(
    closes: list[float], factors: list[float], flags: list[bool]
) -> bool:
    """Whether the two newest rows disagree by more than a jump under `flags`.

    Safe to index two rows back: classification only runs on a series that
    carries the mixed-basis signature, which takes two jumps and so at least
    three rows.
    """
    settled = [
        close * (factor if is_adjusted else 1.0)
        for close, factor, is_adjusted in zip(closes, factors, flags, strict=True)
    ]
    return _log_distance(settled[-1], settled[-2]) > _JUMP_LOG_THRESHOLD
