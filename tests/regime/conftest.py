"""Shared fixtures for the Distribution Day forward-measurement tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd

FETCHED_AT = datetime(2027, 6, 1, tzinfo=UTC)
START = date(2027, 1, 1)


def bars_for(
    symbol: str,
    closes: list[float],
    *,
    start: date = START,
    volumes: list[int] | None = None,
) -> pd.DataFrame:
    """Build tidy bars for `symbol`, one calendar day apart.

    Volumes step up by default so the "higher volume than the prior day"
    precondition in `calculate_distribution_days` never silently suppresses a
    decline the test intended to count.
    """
    if volumes is None:
        volumes = [1_000_000 + index * 1_000 for index in range(len(closes))]
    rows = []
    previous = closes[0]
    for index, (close, volume) in enumerate(zip(closes, volumes, strict=True)):
        rows.append(
            {
                "symbol": symbol,
                "date": start + timedelta(days=index),
                "open": previous,
                "high": max(previous, close) + 0.5,
                "low": min(previous, close) - 0.5,
                "close": close,
                "volume": volume,
                "provider": "test",
                "fetched_at": FETCHED_AT,
            }
        )
        previous = close
    return pd.DataFrame(rows)


def sawtooth(length: int, *, base: float = 100.0) -> list[float]:
    """Alternating small declines and rises.

    Produces a steady stream of Distribution-Day-eligible declines so a scan
    over the series exercises every level rather than sitting in `NORMAL`.
    """
    closes = [base]
    for index in range(1, length):
        step = -0.006 if index % 3 else 0.011
        closes.append(closes[-1] * (1.0 + step))
    return closes


def market_bars(length: int = 120) -> pd.DataFrame:
    """SPY, QQQ, and ^VIX bars for compact, threshold-injected fixtures."""
    spy = sawtooth(length)
    qqq = sawtooth(length, base=300.0)
    vix = [15.0 + (index % 7) for index in range(length)]
    return pd.concat(
        [bars_for("SPY", spy), bars_for("QQQ", qqq), bars_for("^VIX", vix)],
        ignore_index=True,
    )
