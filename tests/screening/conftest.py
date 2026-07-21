"""Shared fixtures for screening tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from swing_copilot.config import load_settings


@dataclass(frozen=True, slots=True)
class FundamentalsSpec:
    accession_no: str
    fiscal_period_end: date
    filed_at: datetime
    net_income: float
    fcf: float
    equity: float
    assets: float


@pytest.fixture
def settings():
    return load_settings("config/settings.yaml")


def make_bars(
    symbol: str, closes: list[float], *, start: date, volume: int = 2_000_000
) -> pd.DataFrame:
    """Build a tidy bars DataFrame for `symbol` from a list of closes.

    High/low are set +/-0.5 around the close and open equals the prior
    close, which is enough for SMA/RSI/ATR to behave sensibly in tests.
    """
    dates = [start + timedelta(days=i) for i in range(len(closes))]
    rows = []
    prev_close = closes[0]
    for bar_date, close in zip(dates, closes, strict=True):
        rows.append(
            {
                "symbol": symbol,
                "date": bar_date,
                "open": prev_close,
                "high": max(prev_close, close) + 0.5,
                "low": min(prev_close, close) - 0.5,
                "close": close,
                "volume": volume,
            }
        )
        prev_close = close
    return pd.DataFrame(rows)


def make_fundamentals_row(symbol: str, spec: FundamentalsSpec) -> dict[str, object]:
    return {
        "accession_no": spec.accession_no,
        "symbol": symbol,
        "form": "10-Q",
        "fiscal_period_end": spec.fiscal_period_end,
        "filed_at": spec.filed_at,
        "revenue": abs(spec.net_income) * 10,
        "net_income": spec.net_income,
        "fcf": spec.fcf,
        "equity": spec.equity,
        "assets": spec.assets,
        "shares": 1_000_000.0,
        "source_url": "https://www.sec.gov/example",
        "fetched_at": datetime(2026, 7, 20, tzinfo=UTC),
    }
