"""Shared pandas indicator functions.

Reused by screening, chart data, and backtesting so all three agree on the
same values (`docs/04_detailed_design.md` 2.1 #5, 3.11; `docs/05_ui_design.md`
10.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from datetime import date


def symbol_bars(bars: pd.DataFrame, symbol: str, as_of: date) -> pd.DataFrame | None:
    """Return `symbol`'s bars up to `as_of`, sorted by date, or `None` if empty.

    Args:
        bars: Tidy bars (`symbol, date, open, high, low, close, volume, ...`).
        symbol: Ticker to select.
        as_of: Point-in-time cutoff (inclusive).

    Returns:
        The symbol's bars, sorted by date, or `None` if there are none.
    """
    subset = bars[(bars["symbol"] == symbol) & (bars["date"] <= as_of)].sort_values(
        "date"
    )
    return subset if not subset.empty else None


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
