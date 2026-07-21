"""Shared fixtures for backtest tests."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

TRADING_DAYS = [date(2027, 1, 1) + timedelta(days=i) for i in range(30)]
LONG_TRADING_DAYS = [date(2027, 1, 1) + timedelta(days=i) for i in range(100)]


def bar_row(
    symbol: str,
    day: date,
    ohlc: tuple[float, float, float, float],
    volume: int = 1_000_000,
) -> dict[str, object]:
    o, h, low, c = ohlc
    return {
        "symbol": symbol,
        "date": day,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": volume,
    }


def flat_bars(symbol: str, days: list[date], price: float) -> list[dict[str, object]]:
    return [bar_row(symbol, day, (price, price + 1, price - 1, price)) for day in days]


def bars_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)
