"""Shared pandas indicator functions.

Reused by screening, chart data, and backtesting so all three agree on the
same values (`docs/04_detailed_design.md` 2.1 #5, 3.11; `docs/05_ui_design.md`
10.1).
"""

from __future__ import annotations

import enum
import weakref
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

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
#
# Caching an index at all assumes the frame is treated as immutable once it
# has been handed to `symbol_bars` -- see that function's docstring. The row
# count stored beside the weak reference catches the one mutation that is
# cheap to detect (rows appended or dropped); an in-place *value* edit cannot
# be detected without re-reading the frame, which is the cost the cache exists
# to avoid.
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


class _IndicatorKind(enum.Enum):
    """The indicator columns `SymbolWindow` precomputes per symbol."""

    SMA = "sma"
    RSI = "rsi"
    ATR = "atr"
    MEAN_VOLUME = "mean_volume"


def _trailing_mean(values: np.ndarray, window: int) -> np.ndarray:
    """Trailing mean of `window` values, matching `Series.tail(w).mean()` exactly.

    Deliberately *not* `Series.rolling(w).mean()`: pandas' rolling mean is a
    streaming add/remove with Kahan compensation, so its result can differ in
    the last bits from the pairwise summation `Series.mean()` performs over one
    window (measured: ~43% of windows on float input). Every call site this
    replaces computed `tail(w).mean()`, and Issue #214 requires the optimized
    backtest to reproduce the previous equity curve bit for bit, so the summation
    order has to be reproduced too.

    Args:
        values: Float64 column, oldest first.
        window: Number of trailing observations to average.

    Returns:
        Same length as `values`; the first `window - 1` entries are `NaN`,
        exactly like a `min_periods=window` rolling mean. `NaN` inputs are
        skipped the way `Series.mean()` skips them.
    """
    result = np.full(len(values), np.nan)
    if len(values) < window:
        return result
    missing = np.isnan(values)
    sums = sliding_window_view(np.where(missing, 0.0, values), window).sum(axis=1)
    counts = window - sliding_window_view(missing, window).sum(axis=1)
    with np.errstate(invalid="ignore"):
        result[window - 1 :] = sums / counts
    return result


class _SymbolSeries:
    """One symbol's full-history bars plus lazily built indicator columns.

    An indicator column is computed once over the symbol's whole history and
    kept here, so a caller sweeping many `as_of` values (the backtest) pays for
    it once instead of once per simulated day.
    """

    __slots__ = ("_closes", "_columns", "dates", "frame")

    def __init__(self, frame: pd.DataFrame, dates: np.ndarray) -> None:
        self.frame = frame
        self.dates = dates
        self._closes: np.ndarray | None = None
        self._columns: dict[tuple[_IndicatorKind, int], np.ndarray] = {}

    @property
    def closes(self) -> np.ndarray:
        """The symbol's closes as a float64 array, oldest first."""
        if self._closes is None:
            self._closes = self.frame["close"].to_numpy(dtype=float)
        return self._closes

    def column(self, kind: _IndicatorKind, window: int) -> np.ndarray:
        """Return one full-history indicator column, computing it on first use."""
        key = (kind, window)
        cached = self._columns.get(key)
        if cached is None:
            cached = self._compute(kind, window)
            self._columns[key] = cached
        return cached

    def _compute(self, kind: _IndicatorKind, window: int) -> np.ndarray:
        frame = self.frame
        match kind:
            case _IndicatorKind.SMA:
                return sma(frame["close"], window).to_numpy(dtype=float)
            case _IndicatorKind.RSI:
                return wilder_rsi(frame["close"], window).to_numpy(dtype=float)
            case _IndicatorKind.ATR:
                return wilder_atr(
                    frame["high"], frame["low"], frame["close"], window
                ).to_numpy(dtype=float)
            case _IndicatorKind.MEAN_VOLUME:
                return _trailing_mean(frame["volume"].to_numpy(dtype=float), window)


