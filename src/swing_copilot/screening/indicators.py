"""Shared pandas indicator functions.

Reused by screening, chart data, and backtesting so all three agree on the
same values (`docs/04_detailed_design.md` 2.1 #5, 3.11; `docs/05_ui_design.md`
10.1).
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

# Identity-keyed cache of `_SymbolIndex`, so a caller that reuses one frame
# across many `as_of` values (the backtest loop) pays the grouping cost once
# instead of once per lookup.
#
# Deliberately NOT stored on `bars.attrs`: pandas deep-copies `attrs` in
# pandas' own `__finalize__`, so parking the index there makes every derived
# frame clone the whole per-symbol grouping -- worse than the scan it replaces.
# Keyed on `id()` with a weak reference alongside it, because `id` values are
# reused after garbage collection; the identity re-check rejects a stale hit.
_SYMBOL_INDEX_CACHE_SIZE = 4


def percentile_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """Return deterministic 0..1 percentile ranks with equal values tied."""
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    if count == 0:
        return {}
    if count == 1:
        return {ordered[0][0]: 0.5}

    ranks: dict[str, float] = {}
    start = 0
    while start < count:
        end = start + 1
        while end < count and ordered[end][1] == ordered[start][1]:
            end += 1
        percentile = ((start + end - 1) / 2) / (count - 1)
        for symbol, _value in ordered[start:end]:
            ranks[symbol] = percentile
        start = end
    return ranks


class _SymbolIndex:
    """Date-sorted per-symbol views of one bars frame, cut by binary search.

    The naive lookup masks the whole frame twice per call. A backtest asks for
    every symbol on every simulated day, so that is O(days x symbols x rows) --
    for a multi-year S&P 500 run, on the order of 10^11 row comparisons, which
    is what made full-period backtests unrunnable. Grouping once and slicing by
    `searchsorted` makes each lookup O(log n) without changing what is returned.
    """

    __slots__ = ("_groups",)

    def __init__(self, bars: pd.DataFrame) -> None:
        self._groups: dict[str, tuple[pd.DataFrame, np.ndarray]] = {}
        # `kind="stable"` so rows sharing a date keep their original relative
        # order, matching the previous mask-then-sort behavior.
        ordered = bars.sort_values(["symbol", "date"], kind="stable")
        for symbol, group in ordered.groupby("symbol", sort=False):
            dates = pd.to_datetime(group["date"]).to_numpy()
            self._groups[str(symbol)] = (group, dates)

    def slice_to(self, symbol: str, as_of: date) -> pd.DataFrame | None:
        """Return `symbol`'s rows dated at or before `as_of`, or `None`."""
        entry = self._groups.get(symbol)
        if entry is None:
            return None
        group, dates = entry
        cut = int(np.searchsorted(dates, np.datetime64(pd.Timestamp(as_of)), "right"))
        return group.iloc[:cut] if cut else None


_symbol_index_cache: dict[
    int, tuple[weakref.ReferenceType[pd.DataFrame], _SymbolIndex]
] = {}


def _symbol_index(bars: pd.DataFrame) -> _SymbolIndex:
    """Return `bars`' cached symbol index, building it on first use."""
    key = id(bars)
    cached = _symbol_index_cache.get(key)
    if cached is not None:
        frame_ref, index = cached
        if frame_ref() is bars:
            return index

    index = _SymbolIndex(bars)
    if len(_symbol_index_cache) >= _SYMBOL_INDEX_CACHE_SIZE:
        # Plain FIFO eviction: callers reuse one frame at a time, so the cache
        # exists to serve the current run, not to retain history.
        _symbol_index_cache.pop(next(iter(_symbol_index_cache)))
    _symbol_index_cache[key] = (weakref.ref(bars), index)
    return index


def symbol_bars(bars: pd.DataFrame, symbol: str, as_of: date) -> pd.DataFrame | None:
    """Return `symbol`'s bars up to `as_of`, sorted by date, or `None` if empty.

    This is the single gateway through which screening reads price history, and
    it always applies the `as_of` cutoff. Callers therefore may hand it a frame
    that extends past `as_of` (the backtest does, to keep one cacheable frame
    for the whole run) without leaking look-ahead.

    Args:
        bars: Tidy bars (`symbol, date, open, high, low, close, volume, ...`).
        symbol: Ticker to select.
        as_of: Point-in-time cutoff (inclusive).

    Returns:
        The symbol's bars, sorted by date, or `None` if there are none.
    """
    if bars.empty:
        return None
    return _symbol_index(bars).slice_to(symbol, as_of)


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average; `NaN` until `window` observations are available.

    Args:
        series: Input values (e.g. daily close).
        window: Number of periods to average.

    Returns:
        The rolling mean, same index as `series`.
    """
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """EMA seeded by the first SMA, requiring twice the period of history.

    The initial SMA seed avoids a first-observation bias. Returning ``NaN``
    through ``2 * period - 1`` guarantees that a regime calculation never
    silently treats a shallow history as an established EMA trend.
    """
    result = pd.Series(float("nan"), index=series.index, dtype=float)
    if len(series) < 2 * period:
        return result
    alpha = 2.0 / (period + 1.0)
    value = float(series.iloc[:period].mean())
    for index in range(period, len(series)):
        value = alpha * float(series.iloc[index]) + (1.0 - alpha) * value
        if index >= 2 * period - 1:
            result.iloc[index] = value
    return result


def wilder_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-smoothed RSI; `NaN` until `period` observations are available.

    Args:
        series: Input values (e.g. daily close).
        period: Smoothing period.

    Returns:
        RSI in `[0, 100]`. A window with no losses is `100`, not `NaN`
        (division by a zero average loss is guarded explicitly).
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.where(avg_loss != 0, 100.0).where(avg_gain.notna())


def wilder_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Wilder-smoothed Average True Range.

    `NaN` until `period` observations are available.

    Args:
        high: Daily high.
        low: Daily low.
        close: Daily close.
        period: Smoothing period.

    Returns:
        ATR, same index as the inputs.
    """
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
