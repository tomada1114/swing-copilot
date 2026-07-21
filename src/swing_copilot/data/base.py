"""`DataProvider` contract: daily OHLCV fetch, adjusted-price-only (FR-02, NFR-07).

Concrete providers (yfinance today, EODHD at P4) normalize their own
provider-specific column layouts and adjustment conventions into this single
tidy schema (`symbol, date, open, high, low, close, volume`) so downstream
code never depends on a provider's raw shape. Per-symbol failures are
returned via `BarFetchResult.failures`, never raised, so a batch fetch always
completes with whatever symbols succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd

BARS_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume")


@dataclass(frozen=True, slots=True)
class FetchFailure:
    """One symbol's fetch failure within a batch request."""

    symbol: str
    reason: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class BarFetchResult:
    """Batch fetch outcome: whatever bars were obtained plus any failures."""

    bars: pd.DataFrame
    failures: tuple[FetchFailure, ...]


class DataProvider(Protocol):
    """Daily OHLCV fetch, abstracted over the underlying price data source."""

    def get_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> BarFetchResult:
        """Fetch daily OHLCV for `symbols` over `[start, end)`.

        Args:
            symbols: Ticker symbols to fetch.
            start: Inclusive range start.
            end: Exclusive range end — returned bars never include `end`.

        Returns:
            Adjusted OHLCV bars (columns: `symbol, date, open, high, low,
            close, volume`) for the symbols that succeeded, plus failures
            for the rest.
        """
        ...  # pragma: no cover

    def get_latest_bars(self, symbols: list[str], as_of: date) -> BarFetchResult:
        """Return each symbol's most recent daily bar on or before `as_of`.

        Args:
            symbols: Ticker symbols to fetch.
            as_of: Latest trading date to consider.

        Returns:
            At most one bar per symbol, plus failures for symbols with no
            bar available on or before `as_of`.
        """
        ...  # pragma: no cover