class SymbolWindow:
    """One symbol's bars at or before `as_of`, plus that day's indicator values.

    Together with `symbol_window` this is the single gateway through which
    screening reads price history, and it always applies the `as_of` cutoff:
    every accessor here reads position `bar_count - 1` (or a `[:bar_count]`
    prefix) of a column, and never a later row.

    Reading a *precomputed* column at that position is equivalent to computing
    the indicator on the `as_of` prefix alone, because every indicator offered
    here is causal -- an SMA/trailing mean over the trailing `window`
    observations, or a Wilder EWM recursion seeded at the first row -- so the
    value at a row is a function of that row and earlier rows only. pandas
    evaluates all of them in index order, which makes the shared prefix
    bit-identical, not merely equal within tolerance (Issue #214;
    `tests/screening/test_indicators.py` pins it, and
    `tests/screening/test_pipeline.py::TestNoLookAheadFromUnslicedBars` pins the
    same property end to end).
    """

    __slots__ = ("_cut", "_series")

    def __init__(self, series: _SymbolSeries, cut: int) -> None:
        """Bind one symbol's history to the `as_of` row count `cut`.

        Args:
            series: The symbol's full-history bars and cached columns.
            cut: Number of rows dated at or before `as_of`; at least one.
        """
        self._series = series
        self._cut = cut

    @property
    def bar_count(self) -> int:
        """Number of bars dated at or before `as_of` (always at least one)."""
        return self._cut

    @property
    def bars(self) -> pd.DataFrame:
        """The symbol's rows dated at or before `as_of`, sorted by date."""
        return self._series.frame.iloc[: self._cut]

    @property
    def close(self) -> float:
        """The close of the last bar at or before `as_of`."""
        return float(self._series.closes[self._cut - 1])

    def sma(self, window: int) -> float:
        """Simple moving average at `as_of`; `NaN` before `window` bars exist."""
        return self._value(_IndicatorKind.SMA, window)

    def sma_history(self, window: int) -> np.ndarray:
        """The simple moving average for every bar at or before `as_of`."""
        return self._series.column(_IndicatorKind.SMA, window)[: self._cut]

    def rsi(self, period: int) -> float:
        """Wilder RSI at `as_of`; `NaN` before `period` bars exist."""
        return self._value(_IndicatorKind.RSI, period)

    def atr(self, period: int) -> float:
        """Wilder ATR at `as_of`; `NaN` before `period` bars exist."""
        return self._value(_IndicatorKind.ATR, period)

    def mean_volume(self, window: int) -> float:
        """Mean volume over the trailing `window` bars ending at `as_of`.

        `NaN` when fewer than `window` bars exist: the callers this replaces
        all skipped a symbol with a shorter history before averaging, so a
        partial window was never an admissible average and is not one here.
        """
        return self._value(_IndicatorKind.MEAN_VOLUME, window)

    def _value(self, kind: _IndicatorKind, window: int) -> float:
        return float(self._series.column(kind, window)[self._cut - 1])


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
        self._groups: dict[str, _SymbolSeries] = {}
        # `kind="stable"` so rows sharing a date keep their original relative
        # order, matching the previous mask-then-sort behavior.
        ordered = bars.sort_values(["symbol", "date"], kind="stable")
        for symbol, group in ordered.groupby("symbol", sort=False):
            dates = pd.to_datetime(group["date"]).to_numpy()
            self._groups[str(symbol)] = _SymbolSeries(group, dates)

    def window_to(self, symbol: str, as_of: date) -> SymbolWindow | None:
        """Return a window over `symbol`'s rows dated at or before `as_of`."""
        series = self._groups.get(symbol)
        if series is None:
            return None
        cut = int(
            np.searchsorted(series.dates, np.datetime64(pd.Timestamp(as_of)), "right")
        )
        return SymbolWindow(series, cut) if cut else None


_symbol_index_cache: dict[
    int, tuple[weakref.ReferenceType[pd.DataFrame], int, _SymbolIndex]
] = {}


def _symbol_index(bars: pd.DataFrame) -> _SymbolIndex:
    """Return `bars`' cached symbol index, building it on first use."""
    key = id(bars)
    cached = _symbol_index_cache.get(key)
    if cached is not None:
        frame_ref, row_count, index = cached
        if frame_ref() is bars and row_count == len(bars):
            return index

    index = _SymbolIndex(bars)
    # Drop entries whose frame the caller has already released: each one pins
    # a full per-symbol copy of a frame that is otherwise garbage, and the
    # FIFO cap alone would keep up to `_SYMBOL_INDEX_CACHE_SIZE` of them alive
    # for the process lifetime.
    for dead_key in [
        cached_key
        for cached_key, (cached_ref, _rows, _index) in _symbol_index_cache.items()
        if cached_ref() is None
    ]:
        del _symbol_index_cache[dead_key]
    if len(_symbol_index_cache) >= _SYMBOL_INDEX_CACHE_SIZE:
        # Plain FIFO eviction: callers reuse one frame at a time, so the cache
        # exists to serve the current run, not to retain history.
        _symbol_index_cache.pop(next(iter(_symbol_index_cache)))
    _symbol_index_cache[key] = (weakref.ref(bars), len(bars), index)
    return index


def symbol_window(bars: pd.DataFrame, symbol: str, as_of: date) -> SymbolWindow | None:
    """Return `symbol`'s point-in-time window at `as_of`, or `None` if empty.

    This and `symbol_bars` are the single gateway through which screening reads
    price history, and both always apply the `as_of` cutoff. Callers therefore
    may hand a frame that extends past `as_of` (the backtest does, to keep one
    cacheable frame for the whole run) without leaking look-ahead.

    Prefer this over `symbol_bars` whenever the caller only needs indicator
    values at `as_of`: the window reads them from columns computed once per
    symbol and cached on the frame, instead of recomputing a full-history
    rolling series and discarding all but its last point (Issue #214).

    The per-symbol index built from `bars` is cached against the frame's
    identity, so a frame handed to this function must be treated as immutable
    from then on. A row-count change is detected and rebuilds the index, but
    an in-place *value* edit (correcting a close) is not, and is served from
    the pre-edit index; build a new frame instead.

    Args:
        bars: Tidy bars (`symbol, date, open, high, low, close, volume, ...`).
        symbol: Ticker to select.
        as_of: Point-in-time cutoff (inclusive).

    Returns:
        The window, or `None` if the symbol has no bars at or before `as_of`.
    """
    if bars.empty:
        return None
    return _symbol_index(bars).window_to(symbol, as_of)


def symbol_bars(bars: pd.DataFrame, symbol: str, as_of: date) -> pd.DataFrame | None:
    """Return `symbol`'s bars up to `as_of`, sorted by date, or `None` if empty.

    See `symbol_window` for the `as_of` and caching contract, which this
    shares. Kept for the callers that genuinely need the raw rows (whole-window
    pattern detection, report diagnostics) rather than one day's indicators.

    Args:
        bars: Tidy bars (`symbol, date, open, high, low, close, volume, ...`).
        symbol: Ticker to select.
        as_of: Point-in-time cutoff (inclusive).

    Returns:
        The symbol's bars, sorted by date, or `None` if there are none.
    """
    window = symbol_window(bars, symbol, as_of)
    return None if window is None else window.bars


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
