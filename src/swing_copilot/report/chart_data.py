"""Per-candidate TradingView chart data (FR-09, `docs/05_ui_design.md` 8.4/10.1).

`build_chart_data()` fetches a warmup-buffered history so SMA50/SMA200 are
correct from the very first day of the visible display window, then slices
the output back down to that window before returning — the buffer itself
never leaks into `ChartData`. SMA math is not reimplemented here; it reuses
`screening.indicators.sma`, the same function `screening/technical_signals.py`
uses, so chart values and screening decisions can never silently disagree
(`docs/04_detailed_design.md` 2.1 #5).
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel

from swing_copilot.screening.indicators import sma

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd

    from swing_copilot.storage.market_store import MarketStore

_DISPLAY_LOOKBACK_MONTHS = 6
_APPROX_DAYS_PER_MONTH = 30
_SMA_WARMUP_BUFFER_DAYS = 300  # >=200 trading days under normal US market calendars
_SMA_SHORT_WINDOW = 50
_SMA_LONG_WINDOW = 200


class OHLCVPoint(BaseModel):
    """One daily OHLCV bar for the chart."""

    time: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class SMAPoint(BaseModel):
    """One SMA value on a given day."""

    time: str
    value: float


class ChartData(BaseModel):
    """Chart JSON handed to the report template for one symbol."""

    symbol: str
    ohlcv: list[OHLCVPoint]
    sma50: list[SMAPoint]
    sma200: list[SMAPoint]


def _sma_points(
    dates: pd.Series, values: pd.Series, display_start: date
) -> list[SMAPoint]:
    points = []
    for day, value in zip(dates, values, strict=True):
        if day < display_start or math.isnan(value):
            continue
        points.append(SMAPoint(time=day.isoformat(), value=float(value)))
    return points


def build_chart_data(
    symbol: str,
    market_store: MarketStore,
    as_of: date,
    lookback_months: int = _DISPLAY_LOOKBACK_MONTHS,
) -> ChartData:
    """Build chart data for one symbol's candidate detail card.

    Args:
        symbol: Ticker to build chart data for.
        market_store: Store to read historical daily bars from.
        as_of: Point-in-time cutoff; no bar after this date is included.
        lookback_months: Width of the visible display window, in months.

    Returns:
        OHLCV plus SMA50/SMA200 for the display window only. Days where
        SMA200 (or SMA50) has insufficient history are omitted, not
        zero-filled.
    """
    display_start = as_of - timedelta(days=lookback_months * _APPROX_DAYS_PER_MONTH)
    fetch_start = display_start - timedelta(days=_SMA_WARMUP_BUFFER_DAYS)

    bars = market_store.read_bars([symbol], fetch_start, as_of, as_of)
    bars = bars.sort_values("date").reset_index(drop=True)

    sma50 = sma(bars["close"], _SMA_SHORT_WINDOW)
    sma200 = sma(bars["close"], _SMA_LONG_WINDOW)

    display_bars = bars[bars["date"] >= display_start]
    ohlcv = [
        OHLCVPoint(
            time=row["date"].isoformat(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
        )
        for _, row in display_bars.iterrows()
    ]

    return ChartData(
        symbol=symbol,
        ohlcv=ohlcv,
        sma50=_sma_points(bars["date"], sma50, display_start),
        sma200=_sma_points(bars["date"], sma200, display_start),
    )
